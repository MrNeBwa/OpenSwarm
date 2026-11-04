import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from openswarm.config import SwarmConfig, default_config
from openswarm.orchestrator import Selection, Orchestrator, Hooks
from openswarm.router import get_router, reset_router, score_model_capabilities
from openswarm.runner import OpenCodeRunner
from openswarm.web import Session, WebApp


class OpenCodePerfectionTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_router()
        router = get_router()
        router.stats.clear()
        router.clear_cooldowns()

    def tearDown(self) -> None:
        reset_router()

    def test_opencode_native_models_defaults_and_scoring(self) -> None:
        cfg = default_config()
        # Verify role models default to active native opencode models
        self.assertEqual(cfg.roles.coder, "opencode/deepseek-v4.1-flash-free")
        self.assertEqual(cfg.roles.planner, "opencode/beam-free")
        self.assertEqual(cfg.roles.architect, "opencode/beam-free")
        self.assertEqual(cfg.roles.reviewer, "opencode/nemotron-3.5-super-vl-free")

        # Verify router score for opencode native models gives high marks
        score_beam = score_model_capabilities("opencode/beam-free")
        self.assertGreaterEqual(score_beam["reasoning"], 93)
        self.assertTrue(score_beam["free"])

        score_ds = score_model_capabilities("opencode/deepseek-v4.1-flash-free")
        self.assertGreaterEqual(score_ds["coding"], 95)
        self.assertGreaterEqual(score_ds["speed"], 90)

        # Check recommendations prioritize opencode / deepseek-harness native models
        router = get_router()
        rec = router.recommend_roles(cfg, "balanced")
        self.assertTrue(rec["coder"].startswith(("opencode/", "deepseek-harness/")))
        self.assertTrue(rec["planner"].startswith(("opencode/", "deepseek-harness/")))

    def test_reasoning_effort_and_runner_flags(self) -> None:
        cfg = default_config()
        cfg.effort = "high"
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            runner = OpenCodeRunner(
                cfg, cwd, agent_id="agent1", role="coder", model="opencode/beam-free",
                effort="max", thinking=True, variant_flag="--variant"
            )
            cmd = runner.build_command("write a test")
            self.assertIn("--variant", cmd)
            self.assertIn("max", cmd)
            self.assertIn("--thinking", cmd)

            # When effort is default, --variant should not be appended
            runner_default = OpenCodeRunner(
                cfg, cwd, agent_id="agent1", role="coder", model="opencode/beam-free",
                effort="default", thinking=False
            )
            cmd_def = runner_default.build_command("write a test")
            self.assertNotIn("--variant", cmd_def)
            self.assertNotIn("--thinking", cmd_def)

            # OpenCode v2 mode: when binary does not support --variant, model is formatted as model#effort
            runner_v2 = OpenCodeRunner(
                cfg, cwd, agent_id="agent1", role="coder", model="opencode/beam-free",
                effort="high", variant_flag="", thinking=True
            )
            cmd_v2 = runner_v2.build_command("write a test")
            self.assertNotIn("--variant", cmd_v2)
            self.assertIn("opencode/beam-free#high", cmd_v2)

            # Flag rejection learning
            from openswarm.capabilities import Caps, rejected_flag
            err_msg = "ERROR\n  Unrecognized flag: --variant in command opencode run\n[exit 1]"
            bad = rejected_flag(err_msg)
            self.assertEqual(bad, "--variant")

            caps = Caps(flags={"--model", "--session", "--variant"}, parsed=True)
            self.assertTrue(caps.can_variant(cfg.opencode))
            caps.disable(bad)
            self.assertFalse(caps.can_variant(cfg.opencode))
            self.assertEqual(caps.variant_flag(cfg.opencode), "")

            # Test non-variant model filtering (e.g. deepseek-v4.1-flash-free does not take variants)
            from openswarm.capabilities import model_supports_variant, rejected_variant_model, mark_model_variant_unsupported
            from openswarm.healer import analyze_error

            self.assertFalse(model_supports_variant("opencode/deepseek-v4.1-flash-free"))
            self.assertTrue(model_supports_variant("opencode/beam-free"))
            self.assertTrue(model_supports_variant("opencode/nemotron-3.5-super-vl-free"))

            runner_flash = OpenCodeRunner(
                cfg, cwd, agent_id="agent1", role="coder", model="opencode/deepseek-v4.1-flash-free",
                effort="medium", thinking=True
            )
            cmd_flash = runner_flash.build_command("write a test")
            self.assertNotIn("--variant", cmd_flash)
            self.assertIn("--model", cmd_flash)
            model_arg = cmd_flash[cmd_flash.index("--model") + 1]
            self.assertNotIn("#medium", model_arg)
            self.assertEqual(model_arg, "opencode/deepseek-v4.1-flash-free")

            # Test variant unavailable error parsing & healing
            var_err = "Error: Variant unavailable for opencode/deepseek-v4.1-flash-free: medium\n[exit 1]"
            diag = analyze_error(var_err)
            self.assertEqual(diag["kind"], "variant_unavailable")
            self.assertEqual(rejected_variant_model(var_err), "opencode/deepseek-v4.1-flash-free")
            mark_model_variant_unsupported("custom/some-model")
            self.assertFalse(model_supports_variant("custom/some-model"))

    def test_conversation_context_memory_retention(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            s = Session("test_sid", cwd, "chat", "Test Session")
            s.emit("user", text="Первый вопрос: опиши структуру проекта")
            s.emit("result", text="Структура: src, tests, README", kind="chat")
            s.emit("user", text="Второй вопрос: добавь класс Car")
            s.emit("result", text="Класс Car добавлен в car.py", kind="chat")

            history = s.build_conversation_history()
            self.assertIn("Первый вопрос", history)
            self.assertIn("src, tests", history)
            self.assertIn("Второй вопрос", history)
            self.assertIn("Класс Car добавлен", history)

    def test_session_fork_api(self) -> None:
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            cfg = default_config()
            app = WebApp(cfg, cwd, port=19999, token="test_token")
            parent = app.new_session(cwd, "chat", "Parent Branch")
            parent.emit("user", text="Original prompt")
            parent.emit("result", text="Original reply", kind="chat")

            forked = app.fork_session(parent.id)
            self.assertNotEqual(forked.id, parent.id)
            self.assertEqual(forked.title, "Parent Branch (fork)")
            self.assertEqual(len(forked.events), 2)
            self.assertEqual(forked.events[0]["text"], "Original prompt")

    def test_soft_success_on_benign_exit_1(self) -> None:
        from openswarm.runner import is_soft_success

        sample_out = """[swarm/r65fa46-pz-contract-sources 925facf] docs(pz): add author brief and verified bibliography.
 3 files changed, 545 insertions(+)
 create mode 100644 docs/pz/AUTHOR_BRIEF.md
 create mode 100644 docs/pz/SOURCES.md
 create mode 100644 docs/pz/sources.json
> build · deepseek-v4.1-flash-free
Thinking: Committed. Need maybe notify swarm peers?
$ git status --short; git log --oneline -1
925facf docs(pz): add author brief and verified bibliography.
> build · deepseek-v4.1-flash-free
Готово. Созданы и закоммичены три файла:
- `docs/pz/AUTHOR_BRIEF.md` — карта фрагментов с бюджетами и владельцами.
- `docs/pz/sources.json` — 26 проверенных источников.
- `docs/pz/SOURCES.md` — протокол проверки.
"""
        ok, reason = is_soft_success(sample_out, "", 1)
        self.assertTrue(ok)
        self.assertIn("git commit", reason)

        # Real errors must NOT be treated as soft success
        ok_err, _ = is_soft_success("", "Error: 429 rate limit exceeded", 1)
        self.assertFalse(ok_err)

        ok_flag, _ = is_soft_success("", "ERROR\n  Unrecognized flag: --attach", 1)
        self.assertFalse(ok_flag)

        ok_code2, _ = is_soft_success(sample_out, "", 2)
        self.assertFalse(ok_code2)

    def test_strip_reasoning_and_free_limit_bypass(self) -> None:
        from openswarm.dsh import (
            is_opencode_free_limited,
            is_opencode_free_model,
            mark_opencode_free_limited,
            next_free_fallback_model,
            resolve_dsh_model,
        )
        from openswarm.kilo import normalize_kilo_model
        from openswarm.runner import RunResult, strip_reasoning

        raw_out = (
            "\x1b[32m> build · nemotron-3.5-super-vl:free\x1b[0m\n"
            "Thinking: Пользователь спрашивает простой вопрос.\n"
            "<think>Internal reasoning steps...</think>\n\n"
            "Готовый чистый ответ."
        )
        res = RunResult(0, output=raw_out)
        self.assertEqual(res.clean_output, "Готовый чистый ответ.")
        self.assertEqual(strip_reasoning(raw_out), "Готовый чистый ответ.")

        # Legacy Laguna S 2.1 model transparently maps to working North Mini Code model
        self.assertEqual(
            normalize_kilo_model("kilo/poolside/laguna-s-2.1:free"),
            "kilo/cohere/north-mini-code:free",
        )

        # Free limit bypass cooldown tracking
        self.assertTrue(is_opencode_free_model("opencode/deepseek-v4.1-flash-free"))
        mark_opencode_free_limited(cooldown_s=60.0)
        self.assertTrue(is_opencode_free_limited())
        self.assertEqual(
            resolve_dsh_model("opencode/deepseek-v4.1-flash-free"),
            "kilo/nvidia/nemotron-3.5-super-vl:free",
        )
        self.assertEqual(
            next_free_fallback_model("opencode/deepseek-v4.1-flash-free"),
            "kilo/kilo-auto/free",
        )
        mark_opencode_free_limited(cooldown_s=-1.0)
        self.assertFalse(is_opencode_free_limited())
