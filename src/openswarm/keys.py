"""Several OpenRouter API keys with automatic failover.

opencode reads the OpenRouter key from the `OPENROUTER_API_KEY` environment variable, so rotation is done
here: every agent is started with one key from the pool; when a run fails because that key is rate limited,
out of credits or rejected, the key is put on a cool-down (or disabled) and the run is repeated at once
with the next key. The state is shared by all sessions of the process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Literal

from .config import OpenRouterSettings, SwarmConfig

Kind = Literal["rate", "quota", "auth", ""]

# Order matters: a key problem is "auth" > "quota" > "rate"; anything else is not the key's fault.
_AUTH_RE = re.compile(
    r"invalid.{0,12}(api.?)?key|incorrect api key|no auth credentials|user not found|"
    r"key.{0,25}(disabled|revoked|expired|deleted)|openrouter.{0,120}\b401\b|\b401\b.{0,120}openrouter",
    re.I | re.S,
)
_QUOTA_RE = re.compile(
    r"insufficient.{0,20}(credit|balance|fund)|\b402\b|payment required|key limit|credit limit|"
    r"quota|free.models?.per.day|daily (request|limit)|limit exceeded.{0,40}(day|month)",
    re.I | re.S,
)
_RATE_RE = re.compile(r"rate.?limit|\b429\b|too many requests|requests per (minute|second)", re.I)


def classify(text: str) -> Kind:
    """What does this failure say about the *key* (as opposed to the model, the network or the task)?"""
    if not text:
        return ""
    if _AUTH_RE.search(text):
        return "auth"
    if _QUOTA_RE.search(text):
        return "quota"
    if _RATE_RE.search(text):
        return "rate"
    return ""


def parse_keys(*sources: str | list[str] | None) -> list[str]:
    """Flatten keys from config lists / env strings (separated by commas, spaces or newlines); dedupe, keep order."""
    out: list[str] = []
    for src in sources:
        items = src if isinstance(src, list) else [src or ""]
        for item in items:
            for tok in re.split(r"[\s,;]+", str(item)):
                tok = tok.strip().strip("'\"")
                if tok and not tok.startswith("${") and tok not in out:
                    out.append(tok)
    return out


def keys_file() -> Path:
    """Keys added in the app live here (not in swarm_config.yaml, which may sit in a git repo)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "openswarm" / "openrouter_keys.json"


def load_stored_keys() -> list[str]:
    try:
        data = json.loads(keys_file().read_text(encoding="utf-8"))
        return parse_keys([str(k) for k in data.get("keys", [])]) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def save_stored_keys(keys: list[str]) -> None:
    path = keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")  # mkstemp creates the file with mode 600
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"keys": keys}, fh, indent=2)
    os.replace(tmp, path)


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def mask(key: str) -> str:
    return key[:7] + "…" + key[-4:] if len(key) > 14 else "…" + key[-4:]


def key_sources(cfg: SwarmConfig) -> list[tuple[str, str]]:
    """[(key, 'config' | 'env' | 'app')] in priority order, deduplicated."""
    s = cfg.openrouter
    out: list[tuple[str, str]] = []
    for src, keys in (("config", parse_keys(s.api_keys)),
                      ("env", parse_keys(os.environ.get(s.keys_env_var), os.environ.get(s.env_var))),
                      ("app", load_stored_keys())):
        out += [(k, src) for k in keys if k not in {x for x, _ in out}]
    return out


def configured_keys(cfg: SwarmConfig) -> list[str]:
    return [k for k, _ in key_sources(cfg)]


def is_openrouter_model(cfg: SwarmConfig, model: str) -> bool:
    return bool(model) and model.startswith(cfg.openrouter.model_prefix)


CONFIG_ENV = "OPENCODE_CONFIG_CONTENT"


