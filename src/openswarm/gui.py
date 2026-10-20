"""Native desktop window for Linux.

`openswarm gui` runs the OpenSwarm server in a background thread and shows it in a real application window:
GTK4 + WebKitGTK when available (sudo pacman -S python-gobject gtk4 webkitgtk-6.0), otherwise a chromeless
Chromium/Chrome/Brave app window, otherwise your browser. `openswarm desktop-install` adds the app-menu entry + icon.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__
from .config import ConfigError, SwarmConfig, discover_models, find_config_path, load_config, reconcile_models
from .state import apply_state, load_state
from .web import WebApp, _open_app_window

APP_ID = "dev.openswarm.app"
ICON = Path(__file__).with_name("openswarm.svg")


# ------------------------------------------------------------------ server in a thread
class ServerHandle:
    def __init__(self, app: WebApp, loop: asyncio.AbstractEventLoop, thread: threading.Thread, box: dict) -> None:
        self.app, self.loop, self.thread, self._box = app, loop, thread, box
        self.url: str = box["url"]

    def stop(self, timeout: float = 10) -> None:
        task = self._box.get("task")
        if task is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(task.cancel)
        self.thread.join(timeout)


def start_server(cfg: SwarmConfig, workdir: Path, port: int = 0, token: str | None = None) -> ServerHandle:
    app = WebApp(cfg, workdir, port, token or secrets.token_urlsafe(18))
    box: dict = {}
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


# --------------------------------------------------------------------------- backends
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


def run_gtk(url: str) -> None:
    """Blocks until the window is closed. Optional niceties (external links, notifications) never block the window."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gio, Gtk, WebKit

    base = url.split("/?")[0]
    app = Gtk.Application(application_id=APP_ID)

    def activate(application: "Gtk.Application") -> None:
        win = Gtk.ApplicationWindow(application=application, title="OpenSwarm")
        win.set_default_size(1360, 880)
        try:
            Gtk.Window.set_default_icon_name("openswarm")
        except Exception:  # noqa: BLE001
            pass
        view = WebKit.WebView()
        view.load_uri(url)

        def policy(_v, decision, dtype) -> bool:  # external links open in the default browser
            try:
                if dtype in (WebKit.PolicyDecisionType.NEW_WINDOW_ACTION, WebKit.PolicyDecisionType.NAVIGATION_ACTION):
                    uri = decision.get_navigation_action().get_request().get_uri()
                    if not uri.startswith(base):
                        Gio.AppInfo.launch_default_for_uri(uri, None)
                        decision.ignore()
                        return True
            except Exception:  # noqa: BLE001
                pass
            return False

        def permission(_v, request) -> bool:  # only desktop notifications are allowed
            try:
                request.allow() if isinstance(request, WebKit.NotificationPermissionRequest) else request.deny()
                return True
            except Exception:  # noqa: BLE001
                return False

        def notify(_v, n) -> bool:
            try:
                note = Gio.Notification.new(n.get_title() or "OpenSwarm")
                note.set_body(n.get_body() or "")
                application.send_notification(None, note)
                return True
            except Exception:  # noqa: BLE001
                return False

        for sig, cb in (("decide-policy", policy), ("permission-request", permission), ("show-notification", notify)):
            try:
                view.connect(sig, cb)
            except TypeError:
                pass
        win.set_child(view)
        win.present()

    app.connect("activate", activate)
    app.run(None)


def wait_for_process_or_ctrl_c(proc: subprocess.Popen | None) -> None:
    started = time.time()
    try:
        while True:
            time.sleep(1)
            if proc is not None and proc.poll() is not None and time.time() - started > 3:
                return  # window closed (a launcher that exits at once just keeps the server running)
    except KeyboardInterrupt:
        return


# ------------------------------------------------------------------------ desktop entry
def install_desktop() -> list[Path]:
    """Icon + application-menu entry (no root needed)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    icon_dir = data / "icons" / "hicolor" / "scalable" / "apps"
    app_dir = data / "applications"
    icon_dir.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)
    icon = icon_dir / "openswarm.svg"
    shutil.copyfile(ICON, icon)
    exe = shutil.which("openswarm") or str(Path(sys.argv[0]).resolve())
    entry = app_dir / f"{APP_ID}.desktop"
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=OpenSwarm\nGenericName=AI workspace\n"
        "Comment=Multi-agent AI workspace on top of opencode\n"
        f"Exec={exe} gui\nIcon=openswarm\nTerminal=false\nCategories=Development;Utility;\n"
        f"StartupWMClass={APP_ID}\nStartupNotify=true\nKeywords=ai;agent;opencode;chat;code;\n",
        encoding="utf-8",
    )
    (app_dir / "openswarm.desktop").unlink(missing_ok=True)  # entry written by older installers
    for tool in (("update-desktop-database", str(app_dir)), ("gtk-update-icon-cache", "-f", "-t", str(data / "icons" / "hicolor"))):
        if shutil.which(tool[0]):
            subprocess.run(list(tool), capture_output=True)
    return [icon, entry]


# --------------------------------------------------------------------------------- CLI
def gui_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="openswarm gui", description="OpenSwarm as a native desktop window")
    p.add_argument("--dir", "--repo", dest="dir", type=Path, default=Path.cwd(), help="default working directory")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--backend", choices=["auto", "gtk", "chromium", "browser"], default="auto")
    p.add_argument("--check", action="store_true", help="show which window backends are available and exit")
    p.add_argument("--safe-render", action="store_true", help="WebKit: disable GPU compositing (fixes blank windows on some drivers)")
    p.add_argument("--no-memory", action="store_true")
    a = p.parse_args(argv)
    if a.check:
        print(f"OpenSwarm {__version__} window backends:")
        print(f"  gtk4+webkitgtk : {'available' if gtk_available() else 'missing  (sudo pacman -S python-gobject gtk4 webkitgtk-6.0; venv needs --system-site-packages)'}")
        print(f"  chromium app   : {'available' if chromium_available() else 'missing  (sudo pacman -S chromium)'}")
        print(f"  browser        : always (webbrowser module)\n  -> would use: {choose_backend(a.backend)}")
        return 0
    if a.safe_render:
        os.environ["WEBKIT_DISABLE_DMABUF_RENDERER"] = "1"
        os.environ["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
    workdir = a.dir.resolve()
    try:
        cfg = load_config((a.config or find_config_path(workdir)).resolve())
    except ConfigError as exc:
        print(exc)
        return 2
    if not a.no_memory:
        apply_state(cfg, load_state())
    if cfg.opencode.discover_models:
        reconcile_models(cfg, asyncio.run(discover_models(cfg)))
    handle = start_server(cfg, workdir, a.port)
    backend = choose_backend(a.backend)
    print(f"OpenSwarm {__version__} - window backend: {backend}   ({handle.url.split('?')[0]})", flush=True)
    try:
        if backend == "gtk":
            try:
                run_gtk(handle.url)
            except Exception as exc:  # noqa: BLE001 - e.g. no display: degrade instead of dying
                print(f"GTK window failed ({exc!r}); falling back", flush=True)
                backend = "chromium" if chromium_available() else "browser"
        if backend == "chromium":
            wait_for_process_or_ctrl_c(_open_app_window(handle.url))
        elif backend == "browser":
            webbrowser.open(handle.url)
            print("Opened in your browser. Press Ctrl+C to stop.", flush=True)
            wait_for_process_or_ctrl_c(None)
    finally:
        handle.stop()
    return 0
