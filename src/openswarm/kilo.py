"""Kilo AI Gateway (`https://api.kilo.ai/api/gateway`) integration for OpenSwarm.

Provides:
1. Zero-config anonymous access (`Authorization: Bearer anonymous`, ~200 requests/hour per IP) to
   Kilo's free model catalog:
   - `kilo/kilo-auto/free` — Kilo Auto Free Router (256K context)
   - `kilo/poolside/laguna-s-2.1:free` — Poolside Laguna S 2.1 Free (Coding specialist, 262K context)
   - `kilo/cohere/north-mini-code:free` — Cohere North Mini Code Free (256K context)
   - `kilo/recursive/flint-v1.0:free` — Recursive Flint 1.0 Free (262K context)
   - `kilo/nvidia/nemotron-3.5-lightning:free` — NVIDIA Nemotron 3.5 Lightning Free (1M context)
   - `kilo/nvidia/nemotron-3.5-super-vl:free` — NVIDIA Nemotron 3.5 Super VL Free (1M context)
   - `kilo/reflection/beam-free` — Reflection Beam 1.0 Free (1M context)
   - `kilo/stealth/glyph-cluster` — Stealth Glyph Cluster Free (256K context)
2. Optional personal `KILO_API_KEY` support (stored in `~/.config/openswarm/kilo_account.json` or
   via `KILO_API_KEY` environment variable) for higher rate limits and full catalog access.
3. Dynamic OpenCode provider injection (`OPENCODE_CONFIG_CONTENT`) via `@ai-sdk/openai-compatible`
   plus direct live catalog discovery from `https://api.kilo.ai/api/gateway/models`.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

KILO_PROVIDER_ID = "kilo"
KILO_GATEWAY_BASE_URL = "https://api.kilo.ai/api/gateway"
KILO_ALT_BASE_URL = "https://ai-gateway.kilo.ai/api/v1"
KILO_ANONYMOUS_TOKEN = "anonymous"
KILO_DEFAULT_MODEL = "kilo/nvidia/nemotron-3.5-super-vl:free"
KILO_FALLBACK_FREE_MODEL = "kilo/cohere/north-mini-code:free"

# Short aliases -> canonical upstream Kilo gateway model IDs
KILO_ALIASES: dict[str, str] = {
    "auto-free": "kilo-auto/free",
    "kilo-auto": "kilo-auto/free",
    "poolside/laguna-s-2.1:free": "cohere/north-mini-code:free",
    "laguna-free": "cohere/north-mini-code:free",
    "laguna-s-2.1-free": "cohere/north-mini-code:free",
    "north-code-free": "cohere/north-mini-code:free",
    "north-mini-code-free": "cohere/north-mini-code:free",
    "recursive/flint-v1.0:free": "cohere/north-mini-code:free",
    "flint-free": "cohere/north-mini-code:free",
    "nemotron-lightning-free": "nvidia/nemotron-3.5-lightning:free",
    "nemotron-super-free": "nvidia/nemotron-3.5-super-vl:free",
    "beam-free": "reflection/beam-free",
    "glyph-free": "stealth/glyph-cluster",
}

KILO_BUILTIN_FREE_MODELS: dict[str, dict[str, Any]] = {
    "cohere/north-mini-code:free": {
        "name": "Cohere North Mini Code Free · Kilo (256K)",
        "limit": {"context": 256000, "output": 32768},
    },
    "nvidia/nemotron-3.5-super-vl:free": {
        "name": "NVIDIA Nemotron 3.5 Super VL Free · Kilo (1M)",
        "limit": {"context": 1000000, "output": 32768},
    },
    "reflection/beam-free": {
        "name": "Reflection Beam 1.0 Free · Kilo (1M)",
        "limit": {"context": 1048576, "output": 65536},
    },
    "nvidia/nemotron-3.5-lightning:free": {
        "name": "NVIDIA Nemotron 3.5 Lightning Free · Kilo (1M)",
        "limit": {"context": 1000000, "output": 32768},
    },
    "kilo-auto/free": {
        "name": "Kilo Auto Free Router (256K)",
        "limit": {"context": 256000, "output": 32768},
    },
    "openrouter/free": {
        "name": "OpenRouter Free Models Router · Kilo (200K)",
        "limit": {"context": 200000, "output": 32768},
    },
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": {
        "name": "NVIDIA Nemotron 3 Nano Omni Reasoning Free · Kilo (256K)",
        "limit": {"context": 262144, "output": 32768},
    },
    "nvidia/nemotron-3-ultra-550b-a55b:free": {
        "name": "NVIDIA Nemotron 3 Ultra 550B Free · Kilo (256K)",
        "limit": {"context": 262144, "output": 32768},
    },
    "stealth/glyph-cluster": {
        "name": "Stealth Glyph Cluster Free · Kilo (256K)",
        "limit": {"context": 262144, "output": 32768},
    },
    "dots-studio/dots-3-note-preview:free": {
        "name": "Dots Studio Dots3-Note Free · Kilo (256K)",
        "limit": {"context": 262144, "output": 32768},
    },
}


def kilo_state_file() -> Path:
    """Where OpenSwarm stores Kilo AI Gateway settings and optional API key."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "openswarm" / "kilo_account.json"