def inline_model_config(cfg: SwarmConfig, model: str, existing: str = "") -> str:
    """opencode only knows the models of its bundled catalogue, so a newer model that exists on openrouter.ai,
    deepseek-harness, or kilo fails with 'model not found'. An inline config declaring the model makes opencode accept it."""
    from .dsh import DSH_PROVIDER_ID, dsh_provider_config, is_dsh_model
    from .kilo import KILO_PROVIDER_ID, is_kilo_model, kilo_provider_config

    if is_dsh_model(model):
        dsh_cfg = dsh_provider_config()
        if not dsh_cfg:
            return existing
        try:
            base = json.loads(existing) if existing else {}
        except ValueError:
            base = {}
        base = base if isinstance(base, dict) else {}
        base.setdefault("provider", {})[DSH_PROVIDER_ID] = dsh_cfg
        return json.dumps(base, separators=(",", ":"), sort_keys=True)

    if is_kilo_model(model):
        k_cfg = kilo_provider_config(extra_model=model)
        try:
            base = json.loads(existing) if existing else {}
        except ValueError:
            base = {}
        base = base if isinstance(base, dict) else {}
        base.setdefault("provider", {})[KILO_PROVIDER_ID] = k_cfg
        return json.dumps(base, separators=(",", ":"), sort_keys=True)

    pre = cfg.openrouter.model_prefix
    if not model.startswith(pre) or len(model) == len(pre):
        return ""
    try:
        base = json.loads(existing) if existing else {}
    except ValueError:
        base = {}
    base = base if isinstance(base, dict) else {}
    prov = base.setdefault("provider", {}).setdefault(pre.rstrip("/"), {})
    prov.setdefault("models", {}).setdefault(model[len(pre):], {"name": model[len(pre):]})
    return json.dumps(base, separators=(",", ":"), sort_keys=True)


@dataclass
class _Key:
    value: str
    index: int
    until: float = 0.0  # unusable until (monotonic seconds)
    dead: bool = False  # rejected by the provider (bad / revoked key) - never used again this process
    uses: int = 0
    last_used: int = 0
    reason: str = ""


class KeyPool:
    def __init__(self, keys: list[str], settings: OpenRouterSettings, clock: Callable[[], float] = time.monotonic) -> None:
        self.s = settings
        self._clock = clock
        self._keys = [_Key(k, i + 1) for i, k in enumerate(keys)]
        self._cur = 0
        self._tick = 0

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def env_var(self) -> str:
        return self.s.env_var

    def label(self, key: str) -> str:
        """Never print a secret: '#2 (…abcd)'."""
        k = next((k for k in self._keys if k.value == key), None)
        return f"#{k.index} (…{key[-4:]})" if k else "(…" + key[-4:] + ")"

    def has_live(self) -> bool:
        return any(not k.dead for k in self._keys)

    def acquire(self) -> tuple[str | None, float]:
        """(key, seconds to wait before using it). (None, 0) when every key has been rejected."""
        now = self._clock()
        live = [k for k in self._keys if not k.dead]
        if not live:
            return None, 0.0
        ready = [k for k in live if k.until <= now]
        if not ready:
            k = min(live, key=lambda x: x.until)
            return k.value, k.until - now
        if self.s.strategy == "round_robin":
            k = min(ready, key=lambda x: (x.last_used, x.index))
        else:  # failover: stay on the current key until it fails, then take the next usable one
            n = len(self._keys)
            k = next(self._keys[(self._cur + i) % n] for i in range(n) if self._keys[(self._cur + i) % n] in ready)
            self._cur = k.index - 1
        self._tick += 1
        k.last_used, k.uses = self._tick, k.uses + 1
        return k.value, 0.0

    def report_failure(self, key: str, text: str) -> Kind:
        """Classify a failed run and penalise the key. Returns the kind ('' = not the key's fault)."""
        kind = classify(text)
        k = next((k for k in self._keys if k.value == key), None)
        if not kind or k is None:
            return ""
        if kind == "auth":
            k.dead, k.reason = True, "rejected by OpenRouter (invalid or revoked)"
        elif kind == "quota":
            k.until, k.reason = self._clock() + self.s.quota_cooldown_seconds, "out of credits / quota"
        else:
            k.until, k.reason = self._clock() + self.s.rate_limit_cooldown_seconds, "rate limited"
        if kind != "auth" and self._keys[self._cur] is k:  # failover moves on
            self._cur = k.index % len(self._keys)
        return kind

    def sync(self, keys: list[str]) -> None:
        """Adopt an edited key list, keeping the cool-down state of keys that stay."""
        old = {k.value: k for k in self._keys}
        cur = self._keys[self._cur].value if self._keys else None
        self._keys = []
        for i, v in enumerate(keys):
            k = old.get(v) or _Key(v, i + 1)
            k.index = i + 1
            self._keys.append(k)
        self._cur = next((k.index - 1 for k in self._keys if k.value == cur), 0)

    def revive(self, key: str) -> None:
        for k in self._keys:
            if k.value == key:
                k.until, k.dead, k.reason = 0.0, False, ""

    def info(self, key: str) -> dict:
        k = next((k for k in self._keys if k.value == key), None)
        if not k:
            return {"state": "ready", "wait_seconds": 0, "uses": 0, "reason": ""}
        now = self._clock()
        return {"state": "disabled" if k.dead else ("cooling" if k.until > now else "ready"),
                "wait_seconds": 0 if k.dead else max(0, round(k.until - now)), "uses": k.uses, "reason": k.reason}

    def status(self) -> list[dict]:
        now = self._clock()
        return [{"key": f"#{k.index} …{k.value[-4:]}", "state": "disabled" if k.dead else ("cooling" if k.until > now else "ready"),
                 "wait_seconds": max(0, round(k.until - now)) if not k.dead else 0, "uses": k.uses, "reason": k.reason}
                for k in self._keys]


