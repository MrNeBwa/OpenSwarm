"""OpenRouter multi-key rotation."""
import json, os

from openswarm.config import OpenRouterSettings
from openswarm.keys import KeyPool, classify, parse_keys, reset_pools
from openswarm.servers import ServerPool
from .helpers import Env


class Clock:
    t = 0.0
    def __call__(self): return self.t


class PoolUnit(Env):
    def test_classify(self):
        self.assertEqual(classify("Error: 429 rate limit exceeded"), "rate")
        self.assertEqual(classify("402 Insufficient credits"), "quota")
        self.assertEqual(classify("Rate limit exceeded: free-models-per-day"), "quota")
        self.assertEqual(classify("401 invalid API key"), "auth")
        self.assertEqual(classify("syntax error in foo.py"), "")

    def test_parse_keys(self):
        self.assertEqual(parse_keys(["a", "b,c"], "c d\ne", None, "${UNSET}"), ["a", "b", "c", "d", "e"])

    def test_failover_and_cooldown(self):
        clk = Clock(); p = KeyPool(["k1", "k2", "k3"], OpenRouterSettings(rate_limit_cooldown_seconds=60), clk)
        self.assertEqual(KeyPool(["sk-or-v1-SECRETSECRET1234"], OpenRouterSettings(), clk).label("sk-or-v1-SECRETSECRET1234"), "#1 (…1234)")
        self.assertEqual(p.acquire(), ("k1", 0)); self.assertEqual(p.acquire(), ("k1", 0))  # sticky
        self.assertEqual(p.report_failure("k1", "429 too many requests"), "rate")
        self.assertEqual(p.acquire(), ("k2", 0))
        p.report_failure("k2", "401 invalid api key"); self.assertEqual(p.acquire(), ("k3", 0))
        p.report_failure("k3", "429"); key, wait = p.acquire()  # k1 and k3 resting, k2 dead
        self.assertEqual(key, "k1"); self.assertGreater(wait, 0)
        clk.t = 61; self.assertEqual(p.acquire(), ("k1", 0))
        self.assertEqual(p.report_failure("k1", "weird crash"), "")  # not the key's fault

    def test_round_robin_and_all_dead(self):
        p = KeyPool(["a", "b"], OpenRouterSettings(strategy="round_robin"), Clock())
        self.assertEqual([p.acquire()[0] for _ in range(4)], ["a", "b", "a", "b"])
        p.report_failure("a", "invalid api key"); p.report_failure("b", "invalid api key")
        self.assertEqual(p.acquire(), (None, 0)); self.assertFalse(p.has_live())


class Rotation(Env):
    def setUp(self):
        super().setUp(); reset_pools()
        self.cfg.openrouter.api_keys = ["key-AAAA", "key-BBBB", "key-CCCC"]

    def keys_used(self):
        return [l.split("=", 1)[1] for l in (self.tmp / "fake.log").read_text().splitlines() if l.startswith("key=")]

    async def test_run_switches_key_on_rate_limit(self):
        os.environ["FAKE_LIMITED_KEYS"] = "key-AAAA"
        s, logs, _, _ = await self.run_mode(self.plain(), "research", coder_model="openrouter/x/y", planner_model="openrouter/x/y", reviewer_model="openrouter/x/y")
        self.assertTrue(s.ok, logs)
        self.assertTrue(any("switching to the next key" in l for l in logs), logs)
        self.assertFalse(any("key-AAAA" in l for l in logs))  # never logged
        used = self.keys_used(); self.assertEqual(used[0], "key-AAAA"); self.assertIn("key-BBBB", used)
        self.assertNotIn("key-AAAA", used[2:])  # A is resting afterwards

    async def test_bad_key_disabled_and_exhaustion(self):
        os.environ["FAKE_BAD_KEYS"] = "key-AAAA,key-BBBB,key-CCCC"
        s, logs, _, _ = await self.run_mode(self.plain(), "chat", prompt="hi", chat_id="c", coder_model="openrouter/x/y")
        self.assertTrue(any("no usable keys left" in l for l in logs), logs)
        self.assertEqual(sorted(set(self.keys_used())), ["key-AAAA", "key-BBBB", "key-CCCC"])

    async def test_non_openrouter_models_untouched(self):
        os.environ["FAKE_LIMITED_KEYS"] = "key-AAAA"
        s, logs, _, _ = await self.run_mode(self.plain(), "research")
        self.assertTrue(s.ok, logs); self.assertEqual(self.keys_used(), [])

    async def test_chat_api_switches_key_with_own_server(self):
        os.environ["FAKE_LIMITED_KEYS"] = "key-AAAA"
        pool = ServerPool(self.cfg); self.addAsyncCleanup(pool.close)
        s, logs, results, _ = await self.run_mode(self.plain(), "chat", prompt="hello", pool=pool, coder_model="openrouter/x/y")
        self.assertTrue(s.ok, logs); self.assertTrue(any("switching to the next key" in l for l in logs), logs)
        self.assertIn("Hello from fake opencode", results[-1][1])
        self.assertEqual(len(pool._procs), 2)  # one warm server per key


