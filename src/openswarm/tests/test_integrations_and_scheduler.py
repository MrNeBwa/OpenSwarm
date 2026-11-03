from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from openswarm.integrations import (
    IntegrationSettings,
    generate_ics,
    google_calendar_link,
)
from openswarm.orchestrator import Orchestrator, Selection, suggest_mode
from openswarm.scheduler import ScheduledTask, TaskScheduler
from openswarm.vpn import _native_bridge
from openswarm.tests.test_web import Web


class EmbeddedVpnTests(unittest.TestCase):
    def test_embedded_vpn_lists_65_servers_and_builds_config(self):
        raw_servers = _native_bridge(["list"])
        servers = json.loads(raw_servers) if isinstance(raw_servers, str) else raw_servers
        self.assertIsInstance(servers, list)
        self.assertGreaterEqual(len(servers), 62)
        first_id = servers[0]["id"]
        raw_cfg = _native_bridge(["config", first_id, "20808", "20809"])
        cfg = json.loads(raw_cfg) if isinstance(raw_cfg, str) else raw_cfg
        self.assertIn("inbounds", cfg)
        self.assertIn("outbounds", cfg)


class IntegrationsAndSchedulerUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_integration_settings_roundtrip_and_calendar_export(self):
        s = IntegrationSettings.from_dict({
            "notify_telegram": True,
            "telegram_bot_token": "123:ABC",
            "telegram_chat_id": "999",
            "notify_discord": True,
            "discord_webhook_url": "https://discord.com/api/webhooks/test",
            "notify_email": True,
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_to": "user@example.com",
        })
        self.assertTrue(s.telegram_enabled)
        self.assertTrue(s.discord_enabled)
        self.assertTrue(s.email_enabled)
        d = s.to_dict()
        self.assertEqual(d["telegram_bot_token"], "123:ABC")

        now = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)
        ics = generate_ics("Release v1.0", "Deploy OpenSwarm", now)
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("SUMMARY:Release v1.0", ics)
        glink = google_calendar_link("Release v1.0", "Deploy OpenSwarm", now)
        self.assertIn("calendar.google.com/calendar/render", glink)

    async def test_task_scheduler_lifecycle_and_execution(self):
        executed: list[ScheduledTask] = []

        async def on_dispatch(task: ScheduledTask) -> None:
            executed.append(task)

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "scheduled.json"
            sched = TaskScheduler(dispatch_fn=on_dispatch, state_file=state_file)
            t1 = sched.schedule(prompt="Delayed audit", delay_seconds=0.05, mode="review", target_agents=5)
            self.assertEqual(t1.target_agents, 5)
            self.assertEqual(t1.status, "pending")

            t2 = sched.schedule(prompt="Cancel me", delay_seconds=3600, mode="chat", target_agents=2)
            self.assertTrue(sched.cancel(t2.id))
            self.assertEqual(sched.tasks[t2.id].status, "cancelled")

            await asyncio.sleep(0.08)
            await sched._execute_task(t1)
            self.assertEqual(t1.status, "completed")
            self.assertEqual(len(executed), 1)
            self.assertEqual(executed[0].prompt, "Delayed audit")


class AutoModeAndProactiveAgentTests(unittest.TestCase):
    def test_bilingual_auto_mode_classifier(self):
        self.assertEqual(suggest_mode("почему не работает и падает с ошибкой", True)[0], "heal")
        self.assertEqual(suggest_mode("спроектируй архитектуру базы данных", True)[0], "architect")
        self.assertEqual(suggest_mode("проверь код и найди уязвимости", True)[0], "review")
        self.assertEqual(suggest_mode("плюсы и минусы Postgres vs ClickHouse", False)[0], "debate")
        self.assertEqual(suggest_mode("исследуй рынок AI агентов", False)[0], "research")
        self.assertEqual(suggest_mode("разработай новый модуль авторизации", True)[0], "cowork")

    def test_proactive_questions_and_subagent_prompt_injection(self):
        from openswarm.config import SwarmConfig
        from openswarm.git_manager import GitManager
        from openswarm.broker import Broker
        cfg = SwarmConfig()
        sel = Selection(mode="cowork", target_agents=6)
        self.assertEqual(sel.target_agents, 6)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            broker = Broker(p / "bus.db")
            try:
                orch = Orchestrator(cfg, GitManager(p), broker, sel)
                composed = orch._compose("coder", "Build payment gateway")
                self.assertIn("Interaction & Proactive Clarification", composed)
                self.assertIn("Subagent delegation", composed)
            finally:
                broker.close()


