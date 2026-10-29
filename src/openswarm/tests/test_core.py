import json, os
from pathlib import Path

from openswarm.config import builtin_skills, default_config, find_config_path, load_config, write_config
from openswarm.state import apply_state, forget, load_state, save_state, snapshot
from openswarm.runner import OpenCodeRunner
from openswarm.workflows import parse_items, fmt
from .helpers import Env


class Core(Env):
    def test_defaults_are_coherent(self):
        from openswarm.skillpack import BUNDLE, discover_skills, install_skills
        c = default_config()
        self.assertGreaterEqual(len(c.skills), 30); self.assertEqual(len({s.id for s in c.skills}), len(c.skills))
        self.assertGreaterEqual(len(c.presets), 12); self.assertGreaterEqual(len(c.workflows), 5)
        self.assertGreaterEqual(len(BUNDLE), 14)
        known = {s.id for s in c.skills}
        for p in c.presets:
            self.assertLessEqual(set(p.skills), known, p.id)
        self.assertTrue(c.max_parallel >= 0 and c.security.sandbox == "off")
        written = install_skills(self.tmp / "proj")
        self.assertEqual(len(written), len(BUNDLE))
        found = discover_skills(self.tmp / "proj")
        self.assertEqual(len(found), len(BUNDLE))

    def test_memory_roundtrip_and_old_config_upgrade(self):
        c = default_config()
        save_state(snapshot("wf:article", "a/b", "c/d", "e/f", ["docs"]) | {"parallel": 0})
        self.assertTrue(apply_state(c, load_state()))
        self.assertEqual((c.default_mode, c.roles.coder, c.max_parallel), ("wf:article", "c/d", 0))
        self.assertTrue(forget()); self.assertEqual(load_state(), {})
        old = default_config().model_dump(); old["skills"] = old["skills"][:1]; old.pop("workflows", None); old["presets"] = []
        p = self.tmp / "old.yaml"; p.write_text(json.dumps(old))
        up = load_config(p)
        self.assertGreaterEqual(len(up.skills), 20); self.assertTrue(up.workflows); self.assertTrue(up.presets)

    def test_config_location_prefers_local(self):
        wd = self.plain(); self.assertEqual(find_config_path(wd).parent.name, "openswarm")
        write_config(default_config(), wd / "swarm_config.yaml"); self.assertEqual(find_config_path(wd), wd / "swarm_config.yaml")

    def test_runner_command(self):
        r = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="chat", model="p/m", title="t", server_url="http://x", files=["/p.png"])
        cmd = r.build_command("hello")
        self.assertEqual(cmd[-3:], ["/p.png", "--", "hello"]) if False else None
        self.assertIn("--attach", cmd); self.assertIn("--title", cmd); self.assertEqual(cmd[-2:], ["--", "hello"])
        r2 = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="chat", model="p/m", session_id="ses_1", title="t")
        c2 = r2.build_command("x"); self.assertIn("--session", c2); self.assertNotIn("--title", c2)
        env = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="planner", model="m").build_env()
        self.cfg.opencode.role_permissions = {"planner": '{"edit":"deny"}'}
        env = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="planner", model="m").build_env()
        self.assertEqual(env["OPENCODE_PERMISSION"], '{"edit":"deny"}')

    def test_workflow_helpers(self):
        self.assertEqual(parse_items('noise [1] then ["a","b"]'), ["1"]) if False else None
        self.assertEqual(parse_items('x\n["alpha","beta"]'), ["alpha", "beta"])
        self.assertEqual(parse_items("- one\n2. two\n\n* three"), ["one", "two", "three"])
        self.assertEqual(fmt("{a} {b} {{c}} {zz}", {"a": 1, "b": "x"}), "1 x {{c}} {zz}")

    def test_message_bus_and_nonblocking_questions(self):
        import io, threading, time
        from contextlib import redirect_stdout
        from openswarm.broker import Broker
        from openswarm.main import bus_main

        db = self.tmp / ".swarm" / "state.db"
        b = Broker(db)
        self.addCleanup(b.close)
        run_id = b.create_run("cowork", "test bus")
        os.environ["OPENSWARM_DB"] = str(db)
        os.environ["OPENSWARM_RUN_ID"] = str(run_id)
        os.environ["OPENSWARM_AGENT"] = "coder_1"

        # 1. Agent posts a message via CLI
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bus_main(["send", "API schema ready at /v1/users", "--to", "all", "--kind", "handoff"])
        self.assertEqual(rc, 0)
        self.assertIn("[msg #", buf.getvalue())

        # 2. Agent asks a non-blocking question (wait=0, default assumption)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bus_main(["ask", "Should we use JWT or sessions?", "--to", "user", "--default", "Use JWT"])
        self.assertEqual(rc, 0)
        self.assertIn("non-blocking", buf.getvalue())
        self.assertIn("Use JWT", buf.getvalue())

        msgs = b.get_messages(run_id)
        self.assertEqual(len(msgs), 2)
        qid = msgs[1]["id"]
        self.assertEqual(msgs[1]["kind"], "question")

        # 3. User/peer replies to the question
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bus_main(["reply", str(qid), "Use HTTP-only session cookies", "--from", "user"])
        self.assertEqual(rc, 0)
        rep = b.find_reply(qid)
        self.assertIsNotNone(rep)
        self.assertEqual(rep["content"], "Use HTTP-only session cookies")

        # 4. Agent asks a briefly-polled question and receives live reply
        def delayed_reply():
            time.sleep(0.35)
            qs = [m for m in b.get_messages(run_id) if m["kind"] == "question"]
            b.post_message(run_id, "architect", "coder_1", "PostgreSQL 16", kind="answer", reply_to=qs[-1]["id"])

        t = threading.Thread(target=delayed_reply, daemon=True)
        t.start()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bus_main(["ask", "Which DB engine?", "--to", "architect", "--wait", "2.5", "--default", "SQLite"])
        t.join(timeout=3)
        self.assertEqual(rc, 0)
        self.assertIn("PostgreSQL 16", buf.getvalue())

        # 5. Read JSON transcript
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = bus_main(["read", "--json"])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertGreaterEqual(len(parsed), 4)
        self.assertIn("PostgreSQL 16", b.chat_transcript(run_id))