class KeysApi(Env):
    """The 'OpenRouter keys' window: add / list / remove through the web API."""
    def setUp(self):
        super().setUp(); reset_pools()
        for k in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEYS"):
            os.environ.pop(k, None)

    async def asyncSetUp(self):
        from .test_web import Web
        self.w = Web(); self.w.cfg = self.cfg; self.w.tmp = self.tmp; self.w.plain = self.plain
        self.w.addAsyncCleanup = self.addAsyncCleanup; self.w.assertEqual = self.assertEqual
        await self.w.start()

    async def test_add_list_remove_and_pool(self):
        from openswarm.keys import get_pool, keys_file
        call = self.w.call
        st, d = await call("GET", "/api/keys"); self.assertEqual((st, d["keys"]), (200, []))
        st, d = await call("POST", "/api/keys", {"text": "sk-or-v1-aaaaaaaaaaaaaaaa1111\nsk-or-v1-bbbbbbbbbbbbbbbb2222, sk-or-v1-aaaaaaaaaaaaaaaa1111"})
        self.assertEqual((st, d["added"], d["duplicates"]), (200, 2, 0))
        self.assertEqual((await call("POST", "/api/keys", {"text": "sk-or-v1-aaaaaaaaaaaaaaaa1111"}))[1]["duplicates"], 1)
        self.assertEqual([k["masked"] for k in d["keys"]], ["sk-or-v…1111", "sk-or-v…2222"])
        self.assertNotIn("aaaaaaaaaaaaaaaa", json.dumps(d))  # secrets never leave the server
        self.assertEqual(oct(keys_file().stat().st_mode & 0o777), "0o600")
        self.assertEqual(len(get_pool(self.cfg)), 2)  # applies to the very next agent, no restart
        self.assertEqual((await call("POST", "/api/keys", {"text": "short"}))[0], 400)
        self.assertEqual((await call("GET", "/api/keys", token="bad"))[0], 403)
        kid = d["keys"][0]["id"]
        st, d = await call("DELETE", f"/api/keys/{kid}"); self.assertEqual([k["masked"] for k in d["keys"]], ["sk-or-v…2222"])
        self.cfg.openrouter.api_keys = ["sk-or-v1-cccccccccccccccc3333"]
        st, d = await call("GET", "/api/keys"); self.assertEqual([k["source"] for k in d["keys"]], ["config", "app"])
        self.assertEqual((await call("DELETE", f"/api/keys/{d['keys'][0]['id']}"))[0], 409)  # config keys are edited in the file

    async def test_state_survives_edit(self):
        from openswarm.keys import get_pool
        await self.w.call("POST", "/api/keys", {"text": "sk-or-v1-aaaaaaaaaaaaaaaa1111 sk-or-v1-bbbbbbbbbbbbbbbb2222"})
        p = get_pool(self.cfg); p.report_failure("sk-or-v1-aaaaaaaaaaaaaaaa1111", "429")
        await self.w.call("POST", "/api/keys", {"text": "sk-or-v1-cccccccccccccccc3333"})
        st, d = await self.w.call("GET", "/api/keys")
        self.assertEqual([k["state"] for k in d["keys"]], ["cooling", "ready", "ready"])
        kid = d["keys"][0]["id"]
        st, d = await self.w.call("POST", f"/api/keys/{kid}/reset"); self.assertEqual(d["keys"][0]["state"], "ready")

    async def test_load_openrouter_models(self):
        import openswarm.web as web
        web.fetch_openrouter_catalogue = lambda: ([("deepseek/x:free", "DeepSeek X"), ("anthropic/y", "Y")], "")
        await self.w.call("POST", "/api/keys", {"text": "sk-or-v1-aaaaaaaaaaaaaaaa1111"})
        st, d = await self.w.call("POST", "/api/keys/models")
        self.assertEqual((st, d["total"], d["added"] + 0 <= 2), (200, 2, True))
        ids = [m["id"] for m in (await self.w.call("GET", "/api/state"))[1]["models"]]
        self.assertEqual([i for i in ids if i.startswith("openrouter/")], ["openrouter/deepseek/x:free", "openrouter/anthropic/y"])  # free first
        web.fetch_openrouter_catalogue = lambda: ([], "cannot load")
        self.cfg.models = [m for m in self.cfg.models if not m.id.startswith("openrouter/")]
        st, d = await self.w.call("POST", "/api/keys/models"); self.assertEqual((d["total"], d["error"]), (0, "cannot load"))


class InlineModel(Env):
    def test_inline_config(self):
        from openswarm.keys import inline_model_config
        c = inline_model_config(self.cfg, "openrouter/deepseek/new-model:free")
        self.assertEqual(json.loads(c), {"provider": {"openrouter": {"models": {"deepseek/new-model:free": {"name": "deepseek/new-model:free"}}}}})
        self.assertEqual(inline_model_config(self.cfg, "anthropic/claude-sonnet-4-5"), "")
        merged = json.loads(inline_model_config(self.cfg, "openrouter/a/b", '{"theme":"x"}'))
        self.assertEqual((merged["theme"], list(merged["provider"]["openrouter"]["models"])), ("x", ["a/b"]))

    async def test_run_passes_it_to_opencode(self):
        reset_pools()
        s, logs, _, _ = await self.run_mode(self.plain(), "research", coder_model="openrouter/x/brand-new", planner_model="openrouter/x/brand-new", reviewer_model="openrouter/x/brand-new")
        self.assertTrue(s.ok, logs)
        cfgs = [l for l in (self.tmp / "fake.log").read_text().splitlines() if l.startswith("cfg=")]
        self.assertTrue(cfgs and all('"x/brand-new"' in c for c in cfgs), cfgs)