_POOL: tuple[tuple, KeyPool] | None = None


def get_pool(cfg: SwarmConfig) -> KeyPool | None:
    """The shared pool (state is common to all sessions), or None when no keys are configured (opencode then uses its own auth).
    Re-read on every call, so keys added in the app apply to the very next agent."""
    global _POOL
    keys = configured_keys(cfg)
    if not keys:
        return None
    s = cfg.openrouter
    sig = (s.strategy, s.env_var, s.rate_limit_cooldown_seconds, s.quota_cooldown_seconds, s.max_wait_seconds)
    if _POOL is None or _POOL[0] != sig:
        _POOL = (sig, KeyPool(keys, s))
    else:
        _POOL[1].sync(keys)
    return _POOL[1]


def reset_pools() -> None:
    global _POOL
    _POOL = None


class KeyRun:
    """Key bookkeeping for one agent: pick a key, and after a failure decide whether to retry with another."""

    def __init__(self, pool: KeyPool, log: Callable[[str], object]) -> None:
        self.pool = pool
        self.key: str | None = None
        self._log = log  # async callable
        self._budget = len(pool) * 2  # each key may be tried about twice per agent run

    @property
    def env(self) -> dict[str, str]:
        return {self.pool.env_var: self.key} if self.key else {}

    async def next_key(self) -> str | None:
        import asyncio

        key, wait = self.pool.acquire()
        if key and wait > 0:
            wait = min(wait, self.pool.s.max_wait_seconds)
            await self._log(f"all OpenRouter keys are resting - waiting {wait:.0f}s for key {self.pool.label(key)}")
            await asyncio.sleep(wait)
        self.key = key
        return key

    async def failed(self, text: str) -> bool:
        """True -> the failure was this key's fault and another attempt (with the next key) is worth making."""
        if not self.key:
            return False
        kind = self.pool.report_failure(self.key, text)
        if not kind:
            return False
        what = {"rate": "is rate limited", "quota": "is out of credits/quota", "auth": "was rejected"}[kind]
        if not self.pool.has_live():
            await self._log(f"OpenRouter key {self.pool.label(self.key)} {what}; no usable keys left")
            return False
        if self._budget <= 0:
            return False
        self._budget -= 1
        await self._log(f"OpenRouter key {self.pool.label(self.key)} {what} - switching to the next key")
        return True