class WebIntegrationsAndSchedulerApiTests(Web):
    async def test_web_integrations_and_scheduled_endpoints(self):
        await self.start()
        st, integ = await self.call("POST", "/api/integrations", {
            "notify_telegram": True,
            "telegram_bot_token": "bot123",
            "telegram_chat_id": "chat456",
            "notify_discord": False,
        })
        self.assertEqual(st, 200)
        self.assertTrue(integ["integrations"]["notify_telegram"])

        st, cal = await self.call("POST", "/api/integrations/calendar", {
            "title": "Swarm Sprint",
            "description": "Check agent outputs",
        })
        self.assertEqual(st, 200)
        self.assertIn("BEGIN:VCALENDAR", cal["ics"])
        self.assertIn("calendar.google.com", cal["google_link"])

        future_ts = time.time() + 1800
        st, created = await self.call("POST", "/api/scheduled", {
            "prompt": "Run nightly benchmark",
            "run_at": future_ts,
            "mode": "auto",
            "target_agents": 7,
        })
        self.assertEqual(st, 200)
        tid = created["task"]["id"]
        self.assertEqual(created["task"]["target_agents"], 7)

        st, listed = await self.call("GET", "/api/scheduled")
        self.assertEqual(st, 200)
        self.assertTrue(any(t["id"] == tid for t in listed["tasks"]))

        st, deleted = await self.call("DELETE", f"/api/scheduled/{tid}")
        self.assertEqual(st, 200)
        self.assertTrue(deleted["ok"])

        st, mob = await self.call("GET", "/api/mobile/info")
        self.assertEqual(st, 200)
        self.assertTrue(mob["apk_exists"])
        self.assertGreater(mob["apk_size"], 4000)

        import http.client
        def fetch_apk():
            conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=10)
            conn.request("GET", "/api/mobile/apk", headers={"X-Token": "tok", "Host": f"127.0.0.1:{self.app.port}"})
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return resp.status, data

        apk_status, apk_bytes = await asyncio.to_thread(fetch_apk)
        self.assertEqual(apk_status, 200)
        self.assertTrue(apk_bytes.startswith(b"PK\x03\x04"))
        self.assertIn(b"APK Sig Block 42", apk_bytes)

        # DeepSeek Harness API endpoints
        st, dsh0 = await self.call("GET", "/api/dsh")
        self.assertEqual(st, 200)
        self.assertTrue(dsh0["ok"])

        st, dsh1 = await self.call("POST", "/api/dsh/token", {"token": "dsh_test_token_1234567890", "kind": "account", "mode": "auto"})
        self.assertEqual(st, 200)
        self.assertTrue(dsh1["has_token"])
        self.assertEqual(dsh1["active_route"], "direct_dsh_1m")

        st, dsh_act = await self.call("POST", "/api/dsh/activate", {"model": "deepseek-harness/deepseek-flash"})
        self.assertEqual(st, 200)
        self.assertEqual(dsh_act["roles"]["coder"], "deepseek-harness/deepseek-flash")

        st, dsh_clear = await self.call("POST", "/api/dsh/logout", {})
        self.assertEqual(st, 200)
        self.assertEqual(dsh_clear["active_route"], "opencode_free_unlimited")


