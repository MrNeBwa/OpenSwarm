"""Application launcher & CLI runner for Turnstone."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from .cli import interactive_chat, manage_brain, run_single_inquiry
from .installer import install_turnstone_desktop
from .server import TurnstoneServer


def start_turnstone_server(workdir: Path, port: int = 0, model: str = "opencode/deepseek-v4.1-flash-free") -> tuple[TurnstoneServer, str]:
    server = TurnstoneServer(workdir=workdir, model=model, port=port)
    ready = threading.Event()
    box: dict[str, Any] = {}
    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)

        async def main() -> None:
            srv = await asyncio.start_server(server.handle_client, "127.0.0.1", port)
            server.port = srv.sockets[0].getsockname()[1]
            box["url"] = f"http://127.0.0.1:{server.port}"
            ready.set()
            async with srv:
                await srv.serve_forever()

        try:
            loop.run_until_complete(main())
        except Exception as exc:
            box["error"] = exc
            ready.set()

    t = threading.Thread(target=runner, name="turnstone-server", daemon=True)
    t.start()
    ready.wait(10)
    if "url" not in box:
        raise RuntimeError(f"Turnstone server failed: {box.get('error')}")
    return server, box["url"]


def turnstone_main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv

    p = argparse.ArgumentParser(
        prog="turnstone",
        description="Turnstone — Autonomous Research Teammate & Tool-Using Intelligence Bot (Powered by OpenCode)",
    )
    p.add_argument("query", nargs="*", help="Query to investigate or subcommand (brain, desktop, research)")
    p.add_argument("--model", type=str, default="opencode/deepseek-v4.1-flash-free", help="OpenCode model to use")
    p.add_argument("--deep", action="store_true", help="Run in autonomous Deep Research mode")
    p.add_argument("--web", action="store_true", help="Launch Turnstone in web browser")
    p.add_argument("--desktop", action="store_true", help="Launch Turnstone in native desktop window")
    p.add_argument("--port", type=int, default=0, help="Web server port (0 = free port)")
    p.add_argument("--install", action="store_true", help="Install desktop menu entry & application icon")

    a = p.parse_args(args_list)

    if a.install:
        installed = install_turnstone_desktop()
        print("Installed Turnstone desktop integration:")
        for path in installed:
            print(f"  ✔ {path}")
        return 0

    sub = a.query[0].lower() if a.query else ""

    if sub == "install":
        installed = install_turnstone_desktop()
        print("Installed Turnstone desktop integration:")
        for path in installed:
            print(f"  ✔ {path}")
        return 0

    if sub == "brain":
        return manage_brain(a.query[1:])

    if sub == "desktop" or a.desktop:
        return turnstone_desktop_main(a.query[1:] if sub == "desktop" else a.query)

    if sub == "research" and len(a.query) > 1:
        topic = " ".join(a.query[1:])
        asyncio.run(run_single_inquiry(topic, deep=True, model=a.model))
        return 0

    if a.web:
        _, url = start_turnstone_server(Path.cwd(), port=a.port, model=a.model)
        print(f"🪨🦅 Turnstone Web UI running at: {url}")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nTurnstone stopped.")
        return 0

    if a.query:
        query_text = " ".join(a.query)
        asyncio.run(run_single_inquiry(query_text, deep=a.deep, model=a.model))
        return 0

    # Default to interactive chat CLI
    try:
        asyncio.run(interactive_chat(model=a.model))
    except KeyboardInterrupt:
        pass
    return 0


def turnstone_desktop_main(argv: list[str] | None = None) -> int:
    """Launch native desktop GTK window for Turnstone."""
    model = "opencode/deepseek-v4.1-flash-free"
    _, url = start_turnstone_server(Path.cwd(), port=0, model=model)
    print(f"🪨🦅 Launching Turnstone Desktop ({url})...")

    # Check GTK4 availability
    try:
        from .desktop import HAS_GTK, run_turnstone_gtk
        if HAS_GTK:
            run_turnstone_gtk(url, Path.cwd())
            return 0
    except Exception as exc:
        print(f"GTK desktop launch note: {exc}; falling back to browser app")

    # Chromium App window fallback
    for b in ("chromium", "google-chrome-stable", "google-chrome", "brave"):
        cmd = shutil.which(b)
        if cmd:
            import subprocess
            subprocess.Popen([cmd, f"--app={url}", "--window-size=1080,740"])
            return 0

    webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(turnstone_main())
