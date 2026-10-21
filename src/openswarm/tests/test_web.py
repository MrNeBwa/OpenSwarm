import asyncio, http.client, json, os, re, subprocess, time
from pathlib import Path

from openswarm.apiclient import Http
from openswarm.config import ScheduleSpec, TriggerSpec
from openswarm.web import WebApp
from .helpers import Env

UI = Path(__file__).resolve().parent.parent / "web_ui.html"


class Web(Env):
    async def start(self, workdir=None):
        self.app = WebApp(self.cfg, workdir or self.plain("wd"), 0, "tok")
        srv = await asyncio.start_server(self.app.handle, "127.0.0.1", 0, limit=2**20)
        self.app.port = srv.sockets[0].getsockname()[1]
        async def stop():
            srv.close(); await self.app.shutdown()
        self.addAsyncCleanup(stop)

    async def call(self, method, path, body=None, token="tok", host=None):
        def go():
            c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=15)
            h = {"X-Token": token, "Host": host or f"127.0.0.1:{self.app.port}"}
            data = None
            if body is not None:
                data, h["Content-Type"] = json.dumps(body), "application/json"
            c.request(method, path, data, h)
            r = c.getresponse(); raw = r.read(); c.close()
            try: return r.status, json.loads(raw)
            except ValueError: return r.status, raw.decode()
        return await asyncio.to_thread(go)

    async def session(self, mode="chat", wd=None):
        st, s = await self.call("POST", "/api/sessions", {"workdir": str(wd or self.plain(f"w{time.time_ns()}")), "mode": mode})
        self.assertEqual(st, 200, s); return s["id"]

    async def events(self, sid):
        return (await self.call("GET", f"/api/sessions/{sid}"))[1]["events"]

    async def wait_for(self, sid, pred, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            evs = await self.events(sid)
            hit = next((e for e in evs if pred(e)), None)
            if hit: return hit
            await asyncio.sleep(0.15)
        self.fail(f"timeout; events: {[e['type'] for e in await self.events(sid)]}")

    BODY = {"models": {"planner": "m", "coder": "m", "reviewer": "m"}, "skills": [], "parallel": 4}


class WebTests(Web):
    async def test_security_and_state(self):
        await self.start()
        st, d = await self.call("GET", "/api/state")
        ids = [m["id"] for m in d["modes"]]
        self.assertTrue({"chat", "auto", "best", "wf:article"} <= set(ids)); self.assertEqual(len(d["skills"]) >= 20, True)
        self.assertEqual((await self.call("GET", "/api/state", token="bad"))[0], 403)
        self.assertEqual((await self.call("GET", "/api/state", host="evil.com"))[0], 403)

    async def test_permission_prompt_flow_allow_and_deny(self):
        await self.start()
        for decision, expect in (("once", "ran it."), ("reject", "permission denied.")):
            sid = await self.session()
            await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "go PERM", "mode": "chat"})
            perm = await self.wait_for(sid, lambda e: e["type"] == "permission")
            self.assertEqual(perm["req"]["tool"], "bash")
            st, _ = await self.call("POST", f"/api/sessions/{sid}/permission", {"id": perm["req"]["id"], "response": decision})
            self.assertEqual(st, 200)
            res = await self.wait_for(sid, lambda e: e["type"] == "result")
            self.assertIn(expect, res["text"])
            done = await self.wait_for(sid, lambda e: e["type"] == "done")
            self.assertTrue(done["ok"]); self.assertEqual(done["usage"]["tokens"]["input"], 120)
            kinds = {e["type"] for e in await self.events(sid)}
            self.assertTrue({"tool", "usage", "permission_resolved"} <= kinds)
            self.assertEqual((await self.call("GET", f"/api/sessions/{sid}"))[1]["oc_session"], "ses_fakeapi1")

    async def test_sse_stream_and_result_endpoint(self):
        await self.start(); sid = await self.session()
        await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "hi", "mode": "chat"})
        seen = []
        async for ev in Http(f"http://127.0.0.1:{self.app.port}").sse(f"/api/sessions/{sid}/events?token=tok"):
            seen.append(ev["type"])
            if ev["type"] == "done": break
        self.assertIn("user", seen); self.assertIn("result", seen)
        st, r = await self.call("GET", f"/api/sessions/{sid}/result")
        self.assertIn("Hello from fake opencode", r["result"]); self.assertFalse(r["busy"])

    async def test_busy_and_cancel(self):
        await self.start(); sid = await self.session()
        os.environ["FAKE_DELAY"] = "2"; os.environ["FAKE_API_STYLE"] = "down"  # slow `run` path
        await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "slow", "mode": "chat"})
        await asyncio.sleep(0.8)
        self.assertEqual((await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "again"}))[0], 409)
        await self.call("POST", f"/api/sessions/{sid}/cancel")
        done = await self.wait_for(sid, lambda e: e["type"] == "done"); self.assertFalse(done["ok"])

    async def test_undo_snapshot_and_git_diff(self):
        await self.start(); d = self.plain("nogit"); (d / "keep.txt").write_text("v1")
        os.environ["FAKE_PLAN"] = '[{"id":"a","title":"A","prompt":"do a"}]'
        sid = await self.session(wd=d)
        await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "x", "mode": "storm"})
        done = await self.wait_for(sid, lambda e: e["type"] == "done"); self.assertTrue(done["snapshot"])
        (d / "keep.txt").write_text("CHANGED")
        st, r = await self.call("POST", f"/api/sessions/{sid}/undo", {"snapshot": done["snapshot"]})
        self.assertEqual(st, 200); self.assertEqual((d / "keep.txt").read_text(), "v1")
        self.assertEqual((await self.call("GET", f"/api/sessions/{sid}/diff"))[1], {})  # not a git repo
        r = self.repo(); sid2 = await self.session(wd=r); (r / "new.txt").write_text("hello"); (r / "README").write_text("changed")
        diff = (await self.call("GET", f"/api/sessions/{sid2}/diff"))[1]
        self.assertIn("new.txt", diff["stat"]); self.assertIn("+changed", diff["patch"])
        audit = (await self.call("GET", "/api/audit?n=50"))[1]["lines"]
        self.assertTrue(any(l["event"] == "undo" for l in audit))

    async def test_messages_endpoint_does_not_consume(self):
        await self.start(); d = self.repo(); sid = await self.session(wd=d)
        await self.call("POST", f"/api/sessions/{sid}/send", {**self.BODY, "prompt": "hi", "mode": "chat"})
        started = await self.wait_for(sid, lambda e: e["type"] == "run_started")
        run = started["run_id"]; self.assertTrue(run)
        _, broker = await self.app.workspace(d)
        mid = broker.send_message(run, "a", "b", "hello bus", "question")
        st, got = await self.call("GET", f"/api/sessions/{sid}/messages?run={run}")
        self.assertEqual(st, 200)
        self.assertEqual([m["body"] for m in got["messages"]], ["hello bus"])
        st, after = await self.call("GET", f"/api/sessions/{sid}/messages?run={run}&after={mid}")
        self.assertEqual((st, after["messages"]), (200, []))
        self.assertEqual(len(broker.inbox(run, "b")), 1)  # viewing must not acknowledge
        # No run id -> falls back to the session's last run_started event.
        st, auto = await self.call("GET", f"/api/sessions/{sid}/messages")
        self.assertEqual((st, auto["run_id"]), (200, run))

    async def test_agents_md_traversal_and_allowed_roots(self):
        await self.start(); d = self.plain("proj"); sid = await self.session(wd=d)
        self.assertEqual((await self.call("PUT", f"/api/sessions/{sid}/agents-md", {"text": "# rules\nbe nice"}))[0], 200)
        self.assertEqual((d / "AGENTS.md").read_text(), "# rules\nbe nice")
        self.assertEqual((await self.call("GET", f"/api/sessions/{sid}/agents-md"))[1]["text"], "# rules\nbe nice")
        self.assertEqual((await self.call("GET", f"/api/sessions/{sid}/file?path=../../etc/passwd"))[0], 403)
        self.cfg.security.allowed_roots = [str(d)]
        self.assertEqual((await self.call("POST", "/api/sessions", {"workdir": "/etc"}))[0], 403)

    async def test_automation_schedule_and_trigger(self):
        await self.start(); wd = self.plain("auto"); watch = self.plain("inbox")
        self.cfg.schedules = [ScheduleSpec(id="nightly", every="1s", prompt="summarise", workdir=str(wd))]
        self.cfg.triggers = [TriggerSpec(id="inbox", watch=str(watch), glob="*.txt", prompt="process {file}", workdir=None) ] if False else [
            TriggerSpec(id="inbox", watch=str(watch), glob="*.txt", prompt="process {file}", debounce_s=0)]
        (watch / "old.txt").write_text("existed before start")
        await self.app.automation.tick()            # baseline: nothing fires
        self.assertEqual(len(self.app.sessions), 0)
        (watch / "new.txt").write_text("fresh"); await asyncio.sleep(1.2)
        await self.app.automation.tick()
        titles = sorted(s.title for s in self.app.sessions.values())
        self.assertEqual(titles, ["⏰ nightly", "👁 inbox"], titles)
        trig = next(s for s in self.app.sessions.values() if s.title.startswith("👁"))
        await self.wait_for(trig.id, lambda e: e["type"] == "user" and "new.txt" in e["text"] and "old.txt" not in e["text"])


class UiStatic(Env):
    def test_every_referenced_element_exists_and_js_parses(self):
        html = UI.read_text(encoding="utf-8")
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        ids = set(re.findall(r'id="([\w-]+)"', html)) | set(re.findall(r"id=\\?\"([\w-]+)\\?\"", js))
        used = set(re.findall(r'\$\("([\w-]+)"\)', js))
        self.assertEqual(sorted(used - ids), [], "UI code references missing elements")
        if subprocess.run(["which", "node"], capture_output=True).returncode == 0:
            p = Path(os.environ["XDG_STATE_HOME"]) / "ui.js"; p.write_text(js)
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