def _read_state() -> dict[str, Any]:
    path = kilo_state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    path = kilo_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def mask_token(token: str) -> str:
    t = (token or "").strip()
    if not t or t == KILO_ANONYMOUS_TOKEN:
        return "anonymous (free)"
    if len(t) <= 10:
        return "***"
    return f"{t[:5]}…{t[-4:]}"


def load_kilo_credentials() -> dict[str, Any]:
    """Load Kilo AI Gateway credentials from state file or `KILO_API_KEY` env."""
    st = _read_state()
    mode = str(st.get("mode") or "anonymous")
    tok = str(st.get("token") or "").strip()
    if tok:
        return {
            "token": tok,
            "masked": mask_token(tok),
            "source": str(st.get("source") or "app"),
            "mode": mode,
            "configured": True,
        }
    env_tok = (os.environ.get("KILO_API_KEY") or "").strip()
    if env_tok:
        return {
            "token": env_tok,
            "masked": mask_token(env_tok),
            "source": "env",
            "mode": "api_key",
            "configured": True,
        }
    return {
        "token": KILO_ANONYMOUS_TOKEN,
        "masked": "anonymous (free tier · ~200 req/hr)",
        "source": "anonymous",
        "mode": "anonymous",
        "configured": False,
    }


def save_kilo_credentials(token: str | None = None, *, mode: str | None = None) -> dict[str, Any]:
    """Save or update Kilo AI Gateway token / mode (`anonymous` or `api_key`)."""
    st = _read_state()
    if token is not None:
        clean = token.strip()
        if clean and clean != KILO_ANONYMOUS_TOKEN:
            st["token"] = clean
            st["source"] = "app"
            st["updated_at"] = int(time.time())
            if not mode:
                mode = "api_key"
        else:
            st.pop("token", None)
            if not mode:
                mode = "anonymous"
    if mode:
        st["mode"] = mode
    _write_state(st)
    return kilo_status()


def clear_kilo_credentials() -> dict[str, Any]:
    """Reset Kilo Gateway to anonymous zero-key mode."""
    st = _read_state()
    st.pop("token", None)
    st["mode"] = "anonymous"
    _write_state(st)
    return kilo_status()


def is_kilo_model(model: str) -> bool:
    """True when `model` belongs to the `kilo/` provider."""
    if not model:
        return False
    raw = model.split("#", 1)[0].strip().lower()
    return raw.startswith(f"{KILO_PROVIDER_ID}/")


def normalize_kilo_model(model: str) -> str:
    """Expand short `kilo/<alias>` IDs into `kilo/<upstream_id>`."""
    if not is_kilo_model(model):
        return model
    base, sep, variant = model.partition("#")
    sub = base[len(KILO_PROVIDER_ID) + 1 :].strip()
    resolved = KILO_ALIASES.get(sub.lower(), sub)
    out = f"{KILO_PROVIDER_ID}/{resolved}"
    return f"{out}#{variant}" if sep else out


def kilo_provider_config(extra_model: str = "") -> dict[str, Any]:
    """Build the OpenCode provider dictionary for `kilo` (`@ai-sdk/openai-compatible`)."""
    creds = load_kilo_credentials()
    tok = str(creds.get("token") or KILO_ANONYMOUS_TOKEN).strip() or KILO_ANONYMOUS_TOKEN
    if creds.get("mode") == "anonymous":
        tok = KILO_ANONYMOUS_TOKEN
    models = dict(KILO_BUILTIN_FREE_MODELS)
    if extra_model and is_kilo_model(extra_model):
        norm = normalize_kilo_model(extra_model).split("#", 1)[0]
        sub = norm[len(KILO_PROVIDER_ID) + 1 :]
        if sub and sub not in models:
            models[sub] = {
                "name": f"{sub} · Kilo",
                "limit": {"context": 256000, "output": 32768},
            }
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Kilo AI Gateway",
        "options": {
            "baseURL": KILO_GATEWAY_BASE_URL,
            "apiKey": tok,
        },
        "models": models,
    }


