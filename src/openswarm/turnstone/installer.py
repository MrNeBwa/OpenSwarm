"""Desktop installer and asset registry for Turnstone."""

from __future__ import annotations

import shutil
from pathlib import Path

APP_ID = "dev.openswarm.turnstone"


def install_turnstone_desktop() -> list[Path]:
    """Install Turnstone .desktop entry, icon, and user launcher symlinks."""
    home = Path.home()
    apps_dir = home / ".local" / "share" / "applications"
    icons_dir = home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    bin_dir = home / ".local" / "bin"

    apps_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    installed: list[Path] = []
    root = Path(__file__).resolve().parent

    # 1. Install Icon
    icon_src = root / "turnstone.svg"
    icon_dest = icons_dir / "turnstone.svg"
    if icon_src.exists():
        shutil.copy2(icon_src, icon_dest)
        installed.append(icon_dest)

    # 2. Install Desktop Entry
    desktop_entry = apps_dir / f"{APP_ID}.desktop"
    desktop_content = f"""[Desktop Entry]
Name=Turnstone
GenericName=Autonomous Research Teammate
Comment=Deep web research, tool execution, and persistent brain powered by OpenCode
Exec=openswarm turnstone desktop %u
Icon=turnstone
Terminal=false
Type=Application
Categories=Development;Office;Utility;Network;
StartupWMClass={APP_ID}
Keywords=AI;Research;Grok;Agent;OpenCode;Turnstone;
"""
    desktop_entry.write_text(desktop_content, encoding="utf-8")
    installed.append(desktop_entry)

    return installed
