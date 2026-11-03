import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openswarm.optimizer import (
    compress_text,
    prune_history_context,
    make_cache_key,
    check_cache,
    store_cache,
    optimize_prompt_for_model,
    get_optimizer_stats,
    reset_optimizer_stats,
    skeletonize_code,
    build_repo_skeleton,
    update_optimizer_settings,
    get_token_saving_directive,
)
from openswarm.telegram_user import TelegramUserManager, get_telegram_user_manager
from openswarm.integrations import IntegrationSettings, send_telegram
from openswarm.desktop_agent import DesktopAgentManager


class TestOptimizer(unittest.TestCase):
    def setUp(self):
        reset_optimizer_stats()

    def test_compress_text_collapsing_lines(self):
        raw = "Line 1\n\n\n\n\nLine 2\r\n\r\nLine 3"
        res = compress_text(raw)
        self.assertNotIn("\n\n\n", res)
        self.assertIn("Line 1\n\nLine 2\n\nLine 3", res)

    def test_compress_huge_log(self):
        huge_log = "Terminal output:\n" + "\n".join(f"build log item {i}" for i in range(250))
        res = compress_text(huge_log, max_head=20, max_tail=20)
        self.assertIn("строк лога/вывода скрыто для экономии токенов", res)
        self.assertLess(len(res), len(huge_log))

    def test_prune_history_context(self):
        turns = [
            {"role": "user", "content": "First user message about building architecture."},
            {"role": "assistant", "content": "First answer explaining system design components."},
            {"role": "user", "content": "Second message asking about databases."},
            {"role": "assistant", "content": "Second answer comparing PostgreSQL and SQLite."},
            {"role": "user", "content": "Third message about migrations."},
            {"role": "assistant", "content": "Third answer detailing EF Core migrations."},
            {"role": "user", "content": "Recent prompt for review."},
        ]
        pruned = prune_history_context(turns, max_verbatim_turns=2)
        self.assertIn("Краткий контекст предыдущих сообщений:", pruned)
        self.assertIn("Recent prompt for review.", pruned)

    def test_cache_and_stats(self):
        key = make_cache_key("test prompt", "coder", "opencode/beam-free")
        self.assertIsNone(check_cache(key))
        store_cache(key, "cached result text")
        self.assertEqual(check_cache(key), "cached result text")
        
        stats = get_optimizer_stats()
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["cache_size"], 1)
        self.assertIn("saved_tokens_est", stats)
        self.assertIn("savings_pct", stats)

    def test_semantic_normalized_cache(self):
        k1 = make_cache_key("Please   explain   how  KV-cache works!", "coder", "m1")
        k2 = make_cache_key("please explain how kv-cache works", "coder", "m1")
        self.assertEqual(k1, k2)

    def test_optimize_prompt_for_model(self):
        p = "System instruction\n\n\n\nUser query\n" + "\n".join(f"diff line {i}" for i in range(120))
        opt = optimize_prompt_for_model(p, role="coder")
        self.assertIn("User query", opt)
        st = get_optimizer_stats()
        self.assertGreater(st["saved_chars"], 0)

    def test_skeletonize_code_and_repo_skeleton(self):
        py_src = (
            "import os\n"
            "class Service:\n"
            "    '''Docstring here'''\n"
            "    def compute(self, x: int) -> int:\n"
            "        a = x + 1\n"
            "        b = a * 2\n"
            "        return b\n"
        )
        skel = skeletonize_code(py_src, "service.py")
        self.assertIn("class Service:", skel)
        self.assertIn("def compute(self, x: int) -> int: ...", skel)
        self.assertNotIn("b = a * 2", skel)

        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "app.py"
            p.write_text(py_src, encoding="utf-8")
            repo_map = build_repo_skeleton(tmpdir)
            self.assertIn("app.py", repo_map)
            self.assertIn("class Service:", repo_map)

    def test_optimizer_settings_and_directive(self):
        s = update_optimizer_settings({"mode": "ultra", "diff_only_output": True})
        self.assertEqual(s["mode"], "ultra")
        directive = get_token_saving_directive("coder")
        self.assertIn("ECONOMY", directive)
        update_optimizer_settings({"mode": "aggressive"})