def fetch_kilo_free_models(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Query `https://api.kilo.ai/api/gateway/models` for currently available free models."""
    req = urllib.request.Request(
        f"{KILO_GATEWAY_BASE_URL}/models",
        headers={"Accept": "application/json", "User-Agent": "OpenSwarm/1.0"},
        method="GET",
    )
    out: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        items = raw.get("data", raw) if isinstance(raw, dict) else raw
        for m in items if isinstance(items, list) else []:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            pricing = m.get("pricing") or {}
            is_free = (
                mid.endswith(":free")
                or mid == "kilo-auto/free"
                or mid.endswith("-free")
                or bool(m.get("isFree"))
                or (
                    str(pricing.get("prompt", "1")) in ("0", "0.0", "0.00")
                    and str(pricing.get("completion", "1")) in ("0", "0.0", "0.00")
                )
            )
            if is_free:
                out.append({
                    "id": f"{KILO_PROVIDER_ID}/{mid}",
                    "upstream_id": mid,
                    "label": str(m.get("name") or mid),
                    "context_length": int(m.get("context_length") or 256000),
                    "free": True,
                })
    except Exception:
        pass
    if not out:
        for mid, info in KILO_BUILTIN_FREE_MODELS.items():
            out.append({
                "id": f"{KILO_PROVIDER_ID}/{mid}",
                "upstream_id": mid,
                "label": str(info.get("name") or mid),
                "context_length": int((info.get("limit") or {}).get("context", 256000)),
                "free": True,
            })
    return out


def test_kilo_completion(model: str = KILO_DEFAULT_MODEL, timeout: float = 20.0) -> dict[str, Any]:
    """Send a quick test completion directly to the Kilo AI Gateway."""
    creds = load_kilo_credentials()
    tok = str(creds.get("token") or KILO_ANONYMOUS_TOKEN).strip() or KILO_ANONYMOUS_TOKEN
    if creds.get("mode") == "anonymous":
        tok = KILO_ANONYMOUS_TOKEN
    norm = normalize_kilo_model(model if is_kilo_model(model) else KILO_DEFAULT_MODEL)
    upstream_model = norm[len(KILO_PROVIDER_ID) + 1 :].split("#", 1)[0]
    body = json.dumps({
        "model": upstream_model,
        "messages": [{"role": "user", "content": "Ответь одним словом: OK"}],
        "max_tokens": 64,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{KILO_GATEWAY_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OpenSwarm/1.0",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        dt = round(time.monotonic() - t0, 2)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        reply = (msg.get("content") or msg.get("reasoning") or "OK").strip()
        return {
            "ok": True,
            "model": f"{KILO_PROVIDER_ID}/{data.get('model') or upstream_model}",
            "latency_s": dt,
            "output": reply[:240],
        }
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:260]
        return {
            "ok": False,
            "model": norm,
            "error": f"HTTP {exc.code}: {err_body}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "model": norm,
            "error": str(exc)[:240],
        }


def kilo_status() -> dict[str, Any]:
    """Return full Kilo AI Gateway status for the Web UI and diagnostics."""
    creds = load_kilo_credentials()
    has_key = bool(creds.get("configured")) and creds.get("mode") != "anonymous"
    return {
        "provider_id": KILO_PROVIDER_ID,
        "gateway_url": KILO_GATEWAY_BASE_URL,
        "alt_gateway_url": KILO_ALT_BASE_URL,
        "mode": "api_key" if has_key else "anonymous",
        "has_api_key": has_key,
        "masked_token": creds.get("masked", "anonymous (free)"),
        "source": creds.get("source", "anonymous"),
        "default_model": KILO_DEFAULT_MODEL,
        "models": [
            {
                "id": f"{KILO_PROVIDER_ID}/{mid}",
                "label": info["name"],
                "context": info["limit"]["context"],
                "free": True,
            }
            for mid, info in KILO_BUILTIN_FREE_MODELS.items()
        ],
    }
