"""Application runner and lifecycle management for OpenSwarm Desktop."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import ConfigError, SwarmConfig, discover_models, find_config_path, load_config, reconcile_models
from ..state import apply_state, load_state
from ..web import WebApp, _open_app_window
from .config import DesktopConfig
from .installer import APP_ID, PET_APP_ID, install_desktop
from .pet import OpenSwarmPetWindow
from .window import OpenSwarmMainWindow

logger = logging.getLogger("openswarm.desktop.app")

try:
    import gi
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    HAS_GTK = True
except Exception:
    HAS_GTK = False

try:
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    HAS_ADW = True
except Exception:
    HAS_ADW = False


class ServerHandle:
    """Manages the background asyncio WebApp server in a worker thread."""

    def __init__(self, app: WebApp, loop: asyncio.AbstractEventLoop, thread: threading.Thread, box: dict[str, Any]) -> None:
        self.app, self.loop, self.thread, self._box = app, loop, thread, box
        self.url: str = box["url"]

    def stop(self, timeout: float = 10) -> None:
        task = self._box.get("task")
        if task is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(task.cancel)
        self.thread.join(timeout)


def start_server(cfg: SwarmConfig, workdir: Path, port: int = 0, token: str | None = None) -> ServerHandle:
    app = WebApp(cfg, workdir, port, token or secrets.token_urlsafe(18))
    box: dict[str, Any] = {}
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    def runner() -> None:
        asyncio.set_event_loop(loop)

        async def main() -> None:
            box["task"] = asyncio.current_task()
            server = await asyncio.start_server(app.handle, "127.0.0.1", port, limit=2**20)
            app.port = server.sockets[0].getsockname()[1]
            box["url"] = f"http://127.0.0.1:{app.port}/?token={app.token}"
            auto = asyncio.create_task(app.automation.run()) if (cfg.schedules or cfg.triggers) else None
            if cfg.opencode.discover_models:
                async def _bg_discover() -> None:
                    try:
                        found = await discover_models(cfg)
                        if found:
                            reconcile_models(cfg, found)
                    except Exception:
                        pass
                asyncio.create_task(_bg_discover())
            ready.set()
            try:
                async with server:
                    await server.serve_forever()
            except asyncio.CancelledError:
                pass
            finally:
                if auto:
                    auto.cancel()
                await app.shutdown()

        try:
            loop.run_until_complete(main())
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
            ready.set()
        finally:
            loop.close()

    thread = threading.Thread(target=runner, name="openswarm-server", daemon=True)
    thread.start()
    if not ready.wait(20) or "url" not in box:
        raise RuntimeError(f"server failed to start: {box.get('error')}")
    return ServerHandle(app, loop, thread, box)


def gtk_available() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("WebKit", "6.0")
        return True
    except (ImportError, ValueError):
        return False


def chromium_available() -> bool:
    return any(shutil.which(x) for x in ("chromium", "google-chrome-stable", "google-chrome", "brave", "chrome", "microsoft-edge-stable"))


def choose_backend(pref: str = "auto") -> str:
    if pref in ("auto", "gtk") and gtk_available():
        return "gtk"
    if pref in ("auto", "gtk", "chromium") and chromium_available():
        return "chromium"
    return "browser"


def run_desktop_gtk(
    url: str,
    desktop_cfg: DesktopConfig,
    workdir: Path,
    gpu: bool = False,
    pet: bool = False,
    new_chat: bool = False,
    settings: bool = False,
) -> None:
    """Runs the modern Libadwaita / GTK4 desktop application."""
    app_id = PET_APP_ID if pet else APP_ID

    if HAS_ADW:
        app = Adw.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
    else:
        app = Gtk.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(application: Any) -> None:
        if pet:
            pet_url = f"{url}&pet=1"
            if desktop_cfg.pet_overlay:
                pet_url += "&overlay=1"
            win = OpenSwarmPetWindow(
                pet_url=pet_url,
                ontop=desktop_cfg.pet_ontop,
                overlay=desktop_cfg.pet_overlay,
                safe_render=desktop_cfg.safe_render,
            )
            win.present()
        else:
            main_win = OpenSwarmMainWindow(
                app=application,
                url=url,
                cfg=desktop_cfg,
                workdir=workdir,
                gpu=gpu,
            )
            main_win.present()
            if new_chat:
                GLib.timeout_add(800, main_win.trigger_new_chat)
            if settings:
                GLib.timeout_add(800, main_win.show_preferences)

    app.connect("activate", on_activate)
    app.run(None)


def desktop_main(argv: list[str]) -> int:
    """CLI Entry point for `openswarm desktop` and `openswarm-desktop`."""
    p = argparse.ArgumentParser(prog="openswarm desktop", description="OpenSwarm as a native Linux desktop application")
    p.add_argument("--version", action="version", version=f"openswarm desktop {__version__}")
    p.add_argument("--dir", "--repo", dest="dir", type=Path, default=Path.cwd(), help="working directory")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--backend", choices=["auto", "gtk", "chromium", "browser"], default="auto")
    p.add_argument("--check", action="store_true", help="show available window backends and exit")
    p.add_argument("--install", action="store_true", help="install desktop entry, icons, and launcher")
    p.add_argument("--gpu", action="store_true", help="WebKit: allow GPU compositing")
    p.add_argument("--pet", action="store_true", help="Launch in compact floating desktop companion pet mode")
    p.add_argument("--pet-url", type=str, default="", help="Attach standalone pet to an existing server URL")
    p.add_argument("--overlay", action="store_true", default=True, help="Run pet in frameless transparent overlay mode")
    p.add_argument("--no-overlay", dest="overlay", action="store_false", help="Run pet in standard window")
    p.add_argument("--ontop", action="store_true", default=True, help="Keep pet pinned above other windows")
    p.add_argument("--no-ontop", dest="ontop", action="store_false", help="Do not keep pet pinned on top")
    p.add_argument("--high-contrast", action="store_true", help="Enable accessible high contrast mode")
    p.add_argument("--new-chat", action="store_true", help="Open directly in new chat dialog")
    p.add_argument("--settings", action="store_true", help="Open directly with preferences dialog")
    p.add_argument("--no-memory", action="store_true")
    a = p.parse_args(argv)

    if a.install:
        installed = install_desktop()
        print("Installed desktop application integration:")
        for path in installed:
            print(f"  ✔ {path}")
        return 0

    if a.check:
        print(f"OpenSwarm {__version__} Desktop Window Backends:")
        print(f"  Libadwaita     : {'available (Adw 1)' if HAS_ADW else 'missing'}")
        print(f"  GTK4 + WebKit6 : {'available' if gtk_available() else 'missing'}")
        print(f"  Chromium App   : {'available' if chromium_available() else 'missing'}")
        print(f"  Browser        : always (webbrowser module)")
        print(f"  -> Recommended backend: {choose_backend(a.backend)}")
        return 0

    desk_cfg = DesktopConfig.load()
    if a.high_contrast:
        desk_cfg.high_contrast = True
    if not a.ontop:
        desk_cfg.pet_ontop = False
    if not a.overlay:
        desk_cfg.pet_overlay = False

    if a.pet_url:
        backend = choose_backend(a.backend)
        if backend == "gtk":
            try:
                run_desktop_gtk(a.pet_url, desk_cfg, Path.cwd(), gpu=a.gpu, pet=True)
                return 0
            except Exception as exc:
                logger.warning("GTK Pet failed: %s; falling back", exc)
                backend = "chromium" if chromium_available() else "browser"
        if backend == "chromium":
            proc = _open_app_window(a.pet_url, size=(340, 420))
            if proc:
                proc.wait()
            return 0
        webbrowser.open(a.pet_url)
        return 0

    workdir = a.dir.resolve()
    try:
        cfg = load_config((a.config or find_config_path(workdir)).resolve())
    except ConfigError as exc:
        print(exc)
        return 2

    if not a.no_memory:
        apply_state(cfg, load_state())

    handle = start_server(cfg, workdir, a.port)
    backend = choose_backend(a.backend)
    target_url = handle.url
    mode_str = " (Pet Overlay)" if (a.pet and desk_cfg.pet_overlay) else (" (Pet Mode)" if a.pet else "")
    print(f"OpenSwarm {__version__}{mode_str} — native desktop [{backend}]   ({handle.url.split('?')[0]})", flush=True)

    try:
        if backend == "gtk":
            try:
                run_desktop_gtk(
                    target_url,
                    desk_cfg,
                    workdir,
                    gpu=a.gpu,
                    pet=a.pet,
                    new_chat=a.new_chat,
                    settings=a.settings,
                )
                return 0
            except Exception as exc:
                print(f"Native GTK desktop window failed ({exc!r}); falling back", flush=True)
                backend = "chromium" if chromium_available() else "browser"

        if backend == "chromium":
            size = (340, 420) if a.pet else None
            proc = _open_app_window(target_url, size=size)
            if proc:
                try:
                    proc.wait()
                except KeyboardInterrupt:
                    proc.terminate()
            return 0
        elif backend == "browser":
            webbrowser.open(target_url)
            print("Opened in your browser. Press Ctrl+C to stop.", flush=True)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        handle.stop()
    return 0