class TestTelegramUserManager(unittest.IsolatedAsyncioTestCase):
    async def test_status_disconnected_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": tmpdir}):
                mgr = TelegramUserManager()
                st = await mgr.get_status()
                self.assertIn("installed", st)
                self.assertIn("active", st)
                self.assertFalse(st["active"])
                self.assertEqual(st["status"], "disconnected")
                self.assertTrue(st["require_confirmation"])

    async def test_send_message_blocks_without_confirmation(self):
        mgr = TelegramUserManager()
        res = await mgr.send_message("12345", "Hello world", confirmed=False, chat_title="Test Chat")
        self.assertFalse(res["ok"])
        self.assertFalse(res["sent"])
        self.assertTrue(res["requires_confirmation"])
        self.assertEqual(len(mgr.list_drafts()), 1)
        draft_id = res["draft"]["id"]
        rej = mgr.reject_draft(draft_id)
        self.assertTrue(rej)
        self.assertEqual(len(mgr.list_drafts()), 0)

    async def test_integration_telegram_blocks_unconfirmed(self):
        cfg = IntegrationSettings(
            telegram_enabled=True,
            telegram_require_confirmation=True,
            telegram_bot_token="fake-token",
            telegram_chat_id="123",
        )
        # Must NOT send if confirmed=False
        self.assertFalse(await send_telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, "Test alert", confirmed=False))

    async def test_suggest_reply_progressive_context_expansion_doubling(self):
        mgr = TelegramUserManager()
        all_msgs = [
            {"id": i, "sender_id": 100, "sender": "Алексей", "out": (i % 2 == 0), "text": f"Сообщение номер {i} про разработку проекта и дедлайн в пятницу", "reply_to_msg_id": None, "date": "2026-11-03T12:00:00"}
            for i in range(1, 25)
        ]
        requested_limits = []

        async def fake_get_messages(chat_id, limit=15):
            requested_limits.append(limit)
            return all_msgs[-limit:]

        mgr.get_messages = fake_get_messages

        call_count = 0

        async def fake_runner(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Model sees 5 messages, asks for more context
                return '{"need_more_context": true, "reason": "Непонятно о каком именно проекте идёт речь"}'
            if call_count == 2:
                # Model sees 10 messages, asks for more context again
                return '{"need_more_context": true, "reason": "Нужно увидеть начало обсуждения дедлайна"}'
            # Model sees 20 messages and returns 3 contextual replies
            return (
                "> chat · opencode/deepseek-v4.1-flash-free\n"
                "Thinking: analyzing 20 messages\n"
                '{"need_more_context": false, "context_summary": "Обсуждение релиза в пятницу", '
                '"replies": ["Да, к пятнице всё успеем!", "Привет! По проекту всё идёт по плану, в пятницу сдадим.", "Давай созвонимся вечером и сверим задачи до пятницы."]}'
            )

        meta = await mgr.suggest_reply("777", runner_fn=fake_runner, initial_limit=5, max_limit=40, return_meta=True)
        self.assertEqual(requested_limits, [5, 10, 20])
        self.assertTrue(meta["expanded"])
        self.assertEqual(meta["steps"], [5, 10, 20])
        self.assertEqual(meta["context_used"], 20)
        self.assertEqual(len(meta["replies"]), 3)
        self.assertEqual(meta["replies"][0], "Да, к пятнице всё успеем!")

    async def test_suggest_reply_auto_expands_on_missing_reply_target(self):
        mgr = TelegramUserManager()
        all_msgs = [
            {"id": i, "sender_id": 100, "sender": "Иван", "out": False, "text": f"Подробное сообщение #{i} с контекстом задачи", "reply_to_msg_id": (2 if i == 10 else None), "date": "2026-11-03T12:00:00"}
            for i in range(1, 11)
        ]
        requested_limits = []

        async def fake_get_messages(chat_id, limit=15):
            requested_limits.append(limit)
            return all_msgs[-limit:]

        mgr.get_messages = fake_get_messages

        async def fake_runner(prompt: str) -> str:
            return '["Понял, сделаю!", "Да, по этому пункту всё готово.", "Ок, сейчас поправлю."]'

        res = await mgr.suggest_reply("888", runner_fn=fake_runner, initial_limit=5, max_limit=20, return_meta=False)
        # First window of 5 messages (ids 6..10) references reply_to_msg_id=2 which is outside 6..10 -> auto-doubles to 10!
        self.assertEqual(requested_limits, [5, 10])
        self.assertEqual(len(res), 3)
        self.assertEqual(res[0], "Понял, сделаю!")


class TestDesktopAgentManager(unittest.IsolatedAsyncioTestCase):
    def test_desktop_action_confirmation_queue(self):
        mgr = DesktopAgentManager()
        mgr.require_confirmation = True
        res = mgr.execute_action(
            kind="paste_text",
            target_window="code",
            text="print('hi')",
            confirmed=False,
        )
        self.assertFalse(res["ok"])
        self.assertTrue(res["requires_confirmation"])
        pending = mgr.list_pending()
        self.assertEqual(len(pending), 1)
        act_id = pending[0]["id"]
        rej = mgr.reject_action(act_id)
        self.assertTrue(rej["ok"])
        self.assertEqual(len(mgr.list_pending()), 0)

    async def test_plan_actions_for_goal(self):
        mgr = DesktopAgentManager()
        planned = await mgr.plan_actions_for_goal('Вставь "hello world" и сохрани', target_window="code")
        self.assertGreaterEqual(len(planned), 2)
        kinds = [p["kind"] for p in planned]
        self.assertIn("focus_window", kinds)
        self.assertIn("type_text", kinds)
        self.assertIn("send_shortcut", kinds)


if __name__ == "__main__":
    unittest.main()
