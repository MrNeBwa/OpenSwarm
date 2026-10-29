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
