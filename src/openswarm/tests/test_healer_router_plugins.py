"""Tests for Self-Healer, Smart Model Router, Priority Profiles, Plugins/MCP & Heal/Architect modes."""

from __future__ import annotations

import json
import os
from pathlib import Path

from openswarm.config import ModelSpec
from openswarm.healer import analyze_error, diagnose_and_heal, heal_git_workspace
from openswarm.plugins import (
    create_custom_skill,
    hook_enabled,
    list_mcp_servers,
    plugins_overview,
    set_hook,
    set_mcp_server,
    toggle_catalog_mcp,
)
from openswarm.router import ModelRouter, reset_router, score_model_capabilities

from .helpers import Env
from .test_web import Web


class HealerRouterPluginsTests(Env):
    def setUp(self) -> None:
        super().setUp()
        reset_router()

    def test_error_classifier(self) -> None:
        r1 = analyze_error("FreeUsageLimitError: Free usage limit reached. Please try again later.")
        self.assertEqual(r1["kind"], "rate_limit")

        r2 = analyze_error("ProviderModelNotFoundError: model opencode/broken-v1 not found")
        self.assertEqual(r2["kind"], "model_broken")

        r3 = analyze_error("Error: 401 Unauthorized: invalid API key")
        self.assertEqual(r3["kind"], "auth_error")

        r4 = analyze_error("fatal: Unable to create '/repo/.git/index.lock': File exists.")
        self.assertEqual(r4["kind"], "git_worktree")

        r5 = analyze_error("tunnel via edge-nl does not pass traffic")
        self.assertEqual(r5["kind"], "vpn_tunnel")

    def test_smart_model_router_and_priorities(self) -> None:
        self.cfg.models = [
            ModelSpec(id="opencode/deepseek-r1-free", label="DeepSeek R1", free=True),
            ModelSpec(id="opencode/qwen-2.5-coder-free", label="Qwen Coder", free=True),
            ModelSpec(id="opencode/gemini-2.5-flash-free", label="Gemini Flash", free=True),
            ModelSpec(id="anthropic/claude-opus-4", label="Claude Opus 4", free=False),
            ModelSpec(id="ollama/qwen2.5-coder:7b", label="Ollama Local", free=True),
        ]
        t = [100.0]
        router = ModelRouter(clock=lambda: t[0])

        # Free priority strictly avoids non-free models
        free_roles = router.recommend_roles(self.cfg, "free")
        self.assertNotEqual(free_roles["coder"], "anthropic/claude-opus-4")
        self.assertTrue(free_roles["coder"].startswith("opencode/") or free_roles["coder"].startswith("ollama/"))

        # Quality priority picks frontier model
        qual_roles = router.recommend_roles(self.cfg, "quality")
        self.assertEqual(qual_roles["planner"], "anthropic/claude-opus-4")

        # Fast priority picks flash model
        fast_roles = router.recommend_roles(self.cfg, "fast")
        self.assertEqual(fast_roles["coder"], "opencode/gemini-2.5-flash-free")

        # Local priority picks ollama model
        local_roles = router.recommend_roles(self.cfg, "local")
        self.assertEqual(local_roles["coder"], "ollama/qwen2.5-coder:7b")

        # Failover skips broken/cooling model
        router.record_failure("opencode/qwen-2.5-coder-free", "ModelNotFoundError", "model_broken")
        self.assertFalse(router.health_info("opencode/qwen-2.5-coder-free")["healthy"])
        fb = router.fallback_model(self.cfg, "coder", "opencode/qwen-2.5-coder-free", "free")
        self.assertIsNotNone(fb)
        self.assertNotEqual(fb, "opencode/qwen-2.5-coder-free")

        router.clear_cooldowns()
        self.assertTrue(router.health_info("opencode/qwen-2.5-coder-free")["healthy"])

    def test_plugins_mcp_hooks_and_custom_skills(self) -> None:
        wd = self.plain("proj")
        # Enable a catalog MCP server
        servers = toggle_catalog_mcp(wd, "sequential-thinking", True)
        self.assertIn("sequential-thinking", servers)
        self.assertTrue((wd / ".mcp.json").is_file())
        self.assertTrue((wd / "opencode.json").is_file())

        # Add a custom MCP server
        set_mcp_server(wd, "custom-db", ["uvx", "mcp-server-sqlite", "--db-path", "test.db"], {"DB": "1"}, True)
        servers = list_mcp_servers(wd)
        self.assertIn("custom-db", servers)

        # Toggle lifecycle hook
        self.assertTrue(hook_enabled("auto_self_heal"))
        set_hook("auto_self_heal", False)
        self.assertFalse(hook_enabled("auto_self_heal"))
        set_hook("auto_self_heal", True)

        # Create custom skill plugin
        slug = create_custom_skill("My Super Plugin", "Automates deployment checks", "# Steps\n1. Check build", project_dir=wd)
        self.assertEqual(slug, "my-super-plugin")
        self.assertTrue((wd / ".opencode" / "skill" / "my-super-plugin" / "SKILL.md").is_file())

        ov = plugins_overview(wd)
        self.assertTrue(any(m["id"] == "sequential-thinking" and m["enabled"] for m in ov["mcp"]))

    async def test_diagnose_and_heal_and_modes(self) -> None:
        r = self.repo()
        lock = r / ".git" / "index.lock"
        lock.write_text("stale")
        os.utime(lock, (1, 1))
        actions = heal_git_workspace(r)
        self.assertFalse(lock.exists())
        self.assertTrue(actions)

        self.cfg.roles.coder = "opencode/nonexistent-broken-model"
        rep = await diagnose_and_heal(self.cfg, r, auto_fix=True)
        self.assertTrue(rep["ok"])
        self.assertNotEqual(self.cfg.roles.coder, "opencode/nonexistent-broken-model")

        # Run heal mode end-to-end
        s1, logs1, res1, _ = await self.run_mode(r, "heal", prompt="diagnose and fix issue")
        self.assertTrue(s1.ok, logs1)
        self.assertEqual(res1[-1][0], "heal")

        # Run architect mode end-to-end
        s2, logs2, res2, _ = await self.run_mode(r, "architect", prompt="design distributed cache")
        self.assertTrue(s2.ok, logs2)
        self.assertEqual(res2[-1][0], "architect")


class WebEndpointsTests(Web):
    async def test_heal_priority_and_plugins_api(self) -> None:
        await self.start()
        st, h = await self.call("GET", "/api/heal")
        self.assertEqual(st, 200)
        self.assertIn("checks", h)

        st, h2 = await self.call("POST", "/api/heal", {"auto_fix": True})
        self.assertEqual(st, 200)
        self.assertTrue(h2["ok"])

        st, p = await self.call("POST", "/api/priority", {"priority": "fast", "auto_pick": True})
        self.assertEqual((st, p["priority"]), (200, "fast"))

        st, rt = await self.call("GET", "/api/router?role=coder&priority=fast")
        self.assertEqual(st, 200)
        self.assertTrue(len(rt["rankings"]) >= 1)

        st, pl = await self.call("GET", "/api/plugins")
        self.assertEqual(st, 200)
        self.assertIn("mcp", pl)

        st, pl2 = await self.call("POST", "/api/plugins/mcp", {"id": "memory", "enabled": True})
        self.assertEqual(st, 200)
        self.assertTrue(any(m["id"] == "memory" and m["enabled"] for m in pl2["mcp"]))
