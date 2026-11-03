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
PET_APP_ID = "dev.openswarm.pet"
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


def run_gtk(url: str, gpu: bool = False, pet: bool = False, ontop: bool = True, overlay: bool = True) -> None:
    """Blocks until the window is closed. Optional niceties (external links, notifications) never block the window."""
    if not gpu:  # WebKitGTK's GPU compositing paints nothing (plain grey window) on many drivers: render in software
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit

    base = url.split("/?")[0]
    app_id = PET_APP_ID if pet else APP_ID
    app = Gtk.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(application: "Gtk.Application") -> None:
        title = f"OpenSwarm Pet" if pet else f"OpenSwarm v{__version__}"
        win = Gtk.ApplicationWindow(application=application, title=title)
        if pet:
            win.set_default_size(330, 410)
            if overlay:
                win.set_decorated(False)
                try:
                    win.add_css_class("pet-overlay")
                    css = "window.pet-overlay { background: transparent; background-color: transparent; border: none; box-shadow: none; }"
                    provider = Gtk.CssProvider()
                    provider.load_from_data(css.encode("utf-8"))
                    Gtk.StyleContext.add_provider_for_display(
                        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            win.set_default_size(1360, 880)
        try:
            Gtk.Window.set_default_icon_name("openswarm")
        except Exception:  # noqa: BLE001
            pass
        try:
            gtk_settings = Gtk.Settings.get_default()
            if gtk_settings:
                gtk_settings.set_property("gtk-application-prefer-dark-theme", True)
        except Exception:  # noqa: BLE001
            pass
        if pet and ontop:
            try:
                gi.require_version("Gtk4LayerShell", "1.0")
                from gi.repository import Gtk4LayerShell

                if Gtk4LayerShell.is_supported():
                    Gtk4LayerShell.init_for_window(win)
                    Gtk4LayerShell.set_layer(win, Gtk4LayerShell.Layer.OVERLAY)
                    Gtk4LayerShell.set_anchor(win, Gtk4LayerShell.Edge.BOTTOM, True)
                    Gtk4LayerShell.set_anchor(win, Gtk4LayerShell.Edge.RIGHT, True)
                    Gtk4LayerShell.set_margin(win, Gtk4LayerShell.Edge.BOTTOM, 24)
                    Gtk4LayerShell.set_margin(win, Gtk4LayerShell.Edge.RIGHT, 24)
                    Gtk4LayerShell.set_keyboard_mode(win, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
                    Gtk4LayerShell.set_namespace(win, "openswarm-pet")
            except Exception:  # noqa: BLE001
                pass
        settings = WebKit.Settings()
        if not gpu:
            try:
                settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.NEVER)
            except Exception:  # noqa: BLE001 - older WebKitGTK
                pass
        try:
            settings.set_javascript_can_access_clipboard(True)
            settings.set_enable_smooth_scrolling(True)
            settings.set_enable_developer_extras(True)
            settings.set_enable_webaudio(True)
            settings.set_enable_media_stream(True)
        except Exception:  # noqa: BLE001
            pass
        view = WebKit.WebView(settings=settings)
        try:  # transparent for pet overlay, dark for full workspace
            bg = Gdk.RGBA()
            if pet and overlay:
                bg.parse("rgba(0,0,0,0)")
            else:
                bg.parse("#191815")
            view.set_background_color(bg)
        except Exception:  # noqa: BLE001
            pass
        crashed = {"n": 0}

        def on_crash(_v, *_a) -> None:  # the web process died (often a GPU problem): reload once
            crashed["n"] += 1
            print(f"WebKit web process terminated ({crashed['n']})" + ("; reloading" if crashed["n"] < 3 else ""), file=sys.stderr, flush=True)
            if crashed["n"] < 3:
                view.reload()

        def on_failed(_v, _ev, uri, err) -> bool:
            print(f"page load failed: {uri}: {getattr(err, 'message', err)}", file=sys.stderr, flush=True)
            return False

        for sig, cb in (("web-process-terminated", on_crash), ("load-failed", on_failed)):
            try:
                view.connect(sig, cb)
            except TypeError:
                pass
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

        def permission(_v, request) -> bool:  # desktop notifications and microphone for real-time voice
            try:
                if isinstance(request, (WebKit.NotificationPermissionRequest, WebKit.UserMediaPermissionRequest)):
                    request.allow()
                    return True
                request.deny()
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
        if pet:
            handle = Gtk.WindowHandle()
            handle.set_child(view)
            win.set_child(handle)
        else:
            win.set_child(view)
        win.present()

        def pin_pet():
            if not ontop:
                return
            if shutil.which("hyprctl"):
                try:
                    for sel in (f"class:{PET_APP_ID}", "title:OpenSwarm Pet"):
                        subprocess.run(["hyprctl", "dispatch", "setfloating", sel], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["hyprctl", "dispatch", "pin", sel], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["hyprctl", "setprop", sel, "pinned", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["hyprctl", "setprop", sel, "noborder", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["hyprctl", "setprop", sel, "noshadow", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            if shutil.which("swaymsg"):
                try:
                    subprocess.run(["swaymsg", f'[app_id="{PET_APP_ID}"] floating enable, sticky enable, border none'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            if shutil.which("wmctrl"):
                try:
                    subprocess.run(["wmctrl", "-r", "OpenSwarm Pet", "-b", "add,above,sticky"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

        if pet and ontop:
            try:
                GLib.timeout_add(150, lambda: (pin_pet(), False)[1])
                GLib.timeout_add(600, lambda: (pin_pet(), False)[1])
            except Exception:
                pass

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
    p.add_argument("--version", action="version", version=f"openswarm gui {__version__}")
    p.add_argument("--dir", "--repo", dest="dir", type=Path, default=Path.cwd(), help="default working directory")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--backend", choices=["auto", "gtk", "chromium", "browser"], default="auto")
    p.add_argument("--check", action="store_true", help="show which window backends are available and exit")
    p.add_argument("--safe-render", action="store_true", help="(default now) WebKit: software rendering, fixes blank/grey windows")
    p.add_argument("--gpu", action="store_true", help="WebKit: allow GPU compositing (faster, but a grey window on some drivers)")
    p.add_argument("--pet", action="store_true", help="Launch compact floating desktop companion / pet mode")
    p.add_argument("--pet-url", type=str, default="", help="Attach standalone pet window to an already-running OpenSwarm server URL")
    p.add_argument("--overlay", action="store_true", default=True, help="Run pet in frameless transparent overlay mode floating over other apps")
    p.add_argument("--no-overlay", dest="overlay", action="store_false", help="Run pet in standard window")
    p.add_argument("--ontop", action="store_true", default=True, help="Keep pet pinned always above other desktop applications")
    p.add_argument("--no-ontop", dest="ontop", action="store_false", help="Do not keep window pinned on top")
    p.add_argument("--no-memory", action="store_true")
    a = p.parse_args(argv)
    if a.check:
        print(f"OpenSwarm {__version__} window backends:")
        print(f"  gtk4+webkitgtk : {'available' if gtk_available() else 'missing  (sudo pacman -S python-gobject gtk4 webkitgtk-6.0; venv needs --system-site-packages)'}")
        print(f"  chromium app   : {'available' if chromium_available() else 'missing  (sudo pacman -S chromium)'}")
        print(f"  browser        : always (webbrowser module)\n  -> would use: {choose_backend(a.backend)}")
        return 0
    if a.pet_url:
        backend = choose_backend(a.backend)
        target_url = a.pet_url
        if backend == "gtk":
            try:
                run_gtk(target_url, gpu=a.gpu, pet=True, ontop=getattr(a, "ontop", True), overlay=getattr(a, "overlay", True))
                return 0
            except Exception as exc:  # noqa: BLE001
                print(f"GTK pet window failed ({exc!r}); falling back", flush=True)
                backend = "chromium" if chromium_available() else "browser"
        if backend == "chromium":
            proc = _open_app_window(target_url, size=(330, 410))
            wait_for_process_or_ctrl_c(proc)
            return 0
        webbrowser.open(target_url)
        return 0
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
    target_url = f"{handle.url}&pet=1" if a.pet else handle.url
    if a.pet and getattr(a, "overlay", True):
        target_url += "&overlay=1"
    mode_str = " (Pet Overlay Mode)" if (a.pet and getattr(a, "overlay", True)) else (" (Pet Mode)" if a.pet else "")
    print(f"OpenSwarm {__version__}{mode_str} - window backend: {backend}   ({handle.url.split('?')[0]})", flush=True)
    try:
        if backend == "gtk":
            try:
                run_gtk(target_url, gpu=a.gpu, pet=a.pet, ontop=getattr(a, "ontop", True), overlay=getattr(a, "overlay", True))
            except Exception as exc:  # noqa: BLE001 - e.g. no display: degrade instead of dying
                print(f"GTK window failed ({exc!r}); falling back", flush=True)
                backend = "chromium" if chromium_available() else "browser"
        if backend == "chromium":
            size = (340, 420) if a.pet else None
            proc = _open_app_window(target_url, size=size)
            if a.pet and getattr(a, "ontop", True):
                def pin_chromium():
                    time.sleep(0.6)
                    if shutil.which("hyprctl"):
                        try:
                            subprocess.run(["hyprctl", "dispatch", "setfloating", "class:google-chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.run(["hyprctl", "dispatch", "pin", "class:google-chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                    if shutil.which("wmctrl"):
                        try:
                            subprocess.run(["wmctrl", "-r", "OpenSwarm", "-b", "add,above,sticky"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                threading.Thread(target=pin_chromium, daemon=True).start()
            wait_for_process_or_ctrl_c(proc)
        elif backend == "browser":
            webbrowser.open(target_url)
            print("Opened in your browser. Press Ctrl+C to stop.", flush=True)
            wait_for_process_or_ctrl_c(None)
    finally:
        handle.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(gui_main(sys.argv[1:]))
