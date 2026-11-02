import asyncio
import base64
import http.client
import json
import tempfile
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from openswarm.adaptive_prompt import (
    classify_user_intent,
    detect_workspace_tech_stack,
    get_adaptive_instructions,
)
from openswarm.config import SwarmConfig
from openswarm.orchestrator import Hooks, Orchestrator, Selection
from openswarm.web import WebApp
from .helpers import Env


class TestAdaptivePrompt(IsolatedAsyncioTestCase):
    def test_detect_workspace_tech_stack(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0", "next": "^14.0.0"}}))
            (p / "tsconfig.json").write_text("{}")
            (p / "Dockerfile").write_text("FROM node:20")
            stack = detect_workspace_tech_stack(p)
            self.assertIn("TypeScript", stack["languages"])
            self.assertIn("Next.js", stack["frameworks"])
            self.assertIn("React", stack["frameworks"])
            self.assertIn("Docker", stack["tools"])

    def test_classify_user_intent(self):
        self.assertEqual(classify_user_intent("Look at this layout", has_images=True), "vision")
        self.assertEqual(classify_user_intent("Fix NullPointerException in auth service"), "debug")
        self.assertEqual(classify_user_intent("Design microservice architecture with Kafka"), "architecture")
        self.assertEqual(classify_user_intent("Write README and API documentation"), "docs")
        self.assertEqual(classify_user_intent("Implement user registration route with bcrypt"), "code")

    def test_get_adaptive_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "requirements.txt").write_text("fastapi\nuvicorn\npydantic")
            instr = get_adaptive_instructions(p, "Find why /login returns 500", focus="debug")
            self.assertIn("FastAPI", instr)
            self.assertIn("Root Cause", instr)
            self.assertIn("@coder", instr)
            self.assertIn("<artifact", instr)


class TestAdaptiveWeb(Env):
    async def start(self, workdir=None):
        self.app = WebApp(self.cfg, workdir or self.plain("wd"), 0, "tok")
        srv = await asyncio.start_server(self.app.handle, "127.0.0.1", 0, limit=2**20)
        self.app.port = srv.sockets[0].getsockname()[1]

        async def stop():
            srv.close()
            await self.app.shutdown()

        self.addAsyncCleanup(stop)

    async def call(self, method, path, body=None, token="tok", raw_body=None, headers=None):
        def go():
            c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=15)
            h = {"X-Token": token, "Host": f"127.0.0.1:{self.app.port}"}
            if headers:
                h.update(headers)
            data = None
            if body is not None:
                data = json.dumps(body)
                h["Content-Type"] = "application/json"
            elif raw_body is not None:
                data = raw_body
            c.request(method, path, data, h)
            r = c.getresponse()
            raw = r.read()
            c.close()
            try:
                return r.status, json.loads(raw), r.getheaders()
            except ValueError:
                return r.status, raw, r.getheaders()

        return await asyncio.to_thread(go)

    async def test_image_upload_raw_and_clarify(self):
        await self.start()
        # 1. Create a session
        st, sess, _ = await self.call("POST", "/api/sessions", {"workdir": str(self.plain("wd_test")), "mode": "chat"})
        self.assertEqual(st, 200)
        sid = sess["id"]

        # 2. Upload an image
        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
        b64_png = "data:image/png;base64," + base64.b64encode(fake_png).decode()
        st, res, _ = await self.call("POST", f"/api/sessions/{sid}/upload-image", {"image": b64_png, "name": "diagram.png"})
        self.assertEqual(st, 200, res)
        self.assertTrue(res["ok"])
        rel_path = res["path"]
        self.assertTrue(rel_path.endswith(".png"))

        # 3. Read raw image
        st, raw_bytes, hdrs = await self.call("GET", f"/api/sessions/{sid}/raw?path={rel_path}")
        self.assertEqual(st, 200)
        self.assertEqual(raw_bytes, fake_png)
        content_type = dict(hdrs).get("Content-Type", "")
        self.assertIn("image/png", content_type)

        # 4. Test clarify endpoint
        s = self.app.sessions[sid]
        # Mock orchestrator on session
        class MockOrch:
            def __init__(self):
                self.clarified = {}
            def resolve_clarification(self, qid, answer):
                self.clarified[qid] = answer
                return True

        s.orch = MockOrch()
        st, c_res, _ = await self.call("POST", f"/api/sessions/{sid}/clarify", {"qid": "q-42", "answer": "Use PostgreSQL"})
        self.assertEqual(st, 200)
        self.assertTrue(c_res["ok"])
        self.assertEqual(s.orch.clarified["q-42"], "Use PostgreSQL")
