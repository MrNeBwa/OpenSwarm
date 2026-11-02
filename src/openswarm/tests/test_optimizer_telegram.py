import unittest
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
)
from openswarm.telegram_user import TelegramUserManager, get_telegram_user_manager


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
        # Old messages are condensed into summary bullet points
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

    def test_optimize_prompt_for_model(self):
        p = "System instruction\n\n\n\nUser query\n" + "\n".join(f"diff line {i}" for i in range(120))
        opt = optimize_prompt_for_model(p, role="coder")
        self.assertIn("User query", opt)
        st = get_optimizer_stats()
        self.assertGreater(st["saved_chars"], 0)


class TestTelegramUserManager(unittest.IsolatedAsyncioTestCase):
    async def test_status_disconnected_by_default(self):
        mgr = TelegramUserManager()
        st = await mgr.get_status()
        self.assertIn("installed", st)
        self.assertIn("active", st)
        self.assertFalse(st["active"])
        self.assertEqual(st["status"], "disconnected")


if __name__ == "__main__":
    unittest.main()
