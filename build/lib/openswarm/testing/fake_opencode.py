#!/usr/bin/env python3
"""A stand-in for the `opencode` CLI used by `openswarm selftest` and the test-suite.

Supports: `models`, `session list`, `run ...` (role-aware canned output) and `serve --port N` which hosts a fake
HTTP/SSE API (FAKE_API_STYLE=v1|v2|down). Not part of the product; it only exists so everything can be tested offline.
"""
import asyncio, base64, json, os, pathlib, sys, time

args = sys.argv[1:]
role = os.environ.get("OPENSWARM_ROLE", "")
LOG = pathlib.Path(os.environ.get("FAKE_LOG", "/tmp/fake_opencode.log"))


def log(s):
    with open(LOG, "a") as fh:
        fh.write(s + "\n")


# ---------------------------------------------------------------------------- fake API server
async def serve(port):
    style = os.environ.get("FAKE_API_STYLE", "v1")
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    subs, perms, aborted, store = [], {}, set(), {"msgs": []}
    log(f"serve cwd={os.getcwd()} port={port} style={style} pw={'yes' if password else 'no'}")

    def publish(ev):
        for q in subs:
            q.put_nowait(ev)

    async def script(sid, text):
        mid_u, mid_a = "msg_u1", "msg_a1"
        publish({"type": "message.updated", "properties": {"info": {"id": mid_u, "sessionID": sid, "role": "user"}}})
        publish({"type": "message.part.updated", "properties": {"part": {"id": "p_u", "sessionID": sid, "messageID": mid_u, "type": "text", "text": text}}})
        publish({"type": "message.updated", "properties": {"info": {"id": mid_a, "sessionID": sid, "role": "assistant"}}})
        reply = "Hello from fake opencode."
        if "PERM" in text:
            pid = "perm_1"
            fut = asyncio.get_event_loop().create_future()
            perms[pid] = fut
            kind = "permission.updated" if style == "v1" else "permission.asked"
            publish({"type": kind, "properties": {"id": pid, "sessionID": sid, "type": "bash", "title": "run: rm -rf /tmp/x", "metadata": {"command": "rm -rf /tmp/x"}}})
            decision = await fut
            reply = "ran it." if decision in ("once", "always") else "permission denied."
        publish({"type": "message.part.updated", "properties": {"part": {"id": "p_t", "sessionID": sid, "messageID": mid_a, "type": "tool", "tool": "bash", "state": {"status": "running", "input": {"command": "ls"}}}}})
        await asyncio.sleep(0.2)
        publish({"type": "message.part.updated", "properties": {"part": {"id": "p_t", "sessionID": sid, "messageID": mid_a, "type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "ls"}, "output": "a.txt"}}}})
        if sid in aborted:
            return
        if style == "v1":
            publish({"type": "message.part.updated", "properties": {"part": {"id": "p_a", "sessionID": sid, "messageID": mid_a, "type": "text", "text": "Hello "}}})
            await asyncio.sleep(0.1)
            publish({"type": "message.part.updated", "properties": {"part": {"id": "p_a", "sessionID": sid, "messageID": mid_a, "type": "text", "text": reply + "\nsecond line"}}})
        else:
            for chunk in (reply[:6], reply[6:], "\nsecond line"):
                publish({"type": "message.part.delta", "properties": {"sessionID": sid, "messageID": mid_a, "partID": "p_a", "field": "text", "delta": chunk}})
                await asyncio.sleep(0.05)
        store["msgs"] = [{"info": {"id": mid_a, "role": "assistant", "time": {"completed": 1}}, "parts": [{"type": "text", "text": reply + "\nsecond line"}]}]
        publish({"type": "message.updated", "properties": {"info": {"id": mid_a, "sessionID": sid, "role": "assistant", "cost": 0.0123, "tokens": {"input": 120, "output": 45, "reasoning": 0}, "time": {"completed": 1}}}})
        if style == "v1":
            publish({"type": "session.idle", "properties": {"sessionID": sid}})
        else:
            publish({"type": "session.status", "properties": {"sessionID": sid, "status": {"type": "idle"}}})

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            h = {k.lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
            body = await reader.readexactly(int(h.get("content-length", 0))) if h.get("content-length") else b""
            js = json.loads(body) if body else {}
            log(f"{method} {target} {json.dumps(js)[:160]}")

            async def send(code, data=None):
                raw = json.dumps(data).encode() if data is not None else b""
                writer.write(f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
                await writer.drain()

            if password:
                want = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
                if h.get("authorization") != want:
                    return await send(401, {"error": "unauthorized"})
            if style == "down":
                return await send(404, {"error": "no api"})
            path = target.split("?")[0]
            if method == "POST" and path == "/session":
                return await send(200, {"id": "ses_fakeapi1", "title": js.get("title", "")})
            if method == "GET" and path == "/event":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                q = asyncio.Queue(); subs.append(q)

                def chunk(s):
                    b = s.encode(); writer.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")

                chunk('data: {"type":"server.connected","properties":{}}\n\n'); await writer.drain()
                try:
                    while True:
                        ev = await q.get()
                        chunk("data: " + json.dumps(ev) + "\n\n"); await writer.drain()
                except Exception:
                    pass
                finally:
                    subs.remove(q)
                return
            parts = path.strip("/").split("/")
            if method == "POST" and len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
                text = next((p.get("text", "") for p in js.get("parts", []) if p.get("type") == "text"), "")
                asyncio.ensure_future(script(parts[1], text))
                return await send(204)
            if method == "POST" and len(parts) == 4 and parts[2] == "permissions":
                fut = perms.get(parts[3])
                if fut and not fut.done():
                    fut.set_result(js.get("response"))
                return await send(200, True)
            if method == "POST" and len(parts) == 3 and parts[2] == "abort":
                aborted.add(parts[1]); return await send(200, True)
            if method == "GET" and len(parts) == 3 and parts[2] == "message":
                return await send(200, store["msgs"])
            await send(404, {"error": "unknown"})
        except Exception as exc:  # noqa
            log(f"handler error {exc!r}")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(handle, "127.0.0.1", port)
    async with srv:
        await srv.serve_forever()


# ------------------------------------------------------------------------------- CLI behaviour
def main():
    if args[:1] == ["models"]:
        print("opencode/fake-free\nanthropic/claude"); return
    if "--help" in args:
        base = "opencode " + " ".join(a for a in args if a != "--help")
        if args[:1] == ["serve"]:
            print(base + "\n  --port  --hostname  --cors")
        else:
            print(base + "\n  --model -m  --continue -c  --session -s  --title  --attach  --file -f  --agent  --format")
        return
    if args[:1] == ["--version"]:
        print("0.0.0-fake"); return
    if args[:2] == ["session", "list"]:
        t = pathlib.Path(os.environ.get("FAKE_TITLE_FILE", "/tmp/fake_opencode.title"))
        print(json.dumps([{"id": "ses_FAKE123", "title": t.read_text() if t.exists() else ""}])); return
    if args[:1] == ["serve"]:
        asyncio.run(serve(int(args[args.index("--port") + 1]))); return
    msg, flags = args[-1], " ".join(args[1:-1])
    if "--title" in args:
        pathlib.Path(os.environ.get("FAKE_TITLE_FILE", "/tmp/fake_opencode.title")).write_text(args[args.index("--title") + 1])
    log(f"{role} :: {flags}")
    flaky = pathlib.Path(os.environ.get("FAKE_FLAKY", "/tmp/fake_opencode.flaky"))
    if role == "coder" and "FLAKY" in msg and not flaky.exists():
        flaky.write_text("1"); print("Error: 429 rate limit exceeded", file=sys.stderr); sys.exit(1)
    if role == "coder" and "ALWAYSFAIL" in msg:
        print("boom", file=sys.stderr); sys.exit(2)
    time.sleep(float(os.environ.get("FAKE_DELAY", "0.3")))
    js = lambda n: json.dumps([{"id": f"p{i}", "title": f"Part {i}", "prompt": f"cover part {i}"} for i in range(1, n + 1)])
    if role == "research_planner": print(js(4))
    elif role == "outliner": print(js(3))
    elif role in ("planner", "wfro_planner", "wfrw_planner"): print(os.environ.get("FAKE_PLAN") or "no json here")
    elif role == "architect": print("# Spec: create hello.txt")
    elif role == "researcher": print("findings for: " + msg.splitlines()[-1][:40])
    elif role == "drafter": print("draft text of: " + msg.splitlines()[-1][:40])
    elif role == "lens": print("- medium src/x.py:3 issue")
    elif role in ("synthesizer", "editor"): print("FINAL REPORT\n" + str(msg.count("###")) + " items merged")
    elif role == "reviewer":
        m = pathlib.Path(os.environ.get("FAKE_REVIEW_MARK", "/tmp/fake_opencode.rejected"))
        if "REJECTONCE" in msg and not m.exists():
            m.write_text("1"); print("needs tests\nVERDICT: CHANGES_REQUESTED")
        else: print("looks good\nVERDICT: APPROVED")
    elif role == "judge":
        print("Candidate 2 is cleanest.\nWINNER: 2")
    elif role == "chat": print("chat reply ok; flags: " + flags)
    elif role == "coder":
        n = os.environ.get("OPENSWARM_AGENT", "x")[-6:]
        pathlib.Path(f"out_{n}.txt").write_text("fixed" if ("FIXME" in msg or "fails the project" in msg) else "x")
        print("coded")
    else: print("ok")


if __name__ == "__main__":
    main()
