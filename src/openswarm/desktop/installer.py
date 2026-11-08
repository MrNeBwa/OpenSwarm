"""Installs desktop menu entry, application icon, and standalone desktop launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "dev.openswarm.app"
PET_APP_ID = "dev.openswarm.pet"
ICON_SOURCE = Path(__file__).resolve().parent.parent / "openswarm.svg"


def install_desktop() -> list[Path]:
    """Installs icon, desktop entry, and standalone `openswarm-desktop` launcher."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    bin_dir = Path.home() / ".local" / "bin"
    icon_dir = data / "icons" / "hicolor" / "scalable" / "apps"
    app_dir = data / "applications"

    icon_dir.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    # 1. Install Icon
    target_icon = icon_dir / "openswarm.svg"
    if ICON_SOURCE.is_file():
        shutil.copyfile(ICON_SOURCE, target_icon)

    # 2. Standalone launcher script: `openswarm-desktop`
    launcher = bin_dir / "openswarm-desktop"
    exe_python = sys.executable
    root_src = Path(__file__).resolve().parent.parent.parent
    launcher.write_text(
        f"#!/usr/bin/env bash\n"
        f'export PYTHONPATH="{root_src}:${{PYTHONPATH:-}}"\n'
        f'exec "{exe_python}" -m openswarm.desktop "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    # 3. Desktop Application Entry
    exe = shutil.which("openswarm") or str(launcher)
    entry = app_dir / f"{APP_ID}.desktop"
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=OpenSwarm\n"
        "GenericName=AI Multi-Agent Workspace\n"
        "Comment=Multi-agent AI workspace and orchestrator for autonomous coding\n"
        f"Exec={exe} gui\n"
        "Icon=openswarm\n"
        "Terminal=false\n"
        "Categories=Development;Utility;IDE;\n"
        f"StartupWMClass={APP_ID}\n"
        "StartupNotify=true\n"
        "Keywords=ai;agent;opencode;chat;code;assistant;orchestrator;\n"
        "MimeType=x-scheme-handler/openswarm;\n"
        "Actions=NewChat;Pet;Settings;\n\n"
        "[Desktop Action NewChat]\n"
        "Name=Новый диалог (New Chat)\n"
        f"Exec={exe} gui --new-chat\n\n"
        "[Desktop Action Pet]\n"
        "Name=Выносной ИИ-питомец (Floating Pet)\n"
        f"Exec={exe} gui --pet\n\n"
        "[Desktop Action Settings]\n"
        "Name=Настройки OpenSwarm\n"
        f"Exec={exe} gui --settings\n",
        encoding="utf-8",
    )

    # Clean up legacy .desktop entries if any
    (app_dir / "openswarm.desktop").unlink(missing_ok=True)

    # Update system caches
    for tool in (("update-desktop-database", str(app_dir)), ("gtk-update-icon-cache", "-f", "-t", str(data / "icons" / "hicolor"))):
        if shutil.which(tool[0]):
            try:
                subprocess.run(list(tool), capture_output=True, timeout=2.0)
            except Exception:
                pass

    return [target_icon, entry]
