"""Persistent memory of your last selections (mode, models per role, skill toggles)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .config import ROLES, SwarmConfig

MODES = ("chat", "auto", "storm", "best", "cowork", "research", "write", "review")


def state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "openswarm" / "state.json"


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # memory is best-effort; never break the app over it


def forget() -> bool:
    try:
        state_path().unlink()
        return True
    except OSError:
        return False


def apply_state(cfg: SwarmConfig, state: dict[str, Any]) -> bool:
    """Overlay remembered selections onto the config defaults. Returns True if anything was restored."""
    restored = False
    roles = state.get("roles")
    if isinstance(roles, dict):
        for role in ROLES:
            val = roles.get(role)
            if isinstance(val, str) and val:
                setattr(cfg.roles, role, val)
                restored = True
    if state.get("mode") in MODES or str(state.get("mode", "")).startswith("wf:"):
        cfg.default_mode = state["mode"]
        restored = True
    skills = state.get("skills")
    if isinstance(skills, list):
        wanted = {s for s in skills if isinstance(s, str)}
        for sk in cfg.skills:
            sk.default = sk.id in wanted
        restored = True
    par = state.get("parallel")
    if isinstance(par, int) and 0 <= par <= 64:
        cfg.max_parallel = par
        restored = True
    return restored


def snapshot(mode: str, planner: str, coder: str, reviewer: str, skills: list[str]) -> dict[str, Any]:
    return {
        "mode": mode,
        "roles": {"planner": planner, "architect": planner, "coder": coder, "reviewer": reviewer},
        "skills": sorted(skills),
    }
