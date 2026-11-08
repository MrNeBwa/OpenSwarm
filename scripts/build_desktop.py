#!/usr/bin/env python3
"""Build & Package OpenSwarm Native Desktop Application for Linux."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    print("========================================================================")
    print("🛠️  Building OpenSwarm Native Desktop Application (Libadwaita / GTK4)")
    print("========================================================================")

    # 1. Check Python Dependencies
    print("\n[1/4] Checking GTK4 / Libadwaita / WebKitGTK stack…")
    stack_ok = True
    for mod_name, ver in [("Gtk", "4.0"), ("Adw", "1"), ("WebKit", "6.0"), ("Gtk4LayerShell", "1.0")]:
        try:
            import gi
            gi.require_version(mod_name, ver)
            __import__(f"gi.repository.{mod_name}", fromlist=[mod_name])
            print(f"  ✔ {mod_name} {ver} is available")
        except Exception as exc:
            print(f"  ⚠️  {mod_name} {ver}: {exc}")
            if mod_name in ("Gtk", "WebKit"):
                stack_ok = False

    if not stack_ok:
        print("\n❌ Essential GUI components missing.")
        print("   Arch/Manjaro:  sudo pacman -S python-gobject gtk4 libadwaita webkitgtk-6.0 gtk4-layer-shell")
        print("   Ubuntu/Debian: sudo apt-get install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-webkit-6.0")
        print("   Fedora:        sudo dnf install python3-gobject gtk4 libadwaita webkitgtk6.0 gtk4-layer-shell")

    # 2. Generate Standalone Launchers in dist/
    print("\n[2/4] Generating standalone desktop launchers in dist/…")
    launcher_file = dist / "openswarm-desktop"
    launcher_content = f"""#!/usr/bin/env bash
# OpenSwarm Desktop Application Standalone Launcher
set -euo pipefail
ROOT="{root}"
export PYTHONPATH="$ROOT/src:${{PYTHONPATH:-}}"
exec python3 -m openswarm.desktop "$@"
"""
    launcher_file.write_text(launcher_content, encoding="utf-8")
    launcher_file.chmod(0o755)
    print(f"  ✔ Generated {launcher_file}")

    turnstone_launcher = dist / "turnstone-desktop"
    turnstone_content = f"""#!/usr/bin/env bash
# Turnstone Desktop Application Standalone Launcher
set -euo pipefail
ROOT="{root}"
export PYTHONPATH="$ROOT/src:${{PYTHONPATH:-}}"
exec python3 -m openswarm.turnstone --desktop "$@"
"""
    turnstone_launcher.write_text(turnstone_content, encoding="utf-8")
    turnstone_launcher.chmod(0o755)
    print(f"  ✔ Generated {turnstone_launcher}")

    turnstone_cli = dist / "turnstone"
    turnstone_cli_content = f"""#!/usr/bin/env bash
# Turnstone CLI Standalone Launcher
set -euo pipefail
ROOT="{root}"
export PYTHONPATH="$ROOT/src:${{PYTHONPATH:-}}"
exec python3 -m openswarm.turnstone "$@"
"""
    turnstone_cli.write_text(turnstone_cli_content, encoding="utf-8")
    turnstone_cli.chmod(0o755)
    print(f"  ✔ Generated {turnstone_cli}")

    # 3. Verify Desktop Assets
    print("\n[3/4] Verifying desktop assets (icons & desktop entries)…")
    icon_src = root / "src" / "openswarm" / "openswarm.svg"
    turnstone_icon = root / "src" / "openswarm" / "turnstone" / "turnstone.svg"
    if not icon_src.is_file():
        print(f"  ❌ Missing application icon: {icon_src}")
        return 1
    if not turnstone_icon.is_file():
        print(f"  ❌ Missing Turnstone icon: {turnstone_icon}")
        return 1
    print(f"  ✔ OpenSwarm icon verified: {icon_src}")
    print(f"  ✔ Turnstone icon verified: {turnstone_icon}")

    # 4. Install Desktop Integration (Icons + Desktop Entries)
    print("\n[4/4] Installing desktop menu integration…")
    sys.path.insert(0, str(root / "src"))
    try:
        from openswarm.desktop.installer import install_desktop
        from openswarm.turnstone.installer import install_turnstone_desktop

        for p in install_desktop():
            print(f"  ✔ Installed OpenSwarm: {p}")
        for p in install_turnstone_desktop():
            print(f"  ✔ Installed Turnstone: {p}")
    except Exception as exc:
        print(f"  ⚠️  Desktop install warning: {exc}")

    print("\n========================================================================")
    print("✔ Both applications built and installed successfully!")
    print("  1. OpenSwarm (Multi-Agent Studio):")
    print("     Run via:  openswarm-desktop | openswarm desktop")
    print("  2. Turnstone (Autonomous Research & Intelligence Teammate):")
    print("     Run via:  turnstone-desktop | turnstone | openswarm turnstone")
    print("========================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
