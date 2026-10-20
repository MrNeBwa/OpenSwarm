import asyncio, os, shutil, stat, subprocess, tempfile, time
from pathlib import Path

from openswarm.orchestrator import Hooks, suggest_mode
from openswarm.safety import SandboxError, bwrap_command, create_snapshot, list_snapshots, restore_snapshot, path_allowed
from openswarm.servers import ServerPool
from .helpers import Env


class Modes(Env):
    async def test_fanout_modes_and_burst(self):
        d = self.plain()
        for mode, n in (("research", 4), ("write", 3), ("review", 4)):
            s, logs, results, _ = await self.run_mode(d, mode)
            self.assertTrue(s.ok, (mode, logs)); self.assertIn(f"{n} items merged", results[-1][1])
        os.environ["FAKE_DELAY"] = "0.6"
        self.cfg.max_parallel = 0
        t = time.time(); await self.run_mode(d, "research"); burst = time.time() - t
        self.cfg.max_parallel = 1
        t = time.time(); await self.run_mode(d, "research"); serial = time.time() - t
        self.assertLess(burst, serial)

    async def test_budget_stops_run(self):
        self.cfg.max_agents_per_run = 2
        s, logs, _, _ = await self.run_mode(self.plain(), "research")
        self.assertIn("budget", s.failed)

    async def test_workflows(self):
        d = self.plain()
        s, logs, results, _ = await self.run_mode(d, "wf:article")
        self.assertTrue(s.ok, logs); self.assertEqual(results[-1][0], "workflow")
        os.environ["FAKE_PLAN"] = '["angle one","angle two","angle three"]'
        s, logs, results, _ = await self.run_mode(d, "wf:deep_dive")
        self.assertTrue(s.ok, logs); self.assertTrue(any("3 item(s) in parallel" in l for l in logs))

    async def test_auto_router(self):
        self.assertEqual(suggest_mode("please review my code", True)[0], "review")
        self.assertEqual(suggest_mode("напиши статью про Arch", False)[0], "write")
        self.assertEqual(suggest_mode("research the market for X", False)[0], "research")
        self.assertEqual(suggest_mode("fix typo", True)[0], "chat")
        self.assertEqual(suggest_mode("1. add api\n2. add tests\n3. update docs", True)[0], "storm")
        s, logs, _, _ = await self.run_mode(self.plain(), "auto", prompt="research the history of X")
        self.assertEqual(s.mode, "research"); self.assertTrue(any("Auto mode" in l for l in logs))


class GitModes(Env):
    async def test_storm_verify_fix_loop(self):
        r = self.repo()
        os.environ["FAKE_PLAN"] = '[{"id":"a","title":"A","prompt":"do a"}]'
        self.cfg.verify_command = "grep -q fixed out_*.txt"
        s, logs, _, _ = await self.run_mode(r, "storm")
        self.assertTrue(s.ok, logs); self.assertTrue(any("asking the coder to fix" in l for l in logs))

    async def test_storm_rate_limit_retry(self):
        r = self.repo(); self.cfg.opencode.retries = 2
        s, logs, _, _ = await self.run_mode(r, "storm", prompt="FLAKY please")
        self.assertTrue(s.ok, logs); self.assertTrue(any("rate limited" in l for l in logs))

    async def test_best_of_n(self):
        r = self.repo(); self.cfg.best_of_n = 3
        s, logs, results, _ = await self.run_mode(r, "best")
        self.assertTrue(s.ok, logs); self.assertTrue(any("Judge picked candidate 2" in l for l in logs))
        branches = subprocess.run(["git", "branch"], cwd=r, capture_output=True, text=True).stdout
        self.assertNotIn("swarm/", branches)  # every candidate branch cleaned up
        self.assertEqual(results[-1][0], "best")

    async def test_best_needs_git(self):
        s, logs, _, _ = await self.run_mode(self.plain(), "best")
        self.assertTrue(any("needs a git repository" in l for l in logs))