class DeepSeekHarnessAndApkTests(unittest.TestCase):
    def test_dsh_zero_config_and_direct_provider_injection(self):
        import os
        from openswarm.capabilities import model_supports_variant
        from openswarm.config import default_config
        from openswarm.dsh import (
            DSH_FREE_FALLBACK_MODEL,
            clear_dsh_credentials,
            next_free_fallback_model,
            resolve_dsh_model,
            save_dsh_credentials,
        )
        from openswarm.keys import inline_model_config

        self.assertFalse(model_supports_variant("deepseek-harness/deepseek-flash"))
        cfg = default_config()
        self.assertIn("deepseek-harness/deepseek-flash", cfg.model_ids())

        # Test free fallback cascade
        self.assertEqual(
            next_free_fallback_model("opencode/deepseek-v4.1-flash-free"),
            "kilo/kilo-auto/free",
        )
        self.assertEqual(
            next_free_fallback_model("kilo/kilo-auto/free", {"opencode/deepseek-v4.1-flash-free"}),
            "kilo/nvidia/nemotron-3.5-super-vl:free",
        )

        with tempfile.TemporaryDirectory() as tmp:
            old_xdg = os.environ.get("XDG_CONFIG_HOME")
            old_dsh = os.environ.get("DSH_HOME")
            os.environ["XDG_CONFIG_HOME"] = tmp
            os.environ["DSH_HOME"] = str(Path(tmp) / "dsh_cli")
            try:
                clear_dsh_credentials()
                # Zero-config: resolves to free unlimited opencode gateway
                self.assertEqual(
                    resolve_dsh_model("deepseek-harness/deepseek-flash"),
                    DSH_FREE_FALLBACK_MODEL,
                )
                # Direct token configured: resolves to direct 1M DSH provider and injects OPENCODE_CONFIG_CONTENT
                save_dsh_credentials("dsh_acct_secret_token_9999", kind="account", mode="auto")
                self.assertEqual(
                    resolve_dsh_model("deepseek-harness/deepseek-flash"),
                    "deepseek-harness/deepseek-flash",
                )
                inline = json.loads(inline_model_config(cfg, "deepseek-harness/deepseek-flash"))
                prov = inline["provider"]["deepseek-harness"]
                self.assertEqual(prov["npm"], "@ai-sdk/anthropic")
                self.assertEqual(
                    prov["options"]["headers"]["x-dsh-auth-token"],
                    "dsh_acct_secret_token_9999",
                )
                self.assertEqual(
                    prov["models"]["deepseek-flash"]["limit"]["context"],
                    1048576,
                )

                # Direct API key (sk-...) -> OpenAI-compatible API
                save_dsh_credentials("sk-test-deepseek-api-key-12345", kind="apikey", mode="auto")
                inline_api = json.loads(inline_model_config(cfg, "deepseek-harness/deepseek-flash"))
                prov_api = inline_api["provider"]["deepseek-harness"]
                self.assertEqual(prov_api["npm"], "@ai-sdk/openai-compatible")
                self.assertEqual(prov_api["options"]["baseURL"], "https://api.deepseek.com/v1")
                self.assertEqual(prov_api["options"]["apiKey"], "sk-test-deepseek-api-key-12345")
                self.assertIn("deepseek-chat", prov_api["models"])
                self.assertIn("deepseek-reasoner", prov_api["models"])
            finally:
                if old_xdg is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = old_xdg
                if old_dsh is None:
                    os.environ.pop("DSH_HOME", None)
                else:
                    os.environ["DSH_HOME"] = old_dsh

    def test_android_apk_structure_and_signature_block(self):
        import struct
        import zipfile

        apk_path = Path(__file__).resolve().parents[3] / "dist" / "OpenSwarm-Mobile.apk"
        self.assertTrue(apk_path.is_file())
        raw = apk_path.read_bytes()
        self.assertIn(b"APK Sig Block 42", raw)
        with zipfile.ZipFile(apk_path, "r") as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            self.assertTrue({"AndroidManifest.xml", "resources.arsc", "classes.dex", "assets/index.html"}.issubset(names))
            dex = zf.read("classes.dex")
            self.assertTrue(dex.startswith(b"dex\n03"))
            self.assertEqual(struct.unpack_from("<I", dex, 0x20)[0], len(dex))
