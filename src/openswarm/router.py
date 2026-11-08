"""Smart Model Router, Priority Profiles & Live Model Health Telemetry for OpenSwarm.

Evaluates available models across reasoning, coding, speed, cost (free vs paid), and live runtime health
(successes, failures, latency, cooldowns) to automatically pick the best model per role and perform
automatic mid-run failover when a model is unavailable, broken, or rate-limited.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import ROLES, SwarmConfig

PRIORITIES: tuple[tuple[str, str, str, str], ...] = (
    ("balanced", "⚖️", "Balanced", "Optimal mix of reasoning depth, coding accuracy, and speed"),
    ("quality", "💎", "Max Quality", "Top-tier frontier reasoning & coding models for maximum accuracy"),
    ("fast", "⚡", "Fast / Turbo", "Low-latency Flash / Mini models for rapid iteration"),
    ("free", "🆓", "Free Only", "Strictly zero-cost models paired with automatic VPN & key rotation"),
    ("local", "🏠", "Local / Offline", "Prefer local Ollama / LM Studio models first, then free models"),
)

VALID_PRIORITIES = {p[0] for p in PRIORITIES}


def priority_list() -> list[dict[str, str]]:
    return [{"id": pid, "icon": icon, "label": label, "desc": desc} for pid, icon, label, desc in PRIORITIES]


def health_state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "openswarm" / "model_health.json"


def is_free_model(model_id: str, spec_free: bool = False) -> bool:
    if spec_free:
        return True
    m = model_id.lower()
    if m == "deepseek-harness/deepseek-flash" or m.startswith("kilo/"):
        return True
    if m.startswith("google/"):
        return True
    return "free" in m or m.endswith(":free") or (m.startswith("opencode/") and any(k in m for k in ("free", "nano", "pickle", "flash")))


def is_local_model(model_id: str) -> bool:
    m = model_id.lower()
    return m.startswith(("ollama/", "lmstudio/", "local/", "llamacpp/", "vllm/"))


def score_model_capabilities(model_id: str, spec_free: bool = False) -> dict[str, Any]:
    """Static heuristic capability profile (0..100) for any model ID."""
    m = model_id.lower()
    free = is_free_model(model_id, spec_free)
    local = is_local_model(model_id)

    reasoning = 68
    coding = 68
    speed = 68

    # OpenCode Google Gemini native models
    if "gemini-3.5-flash" in m or "gemini-3.5" in m:
        reasoning, coding, speed = 94, 96, 96
    elif "gemini-3.1-flash-lite" in m or "gemini-flash-lite" in m:
        reasoning, coding, speed = 91, 92, 98
    elif "gemini-3-flash" in m or "gemini-flash" in m:
        reasoning, coding, speed = 93, 94, 94
    elif "gemini-pro" in m:
        reasoning, coding, speed = 97, 96, 75
    # Specific OpenCode & DeepSeek Harness native models
    elif "beam-free" in m:
        reasoning, coding, speed = 93, 89, 85
    elif "deepseek-harness/deepseek-flash" in m or "deepseek-v4.1-flash" in m or "deepseek-v4" in m:
        reasoning, coding, speed = 90, 96, 94
    elif "nemotron-3.5-super-vl" in m or "nemotron-3.5" in m:
        reasoning, coding, speed = 91, 90, 86
    elif "nemotron-3.5-lightning" in m:
        reasoning, coding, speed = 83, 85, 96
    elif "flint-40b" in m:
        reasoning, coding, speed = 86, 87, 84
    elif "mimo-v2" in m:
        reasoning, coding, speed = 84, 86, 92
    elif "glm-5" in m:
        reasoning, coding, speed = 85, 87, 90
    elif "big-pickle" in m:
        reasoning, coding, speed = 86, 88, 83
    elif "nemotron-3-ultra" in m:
        reasoning, coding, speed = 89, 87, 81
    elif "laguna-s" in m or "north-mini-code" in m:
        reasoning, coding, speed = 87, 93, 91
    elif "kilo-auto" in m or "flint-v1" in m:
        reasoning, coding, speed = 88, 89, 88
    # High-end frontier reasoning & coding models
    elif any(k in m for k in ("claude-opus", "opus-4", "gpt-5", "o3", "o1-pro", "gemini-2.5-pro", "gemini-3")):
        reasoning, coding, speed = 96, 95, 58
    elif any(k in m for k in ("claude-sonnet-4", "sonnet-4", "claude-3-7-sonnet", "claude-3.7-sonnet", "gpt-4.1", "o4-mini")):
        reasoning, coding, speed = 92, 94, 74
    elif any(k in m for k in ("deepseek-r1", "qwq", "thinking", "reasoner")):
        reasoning, coding, speed = 91, 86, 62
    elif any(k in m for k in ("qwen3-coder", "qwen-2.5-coder", "qwen2.5-coder", "codestral", "devstral", "grok-code")):
        reasoning, coding, speed = 82, 92, 78
    elif any(k in m for k in ("qwen3", "kimi-k2", "minimax-m2")):
        reasoning, coding, speed = 86, 88, 76
    elif any(k in m for k in ("gemini-2.5-flash", "gemini-2.0-flash", "claude-haiku", "gpt-4o-mini", "gpt-4.1-mini")):
        reasoning, coding, speed = 78, 80, 93

    # Speed modifiers
    if any(k in m for k in ("flash", "fast", "turbo", "mini", "nano", "haiku", "instant", "lite")):
        speed = min(98, speed + 14)
    if any(k in m for k in ("7b", "8b", "3b", "1.5b")):
        speed = min(96, speed + 10)
        reasoning = max(50, reasoning - 10)
        coding = max(52, coding - 8)

    # Provider reliability bonus: native opencode & google get highest priority among accessible models
    if m.startswith(("google/", "opencode/", "ollama/")):
        coding = min(98, coding + 4)
        reasoning = min(95, reasoning + 3)
        speed = min(98, speed + 4)
    elif m.startswith(("deepseek-harness/", "kilo/")):
        coding = min(94, coding + (3 if free else 1))
        reasoning = min(92, reasoning + (3 if free else 1))
        speed = min(94, speed + 2)

    tier = "S" if max(reasoning, coding) >= 92 else ("A" if max(reasoning, coding) >= 84 else ("B" if max(reasoning, coding) >= 74 else "C"))
    return {
        "id": model_id,
        "reasoning": reasoning,
        "coding": coding,
        "speed": speed,
        "free": free,
        "local": local,
        "tier": tier,
    }


class ModelRouter:
    """Tracks model health and computes priority-aware rankings for each swarm role."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self.stats: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        p = health_state_path()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.stats = raw
        except (OSError, ValueError):
            self.stats = {}

    def _save(self) -> None:
        p = health_state_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.stats, fh, indent=1)
            os.replace(tmp, p)
        except OSError:
            pass

    def record_success(self, model_id: str, latency_s: float = 0.0) -> None:
        if not model_id:
            return
        now = self._clock()
        st = self.stats.setdefault(model_id, {"ok": 0, "fail": 0, "avg_s": 0.0, "cooldown_until": 0.0, "last_error": "", "streak_fail": 0})
        st["ok"] = int(st.get("ok", 0)) + 1
        st["streak_fail"] = 0
        st["cooldown_until"] = 0.0
        st["last_error"] = ""
        st["last_ok"] = round(now, 1)
        if latency_s > 0:
            prev = float(st.get("avg_s") or latency_s)
            st["avg_s"] = round(prev * 0.7 + latency_s * 0.3, 2)
        self._save()

    def record_failure(self, model_id: str, reason: str, kind: str = "transient") -> None:
        if not model_id:
            return
        now = self._clock()
        st = self.stats.setdefault(model_id, {"ok": 0, "fail": 0, "avg_s": 0.0, "cooldown_until": 0.0, "last_error": "", "streak_fail": 0})
        st["fail"] = int(st.get("fail", 0)) + 1
        streak = int(st.get("streak_fail", 0)) + 1
        st["streak_fail"] = streak
        st["last_error"] = (reason or kind)[:200]
        st["last_fail"] = round(now, 1)
        if kind in ("model_broken", "auth_error", "not_found"):
            cooldown = 1800
        elif kind == "rate_limit":
            cooldown = min(600, 90 * streak)
        else:
            cooldown = min(300, 45 * streak)
        st["cooldown_until"] = round(now + cooldown, 1)
        self._save()

    def clear_cooldowns(self) -> None:
        for st in self.stats.values():
            st["cooldown_until"] = 0.0
            st["streak_fail"] = 0
            st["last_error"] = ""
        self._save()

    def health_info(self, model_id: str) -> dict[str, Any]:
        now = self._clock()
        st = self.stats.get(model_id) or {}
        cd = max(0, round(float(st.get("cooldown_until", 0.0)) - now))
        return {
            "ok": int(st.get("ok", 0)),
            "fail": int(st.get("fail", 0)),
            "avg_s": float(st.get("avg_s", 0.0)),
            "cooling_seconds": cd,
            "healthy": cd == 0,
            "streak_fail": int(st.get("streak_fail", 0)),
            "last_error": str(st.get("last_error", "")),
        }

    def score_for_role(self, model_id: str, spec_free: bool, role: str, priority: str = "balanced", has_or_keys: bool = False) -> float:
        cap = score_model_capabilities(model_id, spec_free)
        hlth = self.health_info(model_id)
        priority = priority if priority in VALID_PRIORITIES else "balanced"

        # Base role weights
        if role in ("planner", "architect", "judge", "moderator", "synthesizer", "research_planner"):
            base = cap["reasoning"] * 0.60 + cap["coding"] * 0.25 + cap["speed"] * 0.15
        elif role in ("reviewer", "lens", "researcher", "drafter", "outliner", "editor"):
            base = cap["reasoning"] * 0.45 + cap["coding"] * 0.25 + cap["speed"] * 0.30
        else:  # coder / worker / chat
            base = cap["coding"] * 0.60 + cap["reasoning"] * 0.25 + cap["speed"] * 0.15

        # Priority adjustments
        if priority == "quality":
            score = cap["reasoning"] * 0.50 + cap["coding"] * 0.45 + cap["speed"] * 0.05
        elif priority == "fast":
            score = cap["speed"] * 0.65 + base * 0.35
        elif priority == "free":
            score = base + (35.0 if cap["free"] else -120.0)
        elif priority == "local":
            score = base + (60.0 if cap["local"] else (15.0 if cap["free"] else -40.0))
        else:  # balanced
            score = base + (8.0 if cap["free"] else 0.0)

        # Penalize paid external providers if unconfigured
        m_low = model_id.lower()
        if m_low.startswith("openrouter/") and not has_or_keys and not cap["free"]:
            score -= 50.0
        if m_low.startswith(("anthropic/", "openai/")) and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            # If user hasn't set direct API keys, slightly prefer native opencode/* models in balanced/free modes
            if priority in ("balanced", "free", "fast"):
                score -= 22.0

        # Live telemetry adjustments
        from .dsh import is_opencode_free_limited, is_opencode_free_model
        if is_opencode_free_limited() and is_opencode_free_model(model_id):
            score -= 90.0
        if hlth["ok"] > 0:
            score += min(8.0, hlth["ok"] * 1.2)
        if hlth["avg_s"] > 0 and priority == "fast":
            score += max(-10.0, 10.0 - hlth["avg_s"] * 0.4)
        if hlth["streak_fail"] > 0:
            score -= min(45.0, hlth["streak_fail"] * 12.0)
        if not hlth["healthy"]:
            score -= 80.0

        return round(score, 2)

    def rank_models(
        self,
        cfg: SwarmConfig,
        role: str = "coder",
        priority: str | None = None,
        exclude: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        from .keys import configured_keys

        prio = (priority or getattr(cfg, "priority", "balanced") or "balanced").lower()
        has_keys = bool(configured_keys(cfg))
        excl = exclude or set()
        rows: list[dict[str, Any]] = []
        for m in cfg.models:
            if m.id in excl:
                continue
            cap = score_model_capabilities(m.id, m.free)
            hlth = self.health_info(m.id)
            sc = self.score_for_role(m.id, m.free, role, prio, has_keys)
            rows.append({
                "id": m.id,
                "label": m.display(),
                "score": sc,
                **cap,
                **hlth,
            })
        rows.sort(key=lambda r: (-r["score"], r["cooling_seconds"], r["id"]))
        return rows

    def pick_best_for_role(
        self,
        cfg: SwarmConfig,
        role: str = "coder",
        priority: str | None = None,
        exclude: set[str] | None = None,
    ) -> str:
        ranked = self.rank_models(cfg, role=role, priority=priority, exclude=exclude)
        if not ranked:
            return getattr(cfg.roles, role if role in ROLES else "coder", cfg.roles.coder)
        # Prefer healthy models if any exist
        healthy = [r for r in ranked if r["healthy"]]
        chosen = healthy[0] if healthy else ranked[0]
        return str(chosen["id"])

    def recommend_roles(self, cfg: SwarmConfig, priority: str | None = None) -> dict[str, str]:
        prio = (priority or getattr(cfg, "priority", "balanced") or "balanced").lower()
        coder = self.pick_best_for_role(cfg, "coder", prio)
        planner = self.pick_best_for_role(cfg, "planner", prio)
        if planner == coder and len(cfg.models) > 1 and prio != "quality":
            alt_plan = self.pick_best_for_role(cfg, "planner", prio, exclude={coder})
            if alt_plan:
                planner = alt_plan
        reviewer = self.pick_best_for_role(cfg, "reviewer", prio)
        # Give reviewer a distinct perspective from coder and planner when multiple strong models exist
        if reviewer in (coder, planner) and len(cfg.models) > 2:
            alt = self.pick_best_for_role(cfg, "reviewer", prio, exclude={coder, planner})
            if alt:
                reviewer = alt
        elif reviewer == coder and len(cfg.models) > 1:
            alt = self.pick_best_for_role(cfg, "reviewer", prio, exclude={coder})
            if alt:
                reviewer = alt
        return {
            "planner": planner,
            "architect": planner,
            "coder": coder,
            "reviewer": reviewer,
        }

    def fallback_model(
        self,
        cfg: SwarmConfig,
        role: str,
        failed_model: str,
        priority: str | None = None,
        tried: set[str] | None = None,
    ) -> str | None:
        excl = set(tried or set()) | {failed_model}
        ranked = self.rank_models(cfg, role=role, priority=priority, exclude=excl)
        healthy = [r for r in ranked if r["healthy"]]
        if healthy:
            return str(healthy[0]["id"])
        return str(ranked[0]["id"]) if ranked else None


_ROUTER: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = ModelRouter()
    return _ROUTER


def reset_router() -> None:
    global _ROUTER
    _ROUTER = None