class Safety(Env):
    async def test_snapshot_undo_outside_git(self):
        d = self.plain(); (d / "keep.txt").write_text("v1")
        os.environ["FAKE_PLAN"] = '[{"id":"a","title":"A","prompt":"do a"}]'
        s, logs, _, ev = await self.run_mode(d, "storm")
        self.assertTrue(s.snapshot, logs); self.assertTrue(any(f.name.startswith("out_") for f in d.iterdir()))
        (d / "keep.txt").write_text("CHANGED")
        res = restore_snapshot(d, s.snapshot)
        self.assertEqual((d / "keep.txt").read_text(), "v1")
        self.assertFalse(any(f.name.startswith("out_") for f in d.iterdir()))
        self.assertGreaterEqual(res["removed"], 1)

    def test_snapshot_size_limit_and_traversal(self):
        d = self.plain(); (d / "big").write_bytes(b"0" * 2_000_000)
        self.assertIsNone(create_snapshot(d, "x", 1, 5))
        self.assertEqual(list_snapshots(d), [])

    def test_allowed_roots(self):
        d = self.plain("inside")
        self.cfg.security.allowed_roots = [str(self.tmp)]
        self.assertTrue(path_allowed(self.cfg, d)); self.assertFalse(path_allowed(self.cfg, Path("/etc")))

    def test_bwrap_command_shape_and_missing(self):
        d = self.plain()
        with self.assertRaises(SandboxError):
            os.environ["PATH"] = "/nonexistent"; bwrap_command(["x"], d, self.cfg.security)
        bindir = self.tmp / "bin"; bindir.mkdir(); b = bindir / "bwrap"; b.write_text("#!/bin/sh\nexit 0\n"); b.chmod(0o755)
        os.environ["PATH"] = f"{bindir}:{self._env['PATH']}"
        (self.tmp / "home" / ".ssh").mkdir(); self.cfg.security.sandbox_network = False
        cmd = bwrap_command(["opencode", "run"], d, self.cfg.security)
        self.assertEqual(cmd[0], str(b)); self.assertEqual(cmd[-2:], ["opencode", "run"])
        self.assertIn("--unshare-net", cmd); self.assertIn("--die-with-parent", cmd)
        i = cmd.index("--bind"); self.assertIn(str(d.resolve()), cmd)
        self.assertLess(cmd.index("--tmpfs"), cmd.index("--bind"))  # /tmp tmpfs first so a workdir under /tmp stays visible
        self.assertIn(str(self.tmp / "home" / ".ssh"), cmd)  # secrets hidden
        self.assertIn("--ro-bind", cmd)

    async def test_sandbox_missing_fails_closed(self):
        os.environ["PATH"] = "/nonexistent"
        self.cfg.security.sandbox = "bwrap"
        s, logs, _, _ = await self.run_mode(self.plain(), "research")
        self.assertFalse(s.ok)


class ApiChat(Env):
    async def _pool(self):
        pool = ServerPool(self.cfg); self.addAsyncCleanup(pool.close); return pool

    async def test_api_chat_permissions_tools_usage_and_resume(self):
        d = self.plain(); pool = await self._pool()
        asked = []

        async def ask(agent, req):
            asked.append(req["title"]); return "once"

        s, logs, results, ev = await self.run_mode(d, "chat", prompt="hello PERM", pool=pool, hooks=Hooks(ask_permission=ask),
                                                   chat_id="c1", chat_title="C")
        self.assertTrue(s.ok, logs); self.assertEqual(s.session_id, "ses_fakeapi1"); self.assertIn("ran it.", results[-1][1])
        self.assertEqual(asked, ["run: rm -rf /tmp/x"])
        kinds = [k for _, k, _ in ev]
        self.assertIn("tool", kinds); self.assertIn("permission", kinds); self.assertIn("usage", kinds)
        self.assertAlmostEqual(s.usage["cost"], 0.0123); self.assertEqual(s.usage["tokens"]["input"], 120)
        s2, _, r2, _ = await self.run_mode(d, "chat", prompt="again", pool=pool, chat_id="c1", session_id="ses_fakeapi1")
        self.assertTrue(s2.ok); log = (self.tmp / "fake.log").read_text()
        self.assertIn("/session/ses_fakeapi1/prompt_async", log)
        self.assertEqual(log.count("POST /session {"), 1)  # session created once, then resumed

    async def test_permission_default_reject_without_ui(self):
        s, logs, results, _ = await self.run_mode(self.plain(), "chat", prompt="x PERM", pool=await self._pool())
        self.assertIn("permission denied", results[-1][1]); self.assertTrue(any("no UI to ask" in l for l in logs))

    async def test_server_is_password_protected(self):
        d = self.plain(); pool = await self._pool()
        await self.run_mode(d, "chat", pool=pool)
        self.assertIn("pw=yes", (self.tmp / "fake.log").read_text())

    async def test_v2_event_style(self):
        os.environ["FAKE_API_STYLE"] = "v2"
        s, logs, results, _ = await self.run_mode(self.plain(), "chat", pool=await self._pool())
        self.assertTrue(s.ok, logs); self.assertIn("Hello from fake opencode.", results[-1][1])

    async def test_api_down_falls_back_to_run(self):
        os.environ["FAKE_API_STYLE"] = "down"; pool = await self._pool()
        s, logs, results, _ = await self.run_mode(self.plain(), "chat", pool=pool, chat_id="c9")
        self.assertTrue(s.ok, logs); self.assertTrue(pool.api_bad)
        self.assertTrue(any("using `opencode run` instead" in l for l in logs)); self.assertIn("chat reply ok", results[-1][1])
