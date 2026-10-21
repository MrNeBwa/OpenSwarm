"""OpenSwarm Web: a local app (asyncio HTTP + Server-Sent Events, standard library only).

    openswarm web            # prints a private URL (token), opens your browser
    openswarm web --app      # opens it as a chromeless "app" window when Chromium/Chrome/Brave is installed

Security model: binds to 127.0.0.1 only, every API call needs the random token printed at start-up, and the
Host/Origin headers are checked (blocks DNS-rebinding and drive-by requests from other web pages).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .broker import Broker
from .config import ConfigError, SwarmConfig, discover_models, find_config_path, load_config, reconcile_models
from .docs import DocError, read_file
from .git_manager import GitError, GitManager
from .automation import Automation
from .orchestrator import Hooks, Orchestrator, Selection
from .safety import audit_path, git_diff_summary, list_snapshots, path_allowed, restore_snapshot, audit
from .servers import ServerPool
from .skillpack import discover_skills
from .state import MODES, apply_state, load_state, save_state, snapshot, state_path

MAX_BODY = 64 * 1024 * 1024
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error"}
HIDDEN = {".git", ".swarm", "__pycache__", "node_modules", ".venv"}


class HttpError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status, self.msg = status, msg


def sessions_dir() -> Path:
    return state_path().parent / "sessions"


class Session:
    """One conversation/run history (a browser tab). Several can be running at the same time."""

    def __init__(self, sid: str, workdir: Path, mode: str, title: str = "New session") -> None:
        self.id, self.workdir, self.mode, self.title = sid, workdir, mode, title
        self.created = self.updated = time.time()
        self.oc_session = ""  # opencode session id (chat continuity)
        self.turns = 0
        self.busy = False
        self.task: asyncio.Task | None = None
        self.events: list[dict[str, Any]] = []
        self.loaded = True
        self.listeners: set[asyncio.Queue] = set()
        self.perms: dict[str, asyncio.Future] = {}
        self.snapshot = ""

    # ---------------------------------------------------------------- storage
    @property
    def meta_path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return sessions_dir() / f"{self.id}.jsonl"

    def meta(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "workdir": str(self.workdir), "mode": self.mode,
                "created": self.created, "updated": self.updated, "oc_session": self.oc_session,
                "turns": self.turns, "busy": self.busy}

    def save_meta(self) -> None:
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            self.meta_path.write_text(json.dumps(self.meta()), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def from_meta(cls, m: dict[str, Any]) -> "Session":
        s = cls(m["id"], Path(m["workdir"]), m.get("mode", "chat"), m.get("title", "Session"))
        s.created, s.updated = m.get("created", time.time()), m.get("updated", time.time())
        s.oc_session, s.turns = m.get("oc_session", ""), m.get("turns", 0)
        s.loaded = False
        return s

    def load_events(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            with self.log_path.open(encoding="utf-8") as fh:
                self.events = [json.loads(ln) for ln in fh if ln.strip()][-50_000:]
        except (OSError, ValueError):
            self.events = []

    # ----------------------------------------------------------------- events
    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        self.load_events()
        data = {k: v for k, v in data.items() if k not in ("id", "t", "type")}  # reserved
        ev = {"id": (self.events[-1]["id"] + 1) if self.events else 1, "t": round(time.time(), 2), "type": type_, **data}
        self.events.append(ev)
        self.updated = ev["t"]
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        except OSError:
            pass
        for q in list(self.listeners):
            q.put_nowait(ev)
        return ev


class WebApp:
    def __init__(self, cfg: SwarmConfig, workdir: Path, port: int, token: str) -> None:
        self.cfg, self.workdir, self.port, self.token = cfg, workdir, port, token
        self.pool = ServerPool(cfg)
        self.automation = Automation(self)
        self.sessions: dict[str, Session] = {}
        self._ws: dict[str, tuple[GitManager, Broker]] = {}
        try:
            for mp in sorted(sessions_dir().glob("*.json")):
                m = json.loads(mp.read_text(encoding="utf-8"))
                self.sessions[m["id"]] = Session.from_meta(m)
        except (OSError, ValueError, KeyError):
            pass

    # -------------------------------------------------------------- workspace
    async def workspace(self, path: Path) -> tuple[GitManager, Broker]:
        key = str(path)
        if key not in self._ws:
            git = await GitManager.discover(path, self.cfg.opencode.inherit_paths, self.cfg.opencode.link_mode)
            await git.ensure_ready()
            self._ws[key] = (git, Broker(git.swarm_dir / "swarm.db"))
        return self._ws[key]

    # ------------------------------------------------------------------ state
    def mode_list(self) -> list[dict[str, str]]:
        core = [("chat", "💬", "Chat", "One agent, continuous session, in any directory"),
                ("auto", "🧭", "Auto", "Picks the best mode for your request"),
                ("storm", "⚡", "Storm", "Planner splits the task → parallel workers (git worktrees) → merge"),
                ("best", "🏆", "Best of N", "N independent attempts → verify → judge → merge the winner (git)"),
                ("cowork", "🤝", "Cowork", "Architect → coder → reviewer loop"),
                ("research", "🔎", "Research", "Parallel researchers → synthesized, cited report"),
                ("write", "✍️", "Write", "Outline → parallel section drafts → editor"),
                ("review", "🛡️", "Review", "Parallel review lenses → prioritized report")]
        out = [{"id": i, "icon": ic, "label": lb, "desc": d, "group": "Modes"} for i, ic, lb, d in core]
        out += [{"id": f"wf:{w.id}", "icon": "🧩", "label": w.label, "desc": w.description, "group": "Workflows"} for w in self.cfg.workflows]
        return out

    def valid_mode(self, m: Any) -> bool:
        return isinstance(m, str) and m in {x["id"] for x in self.mode_list()}

    def state(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "version": __version__, "workdir": str(self.workdir), "home": str(Path.home()),
            "modes": self.mode_list(), "mode": c.default_mode, "parallel": c.max_parallel,
            "sandbox": c.security.sandbox == "bwrap", "bwrap": bool(shutil.which("bwrap")),
            "allowed_roots": c.security.allowed_roots,
            "automation": {"schedules": [x.id for x in c.schedules], "triggers": [x.id for x in c.triggers]},
            "models": [{"id": m.id, "label": m.display()} for m in c.models],
            "roles": {"planner": c.roles.planner, "coder": c.roles.coder, "reviewer": c.roles.reviewer},
            "skills": [{"id": s.id, "label": s.label, "category": s.category, "default": s.default, "hint": s.prompt_hint} for s in c.skills],
            "presets": [p.model_dump() for p in c.presets],
            "native_skills": discover_skills(self.workdir),
            "sessions": sorted((s.meta() for s in self.sessions.values()), key=lambda m: -m["updated"]),
        }

    # ------------------------------------------------------------------- runs
    def new_session(self, workdir: Path, mode: str, title: str) -> Session:
        s = Session(uuid.uuid4().hex[:8], workdir, mode, title or "New session")
        self.sessions[s.id] = s
        s.save_meta()
        return s

    async def start_run(self, s: Session, body: dict[str, Any]) -> None:
        if s.busy:
            raise HttpError(409, "This session is busy - open another session or cancel the run.")
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise HttpError(400, "empty prompt")
        mode = body.get("mode") if self.valid_mode(body.get("mode")) else s.mode
        models = body.get("models") or {}
        skills = [x for x in body.get("skills", []) if isinstance(x, str)]
        parallel = body.get("parallel")
        cfg = copy.copy(self.cfg)  # per-run overrides must not leak into other sessions
        if isinstance(parallel, int) and 0 <= parallel <= 64:
            cfg.max_parallel = parallel
        sel = Selection(
            mode=mode, planner_model=str(models.get("planner", "")), coder_model=str(models.get("coder", "")),
            reviewer_model=str(models.get("reviewer", "")), skills=skills, chat_id=f"chat-{s.id}",
            chat_title=s.title, workdir=s.workdir, session_id=s.oc_session,
            continue_session=bool(s.turns > 0 and not s.oc_session and mode == "chat"),
            sandbox=body.get("sandbox") if isinstance(body.get("sandbox"), bool) else None,
            candidate_models=[m for m in (body.get("candidates") or []) if isinstance(m, str)],
        )
        try:
            git, broker = await self.workspace(s.workdir)
        except (GitError, OSError) as exc:
            raise HttpError(400, str(exc))
        s.mode = mode
        if s.title in ("New session", "") and s.turns == 0:
            s.title = prompt.replace("\n", " ")[:48]
        s.busy = True
        s.save_meta()
        st = snapshot(mode, sel.planner_model or cfg.roles.planner, sel.coder_model or cfg.roles.coder,
                      sel.reviewer_model or cfg.roles.reviewer, skills)
        st["parallel"] = cfg.max_parallel
        save_state(st)
        s.emit("user", text=prompt, mode=mode)
        hooks = Hooks(
            on_agent=lambda a, t, r: s.emit("agent", agent=a, title=t, role=r),
            on_status=lambda a, st_: s.emit("status", agent=a, status=st_),
            on_line=lambda a, t, e: s.emit("line", agent=a, text=t[:3000], err=bool(e)),
            on_log=lambda m: s.emit("log", text=m),
            on_result=lambda a, t, k: s.emit("result", agent=a, text=t, kind=k),
            on_event=lambda a, k, p: self._on_event(s, a, k, p),
            ask_permission=lambda a, req: self._ask(s, a, req),
        )
        orch = Orchestrator(cfg, git, broker, sel, hooks, self.pool)
        s.task = asyncio.create_task(self._run(s, orch, prompt, mode))

    def _on_event(self, s: Session, agent: str, kind: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        if "id" in payload:  # never let a payload field shadow the event's own sequence number "id"
            payload["tid"] = payload.pop("id")
        if kind == "snapshot":
            s.snapshot = payload.get("tid", "")
        if kind == "permission":
            s.emit("permission", agent=agent, req={**payload, "id": payload.get("tid")})
        elif kind == "permission_resolved":
            s.emit("permission_resolved", pid=payload.get("tid"), decision=payload.get("decision"))
        else:
            s.emit(kind, agent=agent, **payload)

    async def _ask(self, s: Session, agent: str, req: dict[str, Any]) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        s.perms[str(req.get("id"))] = fut
        try:
            return await fut
        finally:
            s.perms.pop(str(req.get("id")), None)

    async def _run(self, s: Session, orch: Orchestrator, prompt: str, mode: str) -> None:
        try:
            summary = await orch.run(prompt)
            if mode == "chat":
                s.turns += 1
                if summary.session_id and not s.oc_session:
                    s.oc_session = summary.session_id
                    s.emit("log", text=f"opencode session {summary.session_id} captured")
            s.emit("done", ok=summary.ok, merged=summary.merged, failed=summary.failed, snapshot=summary.snapshot, usage=summary.usage, mode=mode)
        except asyncio.CancelledError:
            s.emit("done", ok=False, merged=[], failed=["cancelled"])
        except Exception as exc:  # never let one run take the server down
            s.emit("log", text=f"run crashed: {exc!r}")
            s.emit("done", ok=False, merged=[], failed=["crashed"])
        finally:
            for fut in s.perms.values():
                if not fut.done():
                    fut.set_result("reject")
            s.busy, s.task = False, None
            s.save_meta()

    # ---------------------------------------------------------------- project files
    def agents_md(self, s: Session) -> str:
        p = s.workdir / "AGENTS.md"
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    async def shutdown(self) -> None:
        for s in self.sessions.values():
            if s.task:
                s.task.cancel()
        await asyncio.sleep(0)
        await self.pool.close()
        for _, broker in self._ws.values():
            broker.close()

    # ------------------------------------------------------------------ files
    @staticmethod
    def _inside(root: Path, rel: str) -> Path:
        p = (root / rel).resolve()
        if p != root.resolve() and root.resolve() not in p.parents:
            raise HttpError(403, "path escapes the session directory")
        return p

    def list_dir(self, s: Session, rel: str) -> dict[str, Any]:
        p = self._inside(s.workdir, rel)
        if not p.is_dir():
            raise HttpError(404, "not a directory")
        items = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name in HIDDEN:
                continue
            try:
                items.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else e.stat().st_size})
            except OSError:
                continue
        return {"path": str(p.relative_to(s.workdir.resolve())) if p != s.workdir.resolve() else "", "items": items[:500]}

    def read(self, s: Session, rel: str) -> dict[str, Any]:
        p = self._inside(s.workdir, rel)
        if not p.is_file():
            raise HttpError(404, "no such file")
        try:
            text = read_file(p)
        except (DocError, OSError, Exception) as exc:  # noqa: BLE001 - report any parser failure to the UI
            return {"name": p.name, "text": f"(cannot display: {exc})", "truncated": False}
        return {"name": p.name, "text": text[:200_000], "truncated": len(text) > 200_000}

    def list_dirs(self, path: str) -> dict[str, Any]:
        p = Path(path or Path.home()).expanduser().resolve()
        if not path_allowed(self.cfg, p):
            roots = self.cfg.security.allowed_roots
            p = Path(roots[0]).expanduser().resolve()
        if not p.is_dir():
            raise HttpError(404, "not a directory")
        subs = []
        try:
            for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if e.is_dir() and not e.name.startswith("."):
                    subs.append(e.name)
        except OSError:
            pass
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": subs[:400]}

    # ------------------------------------------------------------------- http
    def _check(self, method: str, headers: dict[str, str], q: dict[str, list[str]], path: str) -> None:
        host = headers.get("host", "")
        if host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            raise HttpError(403, "bad Host header")
        origin = headers.get("origin")
        if origin and origin not in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            raise HttpError(403, "bad Origin")
        if path.startswith("/api/"):
            tok = headers.get("x-token") or (q.get("token") or [""])[0]
            if not secrets.compare_digest(tok, self.token):
                raise HttpError(403, "bad token")

    async def route(self, method: str, path: str, q: dict[str, list[str]], headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter) -> None:
        self._check(method, headers, q, path)
        if method == "GET" and path == "/":
            if (q.get("token") or [""])[0] != self.token:
                raise HttpError(403, "Open the URL printed by `openswarm web` (it contains the access token).")
            html = Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            return await _send(writer, 200, html.encode(), "text/html; charset=utf-8", csp=True)
        if not path.startswith("/api/"):
            raise HttpError(404, "not found")
        js = json.loads(body.decode() or "{}") if body and "json" in headers.get("content-type", "") else {}
        parts = [unquote(x) for x in path.split("/")[2:]]
        if parts == ["state"] and method == "GET":
            return await _json(writer, self.state())
        if parts == ["audit"] and method == "GET":
            try:
                lines = audit_path().read_text(encoding="utf-8").splitlines()[-int((q.get("n") or ["100"])[0]):]
            except OSError:
                lines = []
            return await _json(writer, {"lines": [json.loads(x) for x in lines if x.startswith("{")]})
        if parts == ["dirs"] and method == "GET":
            return await _json(writer, self.list_dirs((q.get("path") or [""])[0]))
        if parts == ["sessions"] and method == "POST":
            wd = Path(str(js.get("workdir") or self.workdir)).expanduser().resolve()
            if not wd.is_dir():
                raise HttpError(400, f"{wd} is not a directory")
            if not path_allowed(self.cfg, wd):
                raise HttpError(403, f"{wd} is outside security.allowed_roots")
            mode = js.get("mode") if self.valid_mode(js.get("mode")) else self.cfg.default_mode
            return await _json(writer, self.new_session(wd, mode, str(js.get("title", ""))).meta())
        if len(parts) >= 2 and parts[0] == "sessions":
            s = self.sessions.get(parts[1])
            if not s:
                raise HttpError(404, "no such session")
            rest = parts[2:]
            if not rest and method == "GET":
                s.load_events()
                return await _json(writer, {**s.meta(), "events": s.events[-20000:]})
            if not rest and method == "DELETE":
                if s.task:
                    s.task.cancel()
                self.sessions.pop(s.id, None)
                for pth in (s.meta_path, s.log_path):
                    pth.unlink(missing_ok=True)
                return await _json(writer, {"ok": True})
            if rest == ["send"] and method == "POST":
                await self.start_run(s, js)
                return await _json(writer, {"ok": True})
            if rest == ["permission"] and method == "POST":
                fut = s.perms.get(str(js.get("id")))
                if not fut or fut.done():
                    raise HttpError(404, "no such pending permission (the run may have ended)")
                fut.set_result(js.get("response") if js.get("response") in ("once", "always", "reject") else "reject")
                return await _json(writer, {"ok": True})
            if rest == ["diff"] and method == "GET":
                return await _json(writer, await asyncio.to_thread(git_diff_summary, s.workdir))
            if rest == ["snapshots"] and method == "GET":
                return await _json(writer, {"snapshots": list_snapshots(s.workdir)})
            if rest == ["undo"] and method == "POST":
                if s.busy:
                    raise HttpError(409, "cannot undo while a run is active")
                sid = str(js.get("snapshot") or (list_snapshots(s.workdir) or [""])[0])
                try:
                    res = await asyncio.to_thread(restore_snapshot, s.workdir, sid)
                except FileNotFoundError:
                    raise HttpError(404, "no such snapshot")
                audit(self.cfg, "undo", session=s.id, snapshot=sid, **res)
                s.emit("log", text=f"↩ restored snapshot {sid}: {res['restored']} file(s) restored, {res['removed']} removed")
                return await _json(writer, res)
            if rest == ["agents-md"] and method == "GET":
                return await _json(writer, {"text": self.agents_md(s)})
            if rest == ["agents-md"] and method == "PUT":
                (s.workdir / "AGENTS.md").write_text(str(js.get("text", "")), encoding="utf-8")
                audit(self.cfg, "agents_md_write", session=s.id)
                return await _json(writer, {"ok": True})
            if rest == ["result"] and method == "GET":
                s.load_events()
                res = next((e for e in reversed(s.events) if e["type"] == "result"), None)
                done = next((e for e in reversed(s.events) if e["type"] == "done"), None)
                return await _json(writer, {"busy": s.busy, "result": res["text"] if res else "", "done": done})
            if rest == ["messages"] and method == "GET":
                _, broker = await self.workspace(s.workdir)
                run_id = (q.get("run") or [""])[0]
                if not run_id:
                    s.load_events()
                    ev = next((e for e in reversed(s.events) if e.get("type") == "run_started"), None)
                    run_id = str(ev.get("run_id")) if ev else broker.latest_run_id()
                try:
                    after = int((q.get("after") or ["0"])[0] or 0)
                except ValueError:
                    after = 0
                return await _json(writer, {"run_id": run_id, "messages": broker.messages_since(run_id, after)})
            if rest == ["cancel"] and method == "POST":
                if s.task:
                    s.task.cancel()
                return await _json(writer, {"ok": True})
            if rest == ["events"] and method == "GET":
                return await self._sse(writer, s, int((q.get("after") or ["0"])[0] or 0))
            if rest == ["files"] and method == "GET":
                return await _json(writer, self.list_dir(s, (q.get("path") or [""])[0]))
            if rest == ["file"] and method == "GET":
                return await _json(writer, self.read(s, (q.get("path") or [""])[0]))
            if rest == ["upload"] and method == "POST":
                name = re.sub(r"[^\w.\- ]", "_", Path((q.get("name") or ["upload.bin"])[0]).name) or "upload.bin"
                dest = self._inside(s.workdir, (q.get("dir") or [""])[0]) / name
                n = 1
                while dest.exists():
                    dest = dest.with_name(f"{Path(name).stem}-{n}{Path(name).suffix}")
                    n += 1
                dest.write_bytes(body)
                return await _json(writer, {"path": str(dest.relative_to(s.workdir.resolve()))})
        raise HttpError(404, "unknown endpoint")

    async def _sse(self, writer: asyncio.StreamWriter, s: Session, after: int) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\n")
        s.load_events()
        q: asyncio.Queue = asyncio.Queue()
        s.listeners.add(q)
        last = after
        try:
            for ev in [e for e in s.events if e["id"] > after]:
                writer.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                last = ev["id"]
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    if ev["id"] <= last:
                        continue
                    last = ev["id"]
                    writer.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            s.listeners.discard(q)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            n = int(headers.get("content-length", "0") or 0)
            if n > MAX_BODY:
                raise HttpError(413, "body too large")
            body = await reader.readexactly(n) if n else b""
            u = urlparse(target)
            await self.route(method, u.path, parse_qs(u.query), headers, body, writer)
        except HttpError as exc:
            await _json(writer, {"error": exc.msg}, exc.status)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                await _json(writer, {"error": f"internal error: {exc!r}"}, 500)
            except ConnectionError:
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


async def _send(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str, csp: bool = False) -> None:
    extra = ("Content-Security-Policy: default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'\r\n") if csp else ""
    writer.write(
        f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
        f"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n{extra}"
        f"Connection: close\r\n\r\n".encode() + body
    )
    await writer.drain()


async def _json(writer: asyncio.StreamWriter, data: Any, status: int = 200) -> None:
    await _send(writer, status, json.dumps(data).encode(), "application/json")


def _open_app_window(url: str) -> subprocess.Popen | None:
    """Open a chromeless window with its own profile, so the process lives exactly as long as the window."""
    profile = state_path().parent / "app-profile"
    for exe in ("chromium", "google-chrome-stable", "google-chrome", "brave", "chrome", "microsoft-edge-stable"):
        path = shutil.which(exe)
        if path:
            return subprocess.Popen(
                [path, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--new-window"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
    return None


async def _watch_window(proc: subprocess.Popen, server: asyncio.AbstractServer) -> None:
    """Stop the server when the app window is closed (ignore a launcher that exits immediately)."""
    started = time.time()
    while proc.poll() is None:
        await asyncio.sleep(1)
    if time.time() - started > 3:
        print("app window closed - stopping OpenSwarm", flush=True)
        server.close()
        await server.wait_closed()
        raise asyncio.CancelledError


async def _serve(app: WebApp, host: str, url_open: bool, as_window: bool, ready: asyncio.Future | None = None) -> None:
    server = await asyncio.start_server(app.handle, host, app.port, limit=2**20)
    app.port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{app.port}/?token={app.token}"
    print(f"OpenSwarm Web {__version__}\n  {url}\n  (private to this machine; Ctrl+C to stop)", flush=True)
    if ready and not ready.done():
        ready.set_result(url)
    auto_task = asyncio.create_task(app.automation.run()) if (app.cfg.schedules or app.cfg.triggers) else None
    watcher: asyncio.Task | None = None
    if url_open:
        proc = _open_app_window(url) if as_window else None
        if proc:
            serving = asyncio.current_task()

            async def _guard() -> None:
                try:
                    await _watch_window(proc, server)
                except asyncio.CancelledError:
                    if serving:
                        serving.cancel()

            watcher = asyncio.create_task(_guard())
        else:
            webbrowser.open(url)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if watcher:
            watcher.cancel()
        if auto_task:
            auto_task.cancel()
        await app.shutdown()


def web_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="openswarm web", description="OpenSwarm as a local web app")
    p.add_argument("--port", type=int, default=8787, help="0 = pick a free port")
    p.add_argument("--dir", "--repo", dest="dir", type=Path, default=Path.cwd(), help="default working directory")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--app", action="store_true", help="open as a chromeless app window (Chromium/Chrome/Brave)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--no-memory", action="store_true")
    p.add_argument("--token", default=os.environ.get("OPENSWARM_TOKEN", ""), help="fixed access token for scripts (default: random; env OPENSWARM_TOKEN)")
    a = p.parse_args(argv)
    workdir = a.dir.resolve()
    try:
        cfg = load_config((a.config or find_config_path(workdir)).resolve())
    except ConfigError as exc:
        print(exc)
        return 2
    if not a.no_memory:
        apply_state(cfg, load_state())
    if cfg.opencode.discover_models:
        reconcile_models(cfg, asyncio.run(discover_models(cfg)))
    app = WebApp(cfg, workdir, a.port, a.token or secrets.token_urlsafe(18))
    try:
        asyncio.run(_serve(app, "127.0.0.1", not a.no_open, a.app))
    except OSError as exc:
        if a.port:
            print(f"port {a.port} unavailable ({exc}); picking a free one")
            app.port = 0
            asyncio.run(_serve(app, "127.0.0.1", not a.no_open, a.app))
        else:
            raise
    except KeyboardInterrupt:
        pass
    return 0
