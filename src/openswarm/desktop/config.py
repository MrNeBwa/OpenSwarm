"""Desktop application settings and persistent state."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def desktop_config_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "openswarm" / "desktop.json"


@dataclass
class DesktopConfig:
    color_scheme: str = "default"  # default (follow system) | dark | light
    zoom_level: float = 1.0  # 0.8 to 2.0
    high_contrast: bool = False
    safe_render: bool = True  # software rendering for WebKit (avoids blank grey driver issues)
    pet_enabled: bool = True
    pet_ontop: bool = True
    pet_overlay: bool = True
    window_width: int = 1380
    window_height: int = 890
    window_maximized: bool = False
    confirm_desktop_actions: bool = True

    @classmethod
    def load(cls) -> DesktopConfig:
        p = desktop_config_path()
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                valid_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**valid_fields)
        except Exception:
            pass
        return cls()

    def save(self) -> None:
        p = desktop_config_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except Exception:
            pass
