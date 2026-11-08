"""Per-app VPN manager, rotation deduplication, --standalone flag, and Web API."""
import json
import os
from unittest.mock import patch

from openswarm.capabilities import parse_help
from openswarm.config import VpnSettings
from openswarm.runner import OpenCodeRunner
from openswarm.vpn import VpnManager, reset_vpn
from .helpers import Env


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


FAKE_SERVERS = [
    {"id": "edge-de", "name": "DE", "country": "DE", "country_code": "DE", "city": "", "protocol": "wireguard", "premium": False, "edge": True},
    {"id": "edge-nl", "name": "NL", "country": "NL", "country_code": "NL", "city": "", "protocol": "wireguard", "premium": False, "edge": True},
    {"id": "edge-fi", "name": "FI", "country": "FI", "country_code": "FI", "city": "", "protocol": "wireguard", "premium": False, "edge": True},
]


class VpnUnit(Env):
    def setUp(self):
        super().setUp()
        reset_vpn()

    def tearDown(self):
        reset_vpn()
        super().tearDown()

    def _make_mgr(self, clk: Clock) -> VpnManager:
        def fake_bridge(args: list[str]) -> str:
            if args[0] == "list":
                return json.dumps(FAKE_SERVERS)
            return json.dumps({"inbounds": [], "outbounds": []})

        s = VpnSettings(order="sequential", cooldown_seconds=600, health_target="")
        mgr = VpnManager(s, clock=clk, bridge=fake_bridge)
        mgr._start_on = lambda sid: setattr(mgr, "current", sid)  # type: ignore[method-assign]
        mgr.running = lambda: bool(mgr.enabled and mgr.current)  # type: ignore[method-assign]
        return mgr

    def test_sequential_rotation_and_concurrent_dedup(self):
        clk = Clock()
        mgr = self._make_mgr(clk)
        self.assertTrue(mgr.set_enabled(True))
        self.assertEqual(mgr.current, "edge-de")

        # Worker 1 and Worker 2 both started on edge-de and hit a limit at the same instant:
        self.assertTrue(mgr.rotate("limit w1", limited=True, from_server="edge-de"))
        self.assertEqual(mgr.current, "edge-nl")
        self.assertEqual(mgr.rotations, 1)

        # Worker 2 finishes 0.1s later having used edge-de -> deduplicated (stays on edge-nl):
        clk.t += 0.1
        self.assertTrue(mgr.rotate("limit w2", limited=True, from_server="edge-de"))
        self.assertEqual(mgr.current, "edge-nl")
        self.assertEqual(mgr.rotations, 1)

        # 0.2s later, the retry on edge-nl ALSO hits a limit -> MUST rotate immediately to edge-fi!
        clk.t += 0.2
        self.assertTrue(mgr.rotate("limit retry on nl", limited=True, from_server="edge-nl"))
        self.assertEqual(mgr.current, "edge-fi")
        self.assertEqual(mgr.rotations, 2)

    def test_auto_switch_toggle_persistence(self):
        clk = Clock()
        mgr = self._make_mgr(clk)
        self.assertTrue(mgr.auto_switch_on_limits)
        mgr.set_auto_switch(False)
        self.assertFalse(mgr.auto_switch_on_limits)
        mgr2 = self._make_mgr(clk)
        self.assertFalse(mgr2.auto_switch_on_limits)

    def test_standalone_flag_in_runner_and_capabilities(self):
        os.environ["FAKE_CLI"] = "new"
        caps = parse_help("--standalone --server --model --auto")
        self.assertTrue(caps.can_standalone(self.cfg.opencode))
        r1 = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="lens", model="p/m", standalone=True)
        self.assertIn("--standalone", r1.build_command("hi"))
        r2 = OpenCodeRunner(self.cfg, self.tmp, agent_id="a", role="chat", model="p/m", server_url="http://127.0.0.1:9999", attach_flag="--server", standalone=True)
        cmd2 = r2.build_command("hi")
        self.assertIn("--server", cmd2)
        self.assertNotIn("--standalone", cmd2)

    def test_rotation_resets_stale_state_and_verifies_tunnel(self):
        clk = Clock()
        mgr = self._make_mgr(clk)
        self.assertTrue(mgr.set_enabled(True))
        # Add an expired cooldown and a future cooldown
        mgr.cooldown["expired-server"] = clk.t - 10
        mgr.cooldown["cooling-server"] = clk.t + 300
        mgr.last_error = "some stale error"

        self.assertTrue(mgr.rotate("manual test", limited=False))
        # Expired cooldown and stale error should be cleaned up:
        self.assertNotIn("expired-server", mgr.cooldown)
        self.assertIn("cooling-server", mgr.cooldown)
        self.assertEqual(mgr.last_error, "")

        # Verification check was run and recorded in history:
        st = mgr.status()
        self.assertIn("last_check", st)
        self.assertTrue(st["last_check"].get("ok"))
        self.assertIn("latency_ms", st["last_check"])

        latest = mgr.history[-1]
        self.assertIn("reset_actions", latest)
        self.assertIn("pruned_1_cooldowns", latest["reset_actions"])
        self.assertIn("cleared_error_flags", latest["reset_actions"])
        self.assertIn("verification", latest)

    def test_verify_method(self):
        clk = Clock()
        mgr = self._make_mgr(clk)
        mgr.current = "edge-de"
        res = mgr.verify(target="custom.target:443")
        self.assertTrue(res["ok"])
        self.assertEqual(res["server"], "edge-de")
        self.assertEqual(res["target"], "custom.target:443")
        self.assertIsNotNone(res["latency_ms"])

    def test_tier1_prioritization_and_restricted_exclusion(self):
        from openswarm.vpn import country_flag
        self.assertEqual(country_flag("DE"), "🇩🇪")
        self.assertEqual(country_flag("NL"), "🇳🇱")
        self.assertEqual(country_flag("US"), "🇺🇸")

        def fake_mixed_bridge(args: list[str]) -> str:
            if args[0] == "list":
                return json.dumps([
                    {"id": "edge-ru", "name": "RU", "country": "RU", "country_code": "RU", "city": "", "protocol": "wireguard", "edge": True},
                    {"id": "edge-cn", "name": "CN", "country": "CN", "country_code": "CN", "city": "", "protocol": "wireguard", "edge": True},
                    {"id": "edge-fr", "name": "FR", "country": "FR", "country_code": "FR", "city": "", "protocol": "wireguard", "edge": True},
                    {"id": "edge-de", "name": "DE", "country": "DE", "country_code": "DE", "city": "", "protocol": "wireguard", "edge": True},
                ])
            return json.dumps({})

        s = VpnSettings()
        mgr = VpnManager(s, bridge=fake_mixed_bridge)
        cands = mgr.candidates()
        # Restricted countries are excluded:
        self.assertNotIn("edge-ru", cands)
        self.assertNotIn("edge-cn", cands)
        # Tier-1 countries (DE, FR) are included:
        self.assertEqual(cands, ["edge-de", "edge-fr"])

        # status() includes flag and tier1 metadata:
        st = mgr.status(with_servers=True)
        self.assertEqual(len(st["servers"]), 4)
        de_srv = next(x for x in st["servers"] if x["id"] == "edge-de")
        self.assertEqual(de_srv["flag"], "🇩🇪")
        self.assertTrue(de_srv["tier1"])
        ru_srv = next(x for x in st["servers"] if x["id"] == "edge-ru")
        self.assertFalse(ru_srv["tier1"])

    async def test_web_api_vpn_endpoints(self):
        import asyncio
        import http.client
        from openswarm.web import WebApp

        app = WebApp(self.cfg, self.plain("wd_vpn"), 0, "tok")
        srv = await asyncio.start_server(app.handle, "127.0.0.1", 0, limit=2**20)
        app.port = srv.sockets[0].getsockname()[1]
        try:
            def call(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
                h = {"X-Token": "tok", "Host": f"127.0.0.1:{app.port}"}
                data = json.dumps(body).encode() if body is not None else None
                if data:
                    h["Content-Type"] = "application/json"
                c.request(method, path, data, h)
                r = c.getresponse()
                raw = r.read()
                c.close()
                return r.status, json.loads(raw)

            # 1. GET /api/vpn
            status, d = await asyncio.to_thread(call, "GET", "/api/vpn")
            self.assertEqual(status, 200)
            self.assertIn("settle_delay_seconds", d)
            self.assertIn("last_check", d)

            # 2. POST /api/vpn/verify
            status, v = await asyncio.to_thread(call, "POST", "/api/vpn/verify", {})
            self.assertEqual(status, 200)
            self.assertIn("ok", v)
            self.assertIn("check", v)
        finally:
            srv.close()
            await app.shutdown()
