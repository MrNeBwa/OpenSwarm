"""DeepSeek Harness (@deepseek-ai/dsh) integration for OpenSwarm.

Provides:
1. Zero-config free unlimited `DeepSeek-V4.1-Flash` routing:
   - When `deepseek-harness/deepseek-flash` is selected and no direct DeepSeek Harness token is
     configured (or when `free_gateway` mode is chosen), transparently routes to
     `opencode/deepseek-v4.1-flash-free` so it works out-of-the-box with zero auth.
2. Direct DeepSeek Harness 1M-context integration (`https://api.deepseek.com/anthropic/v1`):
   - Reads existing credentials from `~/.config/openswarm/dsh_account.json`, environment variables
     (`DEEPSEEK_DSH_TOKEN`, `DEEPSEEK_API_KEY`), or the official `@deepseek-ai/dsh` CLI store
     (`~/.dsh/.credentials.yaml`).
   - Supports the official DeepSeek Harness PKCE Browser OAuth flow
     (`https://platform.deepseek.com/auth-api/v0/dsh/auth_init` & `auth_exchange`).
   - Injects the `deepseek-harness` provider into `OPENCODE_CONFIG_CONTENT` with the exact headers
     required by `deepseek-harness` (`User-Agent`, `x-deepseek-harness-user-id`, `x-dsh-auth-token`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml

DSH_VERSION = "0.2.1-rc.2"
DSH_USER_AGENT = f"deepseek-harness/{DSH_VERSION} (+https://github.com/deepseek-ai/deepseek-harness)"
DSH_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DSH_OPENAI_BASE_URL = "https://api.deepseek.com/v1"
DSH_PLATFORM_BASE_URL = "https://platform.deepseek.com"
DSH_PROVIDER_ID = "deepseek-harness"
DSH_FREE_FALLBACK_MODEL = "kilo/nvidia/nemotron-3.5-super-vl:free"

# Resilient cascade of free-tier models when IP quota or rate limits hit:
DSH_FREE_MODELS_CASCADE: list[str] = [
    "opencode/deepseek-v4.1-flash-free",
    "kilo/kilo-auto/free",
    "kilo/nvidia/nemotron-3.5-super-vl:free",
    "kilo/cohere/north-mini-code:free",
    "kilo/reflection/beam-free",
    "openrouter/openrouter/free",
]

# Track when OpenCode's zen/v1 free quota is exhausted so we bypass it immediately:
_OPENCODE_FREE_LIMITED_UNTIL: float = 0.0


def mark_opencode_free_limited(cooldown_s: float = 1800.0) -> None:
    """Mark OpenCode's built-in `opencode/*-free` models as quota-limited so subsequent calls bypass them."""
    global _OPENCODE_FREE_LIMITED_UNTIL
    _OPENCODE_FREE_LIMITED_UNTIL = time.time() + cooldown_s


def is_opencode_free_limited() -> bool:
    """Return True if OpenCode's built-in `opencode/*-free` models recently hit `Free usage limit reached`."""
    return time.time() < _OPENCODE_FREE_LIMITED_UNTIL


def is_opencode_free_model(model: str) -> bool:
    """Return True if `model` is one of OpenCode's built-in `opencode/*` free models."""
    raw = (model or "").split("#", 1)[0].strip().lower()
    return raw.startswith("opencode/") and (raw.endswith("-free") or raw == "opencode/big-pickle")

DSH_MODELS: dict[str, dict[str, Any]] = {
    "deepseek-flash": {
        "name": "DeepSeek-V4.1-Flash (DeepSeek Harness 1M)",
        "limit": {"context": 1048576, "output": 256000},
    },
    "deepseek-v4-pro": {
        "name": "DeepSeek-V4-Pro (DeepSeek Harness 1M)",
        "limit": {"context": 1048576, "output": 256000},
    },
    "deepseek-chat": {
        "name": "DeepSeek-Chat (DeepSeek V3)",
        "limit": {"context": 128000, "output": 8192},
    },
    "deepseek-reasoner": {
        "name": "DeepSeek-Reasoner (DeepSeek R1)",
        "limit": {"context": 128000, "output": 8192},
    },
}


