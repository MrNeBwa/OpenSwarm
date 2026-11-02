"""Token Saver & Context Optimizer for OpenSwarm.

Provides intelligent context reduction, command output trimming,
history summarization, and prompt token minimization to save API quotas
and reduce latency across free and commercial model tiers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("openswarm.optimizer")


@dataclass
class OptimizationStats:
    total_original_chars: int = 0
    total_compressed_chars: int = 0
    cache_hits: int = 0
    summaries_created: int = 0

    @property
    def saved_chars(self) -> int:
        return max(0, self.total_original_chars - self.total_compressed_chars)

    @property
    def savings_percent(self) -> float:
        if self.total_original_chars <= 0:
            return 0.0
        return round((self.saved_chars / self.total_original_chars) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_chars": self.total_original_chars,
            "compressed_chars": self.total_compressed_chars,
            "saved_chars": self.saved_chars,
            "savings_percent": self.savings_percent,
            "cache_hits": self.cache_hits,
            "summaries_created": self.summaries_created,
            "cache_size": len(_CACHE),
        }


# Global memory cache for prompt responses (TTL = 15 minutes)
_CACHE: dict[str, tuple[float, str]] = {}
_STATS = OptimizationStats()


def reset_optimizer_stats() -> None:
    global _STATS, _CACHE
    _STATS = OptimizationStats()
    _CACHE.clear()


def get_optimizer_stats() -> dict[str, Any]:
    return _STATS.to_dict()


def compress_text(text: str, max_head: int = 25, max_tail: int = 25) -> str:
    """Collapses repetitive whitespace, blank lines, and truncates huge text bodies cleanly."""
    if not text:
        return ""
    orig_len = len(text)
    
    # 1. Normalize line endings and collapse 3+ consecutive newlines to 2
    t = re.sub(r"\r\n|\r", "\n", text)
    t = re.sub(r"\n{3,}", "\n\n", t)
    
    # 2. Trim trailing whitespace on each line
    lines = [ln.rstrip() for ln in t.splitlines()]
    
    # 3. If line count is very large (e.g. huge terminal logs, git diffs, directory trees)
    if len(lines) > (max_head + max_tail + 10):
        head = lines[:max_head]
        tail = lines[-max_tail:]
        omitted = len(lines) - (max_head + max_tail)
        lines = head + [f"\n[... {omitted} строк лога/вывода скрыто для экономии токенов ...]\n"] + tail
        t = "\n".join(lines)
    else:
        t = "\n".join(lines)
        
    _STATS.total_original_chars += orig_len
    _STATS.total_compressed_chars += len(t)
    return t


def prune_history_context(messages: list[dict[str, str]], max_verbatim_turns: int = 4) -> str:
    """Smartly prunes multi-turn history: keeps recent turns verbatim, summarizes older ones."""
    if not messages:
        return ""
    if len(messages) <= max_verbatim_turns:
        parts = []
        for m in messages:
            role = m.get("role", "user")
            content = (m.get("content", "") or "").strip()
            if content:
                label = "Пользователь" if role == "user" else "Ассистент"
                parts.append(f"{label}: {content}")
        return "\n\n".join(parts)

    older = messages[:-max_verbatim_turns]
    recent = messages[-max_verbatim_turns:]
    _STATS.summaries_created += 1

    summary_bullets = []
    for m in older:
        role = m.get("role", "user")
        content = (m.get("content", "") or "").strip()
        if not content:
            continue
        first_line = content.splitlines()[0][:100]
        if role == "user":
            summary_bullets.append(f"- Запрос пользователя: {first_line}")
        else:
            summary_bullets.append(f"- Ответ ассистента: {first_line}")

    older_summary = "### Краткий контекст предыдущих сообщений:\n" + "\n".join(summary_bullets[:8])
    recent_parts = []
    for m in recent:
        role = m.get("role", "user")
        content = (m.get("content", "") or "").strip()
        label = "Пользователь" if role == "user" else "Ассистент"
        recent_parts.append(f"{label}: {content}")

    return older_summary + "\n\n### Текущий диалог:\n" + "\n\n".join(recent_parts)


def make_cache_key(model: str, prompt: str, system: str = "") -> str:
    raw = f"{model}:::{system}:::{prompt}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def check_cache(key: str, ttl_seconds: float = 900.0) -> str | None:
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts <= ttl_seconds:
            _STATS.cache_hits += 1
            return val
        del _CACHE[key]
    return None


def store_cache(key: str, val: str) -> None:
    # Cap cache size to 256 entries
    if len(_CACHE) > 256:
        oldest = min(_CACHE.keys(), key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]
    _CACHE[key] = (time.time(), val)


def optimize_prompt_for_model(prompt: str, role: str = "chat", aggressive: bool = False) -> str:
    """Trims redundant boilerplate and applies token optimization."""
    p = prompt.strip()
    if not p:
        return ""
    # Remove repeated instruction lines if user or workflow appended them multiple times
    lines = p.splitlines()
    seen = set()
    deduped = []
    for line in lines:
        s = line.strip()
        # Keep empty lines and markdown headers
        if not s or s.startswith("#") or s.startswith("```"):
            deduped.append(line)
            continue
        if s.startswith("[Инструкция") and s in seen:
            continue
        seen.add(s)
        deduped.append(line)
        
    res = "\n".join(deduped)
    return compress_text(res, max_head=35 if aggressive else 50, max_tail=35 if aggressive else 50)
