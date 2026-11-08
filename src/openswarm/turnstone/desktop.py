"""Turnstone Native Desktop Window (GTK4 / Libadwaita / WebKit6).

Provides native integration for Turnstone with window controls,
keyboard shortcuts, and direct desktop launching.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .installer import APP_ID

logger = logging.getLogger("turnstone.desktop")

try:
    import gi
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit
    HAS_GTK = True
except Exception:
    HAS_GTK = False

try:
    gi.require_version("Adw", "1")
    from gi.repository import Adw
    HAS_ADW = True
except Exception:
    HAS_ADW = False


class TurnstoneWindow:
    """Native Libadwaita / GTK4 Desktop Window for Turnstone."""

    def __init__(self, app: Any, url: str, workdir: Path | None = None) -> None:
        if not HAS_GTK:
            raise RuntimeError("GTK4 and WebKitGTK 6.0 are required for TurnstoneWindow")

        self.app = app
        self.url = url
        self.workdir = (workdir or Path.cwd()).resolve()

        # Disable software rendering glitches on Wayland
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

        if HAS_ADW:
            self.window = Adw.ApplicationWindow(application=app, title="Turnstone — Autonomous Research Teammate")
        else:
            self.window = Gtk.ApplicationWindow(application=app, title="Turnstone — Autonomous Research Teammate")

        self.window.set_default_size(1080, 740)

        try:
            Gtk.Window.set_default_icon_name("turnstone")
        except Exception:
            pass

        if HAS_ADW:
            style_mgr = Adw.StyleManager.get_default()
            style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)

        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_content(self.main_box) if HAS_ADW else self.window.set_child(self.main_box)

        self._build_header()
        self._build_webview()

    def _build_header(self) -> None:
        if HAS_ADW:
            self.header = Adw.HeaderBar()
            self.title_widget = Adw.WindowTitle(
                title="Turnstone",
                subtitle=f"🪨🦅 {self.workdir.name} · Autonomous Research Teammate",
            )
            self.header.set_title_widget(self.title_widget)
        else:
            self.header = Gtk.HeaderBar()
            lbl = Gtk.Label(label=f"Turnstone — {self.workdir.name}")
            self.header.set_title_widget(lbl)

        # Left: Reload button
        btn_reload = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        btn_reload.set_tooltip_text("Обновить (Ctrl+R)")
        btn_reload.connect("clicked", lambda _: self.web_view.reload())
        self.header.pack_start(btn_reload)

        self.main_box.append(self.header)

    def _build_webview(self) -> None:
        settings = WebKit.Settings()
        try:
            settings.set_javascript_can_access_clipboard(True)
            settings.set_enable_smooth_scrolling(True)
            settings.set_enable_developer_extras(True)
        except Exception:
            pass

        self.web_view = WebKit.WebView(settings=settings)
        self.web_view.set_vexpand(True)
        self.web_view.set_hexpand(True)

        try:
            bg = Gdk.RGBA()
            bg.parse("#090b0e")
            self.web_view.set_background_color(bg)
        except Exception:
            pass

        self.web_view.load_uri(self.url)
        self.main_box.append(self.web_view)

    def present(self) -> None:
        self.window.present()


def run_turnstone_gtk(url: str, workdir: Path | None = None) -> None:
    if HAS_ADW:
        app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    else:
        app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(application: Any) -> None:
        win = TurnstoneWindow(app=application, url=url, workdir=workdir)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)