def dsh_state_file() -> Path:
    """Where OpenSwarm stores DeepSeek Harness settings and OAuth tokens."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "openswarm" / "dsh_account.json"


def official_dsh_dir() -> Path:
    """Official `@deepseek-ai/dsh` CLI state directory (`~/.dsh`)."""
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def _read_state() -> dict[str, Any]:
    path = dsh_state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(data: dict[str, Any]) -> None:
    path = dsh_state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def anonymous_user_id() -> str:
    """Return the persistent anonymous user UUID expected by DeepSeek Harness headers."""
    anon_file = official_dsh_dir() / ".anonymous-user-id"
    try:
        if anon_file.exists():
            val = anon_file.read_text(encoding="utf-8").strip()
            if val:
                return val
    except OSError:
        pass
    st = _read_state()
    uid = str(st.get("anonymous_user_id") or "").strip()
    if not uid:
        uid = str(uuid.uuid4())
        st["anonymous_user_id"] = uid
        try:
            _write_state(st)
        except OSError:
            pass
    return uid


def _read_official_dsh_credentials() -> tuple[str, str]:
    """Read `(token, kind)` from `~/.dsh/.credentials.yaml` if `@deepseek-ai/dsh` is configured."""
    cred_path = official_dsh_dir() / ".credentials.yaml"
    try:
        if not cred_path.exists():
            return "", ""
        raw = yaml.safe_load(cred_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return "", ""
        creds = raw.get("credentials") or {}
        if not isinstance(creds, dict):
            return "", ""
        acct = creds.get("deepseek-account-platform/default")
        if isinstance(acct, dict) and acct.get("value"):
            return str(acct["value"]).strip(), "account"
        apikey = creds.get("llm-deepseek/default")
        if isinstance(apikey, dict) and apikey.get("value"):
            return str(apikey["value"]).strip(), "apikey"
    except (OSError, yaml.YAMLError, ValueError):
        pass
    return "", ""


def mask_token(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    if len(t) <= 10:
        return "…" + t[-3:]
    return f"{t[:6]}…{t[-4:]}"


def load_dsh_credentials() -> dict[str, Any]:
    """Return current DeepSeek Harness credentials and routing mode."""
    st = _read_state()
    mode = str(st.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "free_gateway", "direct_dsh"):
        mode = "auto"

    # 1. Explicit OpenSwarm stored token
    tok = str(st.get("token") or "").strip()
    kind = str(st.get("kind") or "account").strip().lower()
    if tok:
        return {
            "token": tok,
            "kind": "apikey" if kind == "apikey" else "account",
            "source": str(st.get("source") or "openswarm"),
            "mode": mode,
            "updated_at": st.get("updated_at") or 0,
        }

    # 2. Environment variables
    env_dsh = os.environ.get("DEEPSEEK_DSH_TOKEN", "").strip()
    if env_dsh:
        return {"token": env_dsh, "kind": "account", "source": "env:DEEPSEEK_DSH_TOKEN", "mode": mode, "updated_at": 0}
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return {"token": env_key, "kind": "apikey", "source": "env:DEEPSEEK_API_KEY", "mode": mode, "updated_at": 0}

    # 3. Official `~/.dsh/.credentials.yaml`
    cli_tok, cli_kind = _read_official_dsh_credentials()
    if cli_tok:
        return {"token": cli_tok, "kind": cli_kind, "source": "~/.dsh/.credentials.yaml", "mode": mode, "updated_at": 0}

    return {"token": "", "kind": "", "source": "none", "mode": mode, "updated_at": 0}


def save_dsh_credentials(
    token: str | None = None,
    *,
    kind: str = "auto",
    mode: str | None = None,
    source: str = "app",
    sync_cli: bool = True,
) -> dict[str, Any]:
    """Save DeepSeek Harness token and/or routing mode."""
    st = _read_state()
    if token is not None:
        tok = token.strip()
        if not tok:
            st.pop("token", None)
            st.pop("kind", None)
            st.pop("source", None)
        else:
            if kind == "auto":
                # DeepSeek platform API keys start with sk-; OAuth DSH tokens are longer or non-sk
                resolved_kind = "apikey" if (tok.startswith("sk-") and len(tok) <= 64) else "account"
            else:
                resolved_kind = "apikey" if kind == "apikey" else "account"
            st["token"] = tok
            st["kind"] = resolved_kind
            st["source"] = source
            st["updated_at"] = int(time.time())
            if sync_cli:
                _sync_to_official_dsh(tok, resolved_kind)
    if mode is not None and mode in ("auto", "free_gateway", "direct_dsh"):
        st["mode"] = mode
    _write_state(st)
    return dsh_status()


def clear_dsh_credentials() -> dict[str, Any]:
    """Clear stored DeepSeek Harness token from OpenSwarm state and synced CLI store."""
    st = _read_state()
    st.pop("token", None)
    st.pop("kind", None)
    st.pop("source", None)
    st.pop("pending_pkce", None)
    _write_state(st)
    try:
        cred_path = official_dsh_dir() / ".credentials.yaml"
        if cred_path.exists():
            raw = yaml.safe_load(cred_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
                raw["credentials"].pop("deepseek-account-platform/default", None)
                raw["credentials"].pop("llm-deepseek/default", None)
                cred_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    except OSError:
        pass
    return dsh_status()


def _sync_to_official_dsh(token: str, kind: str) -> None:
    """Optionally sync credentials to `~/.dsh/.credentials.yaml` for `@deepseek-ai/dsh` CLI compatibility."""
    try:
        dsh_dir = official_dsh_dir()
        dsh_dir.mkdir(parents=True, exist_ok=True)
        cred_path = dsh_dir / ".credentials.yaml"
        raw: dict[str, Any] = {}
        if cred_path.exists():
            loaded = yaml.safe_load(cred_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        raw.setdefault("version", 1)
        creds = raw.setdefault("credentials", {})
        slot = "llm-deepseek/default" if kind == "apikey" else "deepseek-account-platform/default"
        creds[slot] = {"value": token, "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        fd, tmp = tempfile.mkstemp(dir=str(dsh_dir), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
        os.replace(tmp, cred_path)
    except OSError:
        pass


def is_dsh_model(model: str) -> bool:
    """True when `model` belongs to the `deepseek-harness/` provider."""
    if not model:
        return False
    raw = model.split("#", 1)[0].strip().lower()
    return raw.startswith(f"{DSH_PROVIDER_ID}/")


def has_direct_dsh_auth() -> bool:
    """True when a direct DeepSeek Harness token/key is configured and mode is not forced to free_gateway."""
    creds = load_dsh_credentials()
    if creds.get("mode") == "free_gateway":
        return False
    return bool(creds.get("token"))


def resolve_dsh_model(model: str) -> str:
    """Resolve `deepseek-harness/*` (or quota-limited `opencode/*-free`) to a working model."""
    if is_opencode_free_model(model) and is_opencode_free_limited():
        return DSH_FREE_FALLBACK_MODEL
    if not is_dsh_model(model):
        return model
    if has_direct_dsh_auth():
        return model
    # Zero-config fallback: use verified working 1M-context free gateway model
    return DSH_FREE_FALLBACK_MODEL


def dsh_provider_config() -> dict[str, Any] | None:
    """Build the OpenCode provider dictionary for `deepseek-harness` when direct credentials exist."""
    creds = load_dsh_credentials()
    token = str(creds.get("token") or "").strip()
    if not token or creds.get("mode") == "free_gateway":
        return None
    kind = str(creds.get("kind") or "account").lower()
    if kind == "apikey":
        # DeepSeek Platform API key (sk-...) -> OpenAI-compatible API at https://api.deepseek.com/v1
        return {
            "npm": "@ai-sdk/openai-compatible",
            "name": "DeepSeek Platform API",
            "options": {
                "baseURL": DSH_OPENAI_BASE_URL,
                "apiKey": token,
            },
            "models": DSH_MODELS,
        }
    headers: dict[str, str] = {
        "User-Agent": DSH_USER_AGENT,
        "x-deepseek-harness-user-id": anonymous_user_id(),
        "x-dsh-auth-token": token,
    }
    return {
        "npm": "@ai-sdk/anthropic",
        "name": "DeepSeek Harness",
        "options": {
            "baseURL": DSH_ANTHROPIC_BASE_URL,
            "apiKey": token,
            "headers": headers,
        },
        "models": DSH_MODELS,
    }


def next_free_fallback_model(current_model: str, tried: set[str] | None = None) -> str | None:
    """Return the next available healthy free-tier model in the cascade when quota or rate limits hit."""
    tried_set = set(tried or ())
    oc_limited = is_opencode_free_limited()
    for cand in DSH_FREE_MODELS_CASCADE:
        if oc_limited and is_opencode_free_model(cand):
            continue
        if cand not in tried_set and cand != current_model:
            return cand
    return None


def dsh_status() -> dict[str, Any]:
    """Return full status dictionary for the Web UI and CLI."""
    creds = load_dsh_credentials()
    tok = str(creds.get("token") or "")
    direct_active = bool(tok) and creds.get("mode") != "free_gateway"
    active_route = (
        "direct_dsh_1m"
        if direct_active
        else "opencode_free_unlimited"
    )
    return {
        "ok": True,
        "version": DSH_VERSION,
        "has_token": bool(tok),
        "masked_token": mask_token(tok),
        "kind": creds.get("kind") or "",
        "source": creds.get("source") or "none",
        "mode": creds.get("mode") or "auto",
        "active_route": active_route,
        "effective_model": "deepseek-harness/deepseek-flash" if direct_active else DSH_FREE_FALLBACK_MODEL,
        "models": [
            {
                "id": "deepseek-harness/deepseek-flash",
                "label": "DeepSeek-V4.1-Flash · DeepSeek Harness (Free Unlimited / 1M ctx)",
                "context_window": 1048576,
                "max_output": 256000,
                "free": True,
            },
            {
                "id": "deepseek-harness/deepseek-v4-pro",
                "label": "DeepSeek-V4-Pro · DeepSeek Harness (1M ctx)",
                "context_window": 1048576,
                "max_output": 256000,
                "free": False,
            },
        ],
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _dsh_platform_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": DSH_USER_AGENT,
        "x-client-bundle-id": "",
        "x-client-platform": "web",
        "x-client-version": DSH_VERSION,
        "x-client-locale": "en_US",
        "x-client-timezone-offset": str(int(-time.timezone // 60)),
    }


def start_pkce_signin(callback_origin: str) -> dict[str, Any]:
    """Initiate the official DeepSeek Harness PKCE Browser OAuth flow and return `authorize_url`."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    redirect_uri = f"{callback_origin.rstrip('/')}/oauth/callback"

    payload = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "locale": "en_US",
        "login_source": "web",
    }
    req = urllib.request.Request(
        f"{DSH_PLATFORM_BASE_URL}/auth-api/v0/dsh/auth_init",
        data=json.dumps(payload).encode("utf-8"),
        headers=_dsh_platform_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": f"DeepSeek auth_init failed: {exc}"}

    biz = (body.get("data") or {}).get("biz_data") or {}
    authorize_url = str(biz.get("authorize_url") or "").strip()
    if body.get("code") != 0 or not authorize_url:
        msg = body.get("msg") or (body.get("data") or {}).get("biz_msg") or "Unknown response from platform.deepseek.com"
        return {"ok": False, "error": f"DeepSeek auth_init rejected: {msg}"}

    st = _read_state()
    st["pending_pkce"] = {
        "state": state,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": int(time.time()),
    }
    _write_state(st)
    return {
        "ok": True,
        "authorize_url": authorize_url,
        "state": state,
        "redirect_uri": redirect_uri,
    }


def complete_pkce_signin(code: str, state: str) -> dict[str, Any]:
    """Exchange the OAuth callback `code` for a DeepSeek Harness `x-dsh-auth-token`."""
    code = (code or "").strip()
    state = (state or "").strip()
    if not code or not state:
        return {"ok": False, "error": "Missing OAuth code or state parameter"}

    st = _read_state()
    pending = st.get("pending_pkce") or {}
    if not isinstance(pending, dict) or pending.get("state") != state:
        return {"ok": False, "error": "PKCE state mismatch or expired login session"}

    payload = {
        "code": code,
        "code_verifier": str(pending.get("code_verifier") or ""),
        "redirect_uri": str(pending.get("redirect_uri") or ""),
        "device_id": anonymous_user_id(),
        "device_model": f"{sys.platform}-{platform.machine()}",
        "os_version": f"{platform.system().lower()} {platform.release()}",
    }
    req = urllib.request.Request(
        f"{DSH_PLATFORM_BASE_URL}/auth-api/v0/dsh/auth_exchange",
        data=json.dumps(payload).encode("utf-8"),
        headers=_dsh_platform_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": f"DeepSeek auth_exchange failed: {exc}"}

    biz = (body.get("data") or {}).get("biz_data") or {}
    token = str(biz.get("token") or "").strip()
    if body.get("code") != 0 or not token:
        msg = body.get("msg") or (body.get("data") or {}).get("biz_msg") or "Failed to exchange OAuth code"
        return {"ok": False, "error": f"DeepSeek auth_exchange rejected: {msg}"}

    st.pop("pending_pkce", None)
    _write_state(st)
    status = save_dsh_credentials(token, kind="account", mode="auto", source="deepseek-oauth", sync_cli=True)
    return {"ok": True, "status": status}
