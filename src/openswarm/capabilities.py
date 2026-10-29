"""Which flags does the installed `opencode` really support? Detected from `opencode run --help` (cached),
and refined at run time when a flag is rejected ("Unrecognized flag: --x")."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .config import OpencodeSettings, SwarmConfig

_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_CACHE: dict[str, "Caps"] = {}


@dataclass
class Caps:
    flags: set[str] = field(default_factory=set)
    parsed: bool = False  # False: help could not be read -> keep legacy defaults, never add optional flags

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def attach_flag(self, oc: OpencodeSettings) -> str:
        """Flag for connecting to a specific server: --attach (older) or --server (newer). '' = unsupported."""
        if not self.parsed:
            return oc.attach_flag
        return next((f for f in (oc.attach_flag, oc.server_flag) if f and f in self.flags), "")

    def can_auto(self, oc: OpencodeSettings) -> bool:
        return self.parsed and bool(oc.auto_flag) and oc.auto_flag in self.flags

    def can_standalone(self, oc: OpencodeSettings) -> bool:
        return self.parsed and bool(oc.standalone_flag) and oc.standalone_flag in self.flags

    def can_variant(self, oc: OpencodeSettings) -> bool:
        vf = getattr(oc, "variant_flag", "")
        return self.parsed and bool(vf) and vf in self.flags

    def variant_flag(self, oc: OpencodeSettings) -> str:
        vf = getattr(oc, "variant_flag", "")
        return vf if self.can_variant(oc) else ""

    def can_thinking(self, oc: OpencodeSettings) -> bool:
        tf = getattr(oc, "thinking_flag", "")
        return self.parsed and bool(tf) and tf in self.flags

    def thinking_flag(self, oc: OpencodeSettings) -> str:
        tf = getattr(oc, "thinking_flag", "")
        return tf if self.can_thinking(oc) else ""

    def disable(self, flag: str) -> None:
        self.flags.discard(flag)
        self.parsed = True if self.flags else self.parsed


def parse_help(text: str) -> Caps:
    flags = set(_FLAG.findall(text))
    return Caps(flags=flags, parsed=("--model" in flags or "--session" in flags or "--continue" in flags))


def probe(cfg: SwarmConfig) -> Caps:
    oc = cfg.opencode
    binary = shutil.which(oc.binary) or oc.binary
    if binary in _CACHE:
        return _CACHE[binary]
    try:
        r = subprocess.run([binary, *oc.subcommand, "--help"], capture_output=True, text=True, timeout=20)
        caps = parse_help(r.stdout + r.stderr)
    except (OSError, subprocess.SubprocessError):
        caps = Caps()
    _CACHE[binary] = caps
    return caps


async def get_caps(cfg: SwarmConfig) -> Caps:
    return await asyncio.to_thread(probe, cfg)


def forget_cache() -> None:
    _CACHE.clear()


_REJECTED = re.compile(r"(?:unrecognized|unknown)\s+(?:flag|option|argument)s?:?\s*['\"]?(--[\w-]+)", re.I)


def rejected_flag(output: str) -> str:
    """The flag named in an 'Unrecognized flag: --x' style error, or ''."""
    m = _REJECTED.search(output)
    return m.group(1) if m else ""


# Models known to NOT support reasoning variants / effort in OpenCode
_KNOWN_NON_VARIANT_PATTERNS = (
    "deepseek-v4.1-flash",
    "deepseek-flash",
    "deepseek-harness/",
    "kilo/",
    "glm-5.3-flash",
    "mimo-v2.6-flash",
    "big-pickle",
    "nemotron-3-ultra",
    "nemotron-3.5-lightning",
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "claude-3-5-haiku",
    "claude-3-haiku",
    "gemini-1.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
)

_UNSUPPORTED_VARIANT_MODELS: set[str] = set()


def model_supports_variant(model: str) -> bool:
    """Returns True if the model is expected to support reasoning variants/effort."""
    if not model:
        return False
    raw = model.split("#", 1)[0].strip().lower()
    if raw in _UNSUPPORTED_VARIANT_MODELS:
        return False
    bare = raw.split("/", 1)[1] if "/" in raw else raw
    if bare in _UNSUPPORTED_VARIANT_MODELS:
        return False
    for pat in _KNOWN_NON_VARIANT_PATTERNS:
        if pat in raw:
            return False
    return True


def mark_model_variant_unsupported(model: str) -> None:
    """Remember that this model rejected reasoning effort / variant."""
    if not model:
        return
    raw = model.split("#", 1)[0].strip().lower()
    _UNSUPPORTED_VARIANT_MODELS.add(raw)
    if "/" in raw:
        _UNSUPPORTED_VARIANT_MODELS.add(raw.split("/", 1)[1])


_VARIANT_UNAVAILABLE = re.compile(
    r"(?:error:\s*)?variant\s+unavailable(?:\s+for\s+([^:\n\r]+))?",
    re.I,
)


def rejected_variant_model(output: str) -> str:
    """The model name in a 'Variant unavailable for <model>: <variant>' error, or ''."""
    m = _VARIANT_UNAVAILABLE.search(output)
    if m and m.group(1):
        return m.group(1).strip()
    return ""

