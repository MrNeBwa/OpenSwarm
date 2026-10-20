"""Minimal asyncio client for `opencode serve` (REST + SSE): permissions, tool cards, usage, abort.

Endpoints used (from the opencode server docs): POST /session, POST /session/:id/prompt_async,
GET /event (SSE), POST /session/:id/permissions/:permissionID, POST /session/:id/abort,
GET /session/:id/message, GET /session/:id/diff. Event/field shapes differ a little between opencode
versions, so parsing is deliberately tolerant and anything unexpected falls back to `opencode run`.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse


class ApiError(RuntimeError):
    pass


class ApiUnavailable(ApiError):
    """Server unreachable / API shape not as expected -> caller should fall back to `opencode run`."""


class Http:
    def __init__(self, base_url: str, password: str = "") -> None:
        u = urlparse(base_url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.auth = ("Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()) if password else ""

    async def _open(self, method: str, path: str, body: Any | None):
        reader, writer = await asyncio.open_connection(self.host, self.port, limit=2**22)
        data = json.dumps(body).encode() if body is not None else b""
        head = [f"{method} {path} HTTP/1.1", f"Host: {self.host}:{self.port}", "Connection: close", "Accept: */*"]
        if self.auth:
            head.append(f"Authorization: {self.auth}")
        if body is not None:
            head += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
        await writer.drain()
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        return reader, writer, status, headers

    @staticmethod
    async def _body(reader: asyncio.StreamReader, headers: dict[str, str]) -> AsyncIterator[bytes]:
        if "chunked" in headers.get("transfer-encoding", "").lower():
            while True:
                size = int((await reader.readline()).strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    await reader.readline()
                    return
                yield await reader.readexactly(size)
                await reader.readline()
        elif "content-length" in headers:
            n = int(headers["content-length"])
            if n:
                yield await reader.readexactly(n)
        else:
            while chunk := await reader.read(65536):
                yield chunk

    async def request(self, method: str, path: str, body: Any | None = None, timeout: float = 30) -> tuple[int, Any]:
        async def go() -> tuple[int, Any]:
            reader, writer, status, headers = await self._open(method, path, body)
            try:
                raw = b"".join([c async for c in self._body(reader, headers)])
            finally:
                writer.close()
            try:
                return status, (json.loads(raw) if raw.strip() else None)
            except ValueError:
                return status, raw.decode(errors="replace")

        try:
            return await asyncio.wait_for(go(), timeout)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
            raise ApiUnavailable(f"{method} {path}: {exc!r}") from exc

    async def sse(self, path: str) -> AsyncIterator[dict[str, Any]]:
        try:
            reader, writer, status, headers = await self._open("GET", path, None)
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            raise ApiUnavailable(f"SSE {path}: {exc!r}") from exc
        if status != 200:
            writer.close()
            raise ApiUnavailable(f"SSE {path}: HTTP {status}")
        buf = b""
        try:
            async for chunk in self._body(reader, headers):
                buf += chunk.replace(b"\r\n", b"\n")
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    data = "\n".join(ln[5:].lstrip() for ln in block.decode(errors="replace").split("\n") if ln.startswith("data:"))
                    if data:
                        try:
                            yield json.loads(data)
                        except ValueError:
                            continue
        except (OSError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()


@dataclass
class ApiResult:
    ok: bool
    text: str = ""
    session_id: str = ""
    cost: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""


EventCb = Callable[[str, dict[str, Any]], None]  # kind, payload
PermissionCb = Callable[[dict[str, Any]], Awaitable[str]]  # request -> "once" | "always" | "reject"


def split_model(model: str) -> dict[str, str] | None:
    if "/" not in model:
        return None
    p, _, m = model.partition("/")
    return {"providerID": p, "modelID": m}


def _tool_summary(state: dict[str, Any]) -> str:
    inp = state.get("input") or {}
    for k in ("command", "filePath", "path", "pattern", "url", "query", "description"):
        if inp.get(k):
            return str(inp[k])[:200]
    return json.dumps(inp)[:200] if inp else ""


async def run_chat_api(
    http: Http,
    *,
    text: str,
    title: str = "",
    session_id: str = "",
    model: str = "",
    agent: str = "",
    files: list[dict[str, str]] | None = None,
    on_line: Callable[[str, bool], None] = lambda s, e: None,
    on_event: EventCb = lambda k, p: None,
    on_permission: PermissionCb | None = None,
    timeout: float = 1800,
    idle_poll: float = 120,
) -> ApiResult:
    # 1) session
    if not session_id:
        code, sess = await http.request("POST", "/session", {"title": title} if title else {})
        if code >= 300 or not isinstance(sess, dict) or not sess.get("id"):
            raise ApiUnavailable(f"POST /session -> {code} {str(sess)[:120]}")
        session_id = str(sess["id"])
    # 2) event stream first (so nothing is missed), then the prompt
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for ev in http.sse("/event"):
                queue.put_nowait(ev)
        except ApiUnavailable as exc:
            queue.put_nowait({"type": "_pump_error", "properties": {"error": str(exc)}})
        queue.put_nowait(None)

    pump_task = asyncio.create_task(pump())
    try:
        first = await asyncio.wait_for(queue.get(), 10)
        if first is None or first.get("type") == "_pump_error":
            raise ApiUnavailable("event stream did not open")
    except asyncio.TimeoutError as exc:
        pump_task.cancel()
        raise ApiUnavailable("no server.connected event") from exc

    body: dict[str, Any] = {"parts": [{"type": "text", "text": text}, *(files or [])]}
    if (m := split_model(model)):
        body["model"] = m
    if agent:
        body["agent"] = agent
    code, resp = await http.request("POST", f"/session/{session_id}/prompt_async", body)
    if code >= 300:
        pump_task.cancel()
        raise ApiUnavailable(f"prompt_async -> {code} {str(resp)[:160]}")

    roles: dict[str, str] = {}
    parts: dict[str, str] = {}
    order: list[str] = []
    usage: dict[str, tuple[float, dict[str, int]]] = {}
    tool_state: dict[str, str] = {}
    pending_line = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    err = ""

    def flush(force: bool = False) -> None:
        nonlocal pending_line
        while "\n" in pending_line:
            line, pending_line = pending_line.split("\n", 1)
            on_line(line, False)
        if force and pending_line:
            on_line(pending_line, False)
            pending_line = ""

    def add_text(pid: str, mid: str, new_full: str | None = None, delta: str = "") -> None:
        nonlocal pending_line
        if roles.get(mid) == "user":
            return
        if pid not in parts:
            parts[pid] = ""
            order.append(pid)
        if new_full is not None:
            if new_full.startswith(parts[pid]):
                delta = new_full[len(parts[pid]):]
            else:
                delta, parts[pid] = new_full, ""
        parts[pid] += delta
        pending_line += delta
        flush()

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                err = "timed out"
                break
            try:
                ev = await asyncio.wait_for(queue.get(), min(remaining, idle_poll))
            except asyncio.TimeoutError:
                done = await _assistant_done(http, session_id)  # quiet server: maybe we missed the idle event
                if done:
                    break
                continue
            if ev is None or ev.get("type") == "_pump_error":
                done = await _assistant_done(http, session_id)
                err = "" if done else "event stream closed"
                break
            typ, props = str(ev.get("type", "")), ev.get("properties") or ev.get("data") or {}
            sid = props.get("sessionID") or (props.get("info") or {}).get("sessionID") or (props.get("part") or {}).get("sessionID")
            if sid and sid != session_id:
                continue
            if typ == "message.updated":
                info = props.get("info") or {}
                roles[str(info.get("id"))] = str(info.get("role"))
                if info.get("role") == "assistant" and (info.get("tokens") or info.get("cost")):
                    t = info.get("tokens") or {}
                    usage[str(info["id"])] = (float(info.get("cost") or 0), {
                        "input": int(t.get("input") or 0), "output": int(t.get("output") or 0), "reasoning": int(t.get("reasoning") or 0)})
                    on_event("usage", _totals(usage))
            elif typ == "message.part.updated":
                part = props.get("part") or {}
                pid, mid, ptype = str(part.get("id")), str(part.get("messageID")), part.get("type")
                if ptype == "text" and part.get("text") is not None and part.get("text") != text:
                    add_text(pid, mid, new_full=str(part["text"]))
                elif ptype == "tool":
                    st = part.get("state") or {}
                    status = str(st.get("status", ""))
                    if tool_state.get(pid) != status:
                        tool_state[pid] = status
                        on_event("tool", {"id": pid, "tool": part.get("tool", "tool"), "status": status, "summary": _tool_summary(st),
                                          "output": str(st.get("output") or st.get("error") or "")[:600]})
            elif typ == "message.part.delta":
                if props.get("field", "text") == "text":
                    add_text(str(props.get("partID")), str(props.get("messageID")), delta=str(props.get("delta", "")))
            elif typ.startswith("permission.") and typ not in ("permission.replied",):
                req = {"id": props.get("id") or props.get("requestID") or props.get("permissionID"), "session": session_id,
                       "tool": props.get("type") or props.get("permission") or "tool",
                       "title": props.get("title") or props.get("permission") or "permission requested",
                       "detail": json.dumps(props.get("metadata") or props.get("patterns") or props.get("pattern") or "")[:400]}
                if req["id"]:
                    decision = await (on_permission(req) if on_permission else asyncio.sleep(0, "reject"))
                    c, _ = await http.request("POST", f"/session/{session_id}/permissions/{req['id']}", {"response": decision})
                    if c == 404:  # newer API shape
                        await http.request("POST", f"/permission/{req['id']}/reply", {"reply": decision})
            elif typ == "session.error":
                e = props.get("error") or {}
                err = str((e.get("data") or {}).get("message") or e.get("message") or e.get("name") or "session error")
                break
            elif typ == "session.idle" or (typ == "session.status" and (props.get("status") or {}).get("type") == "idle"):
                break
    except asyncio.CancelledError:
        await asyncio.shield(_abort(http, session_id))
        raise
    finally:
        pump_task.cancel()
    flush(force=True)
    final = "".join(parts[p] for p in order).strip()
    if not final and not err:
        final = await _last_assistant_text(http, session_id)
    cost, toks = _totals(usage)["cost"], _totals(usage)["tokens"]
    return ApiResult(ok=not err, text=final, session_id=session_id, cost=cost, tokens=toks, error=err)


def _totals(usage: dict[str, tuple[float, dict[str, int]]]) -> dict[str, Any]:
    toks = {"input": 0, "output": 0, "reasoning": 0}
    for _, t in usage.values():
        for k in toks:
            toks[k] += t.get(k, 0)
    return {"cost": round(sum(c for c, _ in usage.values()), 6), "tokens": toks}


async def _abort(http: Http, session_id: str) -> None:
    try:
        await http.request("POST", f"/session/{session_id}/abort", None, timeout=5)
    except ApiError:
        pass


async def _messages(http: Http, session_id: str) -> list[dict[str, Any]]:
    try:
        code, data = await http.request("GET", f"/session/{session_id}/message", timeout=15)
    except ApiError:
        return []
    return data if code == 200 and isinstance(data, list) else []


async def _assistant_done(http: Http, session_id: str) -> bool:
    msgs = await _messages(http, session_id)
    for m in reversed(msgs):
        info = m.get("info") or {}
        if info.get("role") == "assistant":
            return bool((info.get("time") or {}).get("completed"))
    return False


async def _last_assistant_text(http: Http, session_id: str) -> str:
    for m in reversed(await _messages(http, session_id)):
        if (m.get("info") or {}).get("role") == "assistant":
            return "\n".join(p.get("text", "") for p in m.get("parts", []) if p.get("type") == "text").strip()
    return ""
