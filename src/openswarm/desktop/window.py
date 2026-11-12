"""Native Libadwaita / GTK4 Main Window for OpenSwarm.
Provides native HeaderBar, WindowTitle, folder picker, keyboard shortcuts, zoom controls,
accessibility modes, floating pet companion management, and seamless WebKitGTK integration.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .. import __version__
from .bridge import DesktopBridge, HIGH_CONTRAST_CSS
from .config import DesktopConfig
from .dialogs import (
    show_about_dialog,
    show_folder_chooser,
    show_preferences_window,
    show_shortcuts_window,
)
from .pet import OpenSwarmPetWindow

logger = logging.getLogger("openswarm.desktop.window")

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


class OpenSwarmMainWindow:
    """The modern, accessible native desktop window for OpenSwarm."""

    def __init__(
        self,
        app: Any,
        url: str,
        cfg: DesktopConfig,
        workdir: Path | None = None,
        gpu: bool = False,
    ) -> None:
        if not HAS_GTK:
            raise RuntimeError("GTK4 is required for OpenSwarmMainWindow")

        self.app = app
        self.url = url
        self.cfg = cfg
        self.workdir = workdir or Path.cwd()
        self.gpu = gpu
        self.pet_window: OpenSwarmPetWindow | None = None

        if not gpu or cfg.safe_render:
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
            os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

        # Create Window (Libadwaita if available, else GTK4 ApplicationWindow)
        if HAS_ADW:
            self.window = Adw.ApplicationWindow(application=app, title=f"OpenSwarm v{__version__}")
        else:
            self.window = Gtk.ApplicationWindow(application=app, title=f"OpenSwarm v{__version__}")

        self.window.set_default_size(cfg.window_width, cfg.window_height)
        if cfg.window_maximized:
            self.window.maximize()

        try:
            Gtk.Window.set_default_icon_name("openswarm")
        except Exception:
            pass

        # Apply saved color scheme
        if HAS_ADW:
            style_mgr = Adw.StyleManager.get_default()
            if cfg.color_scheme == "dark":
                style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            elif cfg.color_scheme == "light":
                style_mgr.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            else:
                style_mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)

        # Main Vertical Layout Box
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.set_content(self.main_box) if HAS_ADW else self.window.set_child(self.main_box)

        # Build Native HeaderBar
        self._build_header_bar()

        # Build Web View
        self._build_web_view()

        # Build Keyboard Shortcuts Controller
        self._setup_shortcuts()

        # Save window geometry on close
        self.window.connect("close-request", self._on_close_request)

    def _build_header_bar(self) -> None:
        if HAS_ADW:
            self.header = Adw.HeaderBar()
        else:
            self.header = Gtk.HeaderBar()

        # --- Left Controls
        left_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left_box.set_margin_start(4)

        # New Chat Button
        self.btn_new = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self.btn_new.set_tooltip_text("Новый чат (Ctrl+N)")
        self.btn_new.update_property([Gtk.AccessibleProperty.LABEL], ["Создать новый чат"])
        self.btn_new.connect("clicked", lambda _: self.trigger_new_chat())
        left_box.append(self.btn_new)

        # Open Working Directory Button
        self.btn_open = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        self.btn_open.set_tooltip_text("Выбрать рабочую папку проекта (Ctrl+O)")
        self.btn_open.update_property([Gtk.AccessibleProperty.LABEL], ["Выбрать рабочую папку"])
        self.btn_open.connect("clicked", lambda _: self.trigger_open_dir())
        left_box.append(self.btn_open)

        # Mode quick switcher button / popover
        self.btn_mode = Gtk.MenuButton()
        self.btn_mode.set_icon_name("view-grid-symbolic")
        self.btn_mode.set_tooltip_text("Режим работы роя агентов")
        self._build_mode_menu()
        left_box.append(self.btn_mode)

        self.header.pack_start(left_box)

        # --- Center Title & Subtitle
        short_dir = self.workdir.name or str(self.workdir)
        if HAS_ADW:
            self.title_widget = Adw.WindowTitle(
                title="OpenSwarm",
                subtitle=f"📁 {short_dir} · Авто-режим",
            )
            self.header.set_title_widget(self.title_widget)
        else:
            self.title_label = Gtk.Label(label=f"OpenSwarm — 📁 {short_dir}")
            self.header.set_title_widget(self.title_label)

        # --- Right Controls
        right_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right_box.set_margin_end(4)

        # Status spinner & badge
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.spinner = Gtk.Spinner()
        self.status_label = Gtk.Label(label="● Готов")
        self.status_label.add_css_class("caption")
        self.status_box.append(self.spinner)
        self.status_box.append(self.status_label)
        right_box.append(self.status_box)

        # Floating Pet Companion Button
        self.btn_pet = Gtk.Button.new_from_icon_name("emblem-favorite-symbolic")
        self.btn_pet.set_tooltip_text("Вынести плавающего питомца-помощника (Ctrl+Shift+P)")
        self.btn_pet.update_property([Gtk.AccessibleProperty.LABEL], ["Включить или скрыть выносного питомца"])
        self.btn_pet.connect("clicked", lambda _: self.toggle_pet())
        right_box.append(self.btn_pet)

        # Zoom Controls (Accessibility)
        self.btn_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic")
        self.btn_zoom_in.set_tooltip_text("Увеличить масштаб (Ctrl++)")
        self.btn_zoom_in.connect("clicked", lambda _: self.zoom_step(0.1))
        right_box.append(self.btn_zoom_in)

        self.btn_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic")
        self.btn_zoom_out.set_tooltip_text("Уменьшить масштаб (Ctrl+-)")
        self.btn_zoom_out.connect("clicked", lambda _: self.zoom_step(-0.1))
        right_box.append(self.btn_zoom_out)

        # Primary Application Menu
        self.btn_menu = Gtk.MenuButton()
        self.btn_menu.set_icon_name("open-menu-symbolic")
        self.btn_menu.set_tooltip_text("Главное меню")
        self._build_primary_menu()
        right_box.append(self.btn_menu)

        self.header.pack_end(right_box)
        self.main_box.append(self.header)

    def _build_mode_menu(self) -> None:
        menu = Gio.Menu()
        modes = [
            ("✨ Авто-режим (Умный выбор)", "auto"),
            ("💬 Одиночный Чат", "chat"),
            ("🤝 Разработка в Команде (Cowork)", "cowork"),
            ("⚡ Параллельный Рой (Storm)", "storm"),
            ("📐 Архитектор и План", "architect"),
            ("🩺 Диагностика и Починка (Heal)", "heal"),
        ]
        for label, m_id in modes:
            menu.append(label, f"win.mode_{m_id}")

        self.btn_mode.set_menu_model(menu)
        for _, m_id in modes:
            act = Gio.SimpleAction.new(f"mode_{m_id}", None)
            act.connect("activate", lambda _a, _p, mid=m_id: self.set_mode(mid))
            self.window.add_action(act)

    def _build_primary_menu(self) -> None:
        menu = Gio.Menu()

        sec_main = Gio.Menu()
        sec_main.append("Новый чат", "win.new_chat")
        sec_main.append("Открыть рабочую папку…", "win.open_dir")
        sec_main.append("Вынести питомца на рабочий стол", "win.toggle_pet")
        menu.append_section(None, sec_main)

        sec_a11y = Gio.Menu()
        sec_a11y.append("Сбросить масштаб (100%)", "win.zoom_reset")
        sec_a11y.append("Режим высокой контрастности", "win.toggle_contrast")
        menu.append_section("Доступность", sec_a11y)

        sec_settings = Gio.Menu()
        sec_settings.append("Настройки…", "win.preferences")
        sec_settings.append("Горячие клавиши", "win.shortcuts")
        sec_settings.append("Открыть в веб-браузере", "win.open_browser")
        sec_settings.append("Обновить интерфейс", "win.reload")
        sec_settings.append("О программе OpenSwarm", "win.about")
        menu.append_section(None, sec_settings)

        self.btn_menu.set_menu_model(menu)

        # Bind actions
        actions = [
            ("new_chat", lambda *_: self.trigger_new_chat()),
            ("open_dir", lambda *_: self.trigger_open_dir()),
            ("toggle_pet", lambda *_: self.toggle_pet()),
            ("zoom_reset", lambda *_: self.set_zoom_level(1.0)),
            ("toggle_contrast", lambda *_: self.toggle_high_contrast()),
            ("preferences", lambda *_: self.show_preferences()),
            ("shortcuts", lambda *_: self.show_shortcuts()),
            ("open_browser", lambda *_: self.open_in_browser()),
            ("reload", lambda *_: self.web_view.reload()),
            ("about", lambda *_: self.show_about()),
        ]
        for name, callback in actions:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", callback)
            self.window.add_action(act)

    def _build_web_view(self) -> None:
        settings = WebKit.Settings()
        try:
            settings.set_javascript_can_access_clipboard(True)
            settings.set_enable_smooth_scrolling(True)
            settings.set_enable_developer_extras(True)
            settings.set_enable_webaudio(True)
            settings.set_enable_media_stream(True)
            if not self.gpu or self.cfg.safe_render:
                settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.NEVER)
        except Exception:
            pass

        self.user_content = WebKit.UserContentManager()
        self.bridge = DesktopBridge(self)
        self.bridge.register(self.user_content)

        self.web_view = WebKit.WebView(settings=settings, user_content_manager=self.user_content)
        self.web_view.set_vexpand(True)
        self.web_view.set_hexpand(True)

        try:
            bg = Gdk.RGBA()
            bg.parse("#090a0f")
            self.web_view.set_background_color(bg)
        except Exception:
            pass

        # Apply initial zoom level
        if self.cfg.zoom_level != 1.0:
            self.web_view.set_zoom_level(self.cfg.zoom_level)

        # Handle links (external links open in system default browser)
        base = self.url.split("/?")[0]

        def on_policy(_v: Any, decision: Any, dtype: Any) -> bool:
            try:
                if dtype in (WebKit.PolicyDecisionType.NEW_WINDOW_ACTION, WebKit.PolicyDecisionType.NAVIGATION_ACTION):
                    uri = decision.get_navigation_action().get_request().get_uri()
                    if not uri.startswith(base) and not uri.startswith("about:"):
                        Gio.AppInfo.launch_default_for_uri(uri, None)
                        decision.ignore()
                        return True
            except Exception:
                pass
            return False

        def on_permission(_v: Any, req: Any) -> bool:
            try:
                if isinstance(req, (WebKit.NotificationPermissionRequest, WebKit.UserMediaPermissionRequest)):
                    req.allow()
                    return True
                req.deny()
                return True
            except Exception:
                return False

        def on_notify(_v: Any, n: Any) -> bool:
            try:
                self.send_desktop_notification(n.get_title() or "OpenSwarm", n.get_body() or "")
                return True
            except Exception:
                return False

        self.web_view.connect("decide-policy", on_policy)
        self.web_view.connect("permission-request", on_permission)
        self.web_view.connect("show-notification", on_notify)

        self.web_view.load_uri(self.url)
        self.main_box.append(self.web_view)

        # If High Contrast enabled in config, apply it once page loads
        if self.cfg.high_contrast:
            self.web_view.connect("load-changed", lambda _v, ev: self._apply_contrast_if_loaded(ev))

    def _setup_shortcuts(self) -> None:
        key_ctrl = Gtk.EventControllerKey.new()

        def on_key_pressed(_ctrl: Any, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
            is_ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            is_shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            is_alt = bool(state & Gdk.ModifierType.ALT_MASK)

            # Ctrl+N -> New Chat
            if is_ctrl and not is_shift and keyval in (Gdk.KEY_n, Gdk.KEY_N):
                self.trigger_new_chat()
                return True
            # Ctrl+O -> Open Folder
            if is_ctrl and not is_shift and keyval in (Gdk.KEY_o, Gdk.KEY_O):
                self.trigger_open_dir()
                return True
            # Ctrl+Shift+P -> Toggle Pet
            if is_ctrl and is_shift and keyval in (Gdk.KEY_p, Gdk.KEY_P):
                self.toggle_pet()
                return True
            # Ctrl+Plus / Ctrl+= -> Zoom in
            if is_ctrl and keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self.zoom_step(0.1)
                return True
            # Ctrl+Minus -> Zoom out
            if is_ctrl and keyval in (Gdk.KEY_minus, Gdk.KEY_underscore, Gdk.KEY_KP_Subtract):
                self.zoom_step(-0.1)
                return True
            # Ctrl+0 -> Zoom reset
            if is_ctrl and keyval in (Gdk.KEY_0, Gdk.KEY_KP_0):
                self.set_zoom_level(1.0)
                return True
            # Alt+Shift+H -> High Contrast
            if is_alt and is_shift and keyval in (Gdk.KEY_h, Gdk.KEY_H):
                self.toggle_high_contrast()
                return True
            # F1 -> Shortcuts
            if keyval == Gdk.KEY_F1:
                self.show_shortcuts()
                return True
            # Ctrl+Comma -> Settings
            if is_ctrl and keyval in (Gdk.KEY_comma, Gdk.KEY_less):
                self.show_preferences()
                return True
            # F11 -> Toggle Fullscreen
            if keyval == Gdk.KEY_F11:
                if self.window.is_fullscreen():
                    self.window.unfullscreen()
                else:
                    self.window.fullscreen()
                return True
            # Ctrl+R or F5 -> Reload
            if (is_ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R)) or keyval == Gdk.KEY_F5:
                self.web_view.reload()
                return True
            return False

        key_ctrl.connect("key-pressed", on_key_pressed)
        self.window.add_controller(key_ctrl)

    # ---------------------------------------------------------------- Actions
    def trigger_new_chat(self) -> None:
        """Invokes New Chat in the web UI."""
        js = "try { if (window.openNewModal) window.openNewModal(); else if (window.createSession) window.createSession(null, null, 'auto'); } catch(e){}"
        self.web_view.evaluate_javascript(js, -1, None, None, None, None, None)

    def trigger_open_dir(self) -> None:
        """Opens native folder chooser and switches workspace."""
        def on_picked(path_str: str | None) -> None:
            if path_str:
                self.on_directory_picked(path_str)

        show_folder_chooser(self.window, str(self.workdir), on_picked)

    def on_directory_picked(self, path_str: str) -> None:
        self.workdir = Path(path_str)
        short_dir = self.workdir.name or str(self.workdir)
        if HAS_ADW and hasattr(self, "title_widget"):
            self.title_widget.set_subtitle(f"📁 {short_dir}")
        elif hasattr(self, "title_label"):
            self.title_label.set_text(f"OpenSwarm — 📁 {short_dir}")

        import json
        escaped_path = json.dumps(path_str)
        js = f"try {{ if (window.createSession) window.createSession(null, {escaped_path}); else if (window.loadFiles) window.loadFiles(''); }} catch(e){{}}"
        self.web_view.evaluate_javascript(js, -1, None, None, None, None, None)

    def set_mode(self, mode_id: str) -> None:
        import json
        js = f"try {{ if (window.sel) {{ window.sel.mode = {json.dumps(mode_id)}; if (window.renderModes) window.renderModes(); }} }} catch(e){{}}"
        self.web_view.evaluate_javascript(js, -1, None, None, None, None, None)
        mode_names = {"auto": "Авто", "chat": "Чат", "cowork": "Cowork", "storm": "Storm", "architect": "Архитектор", "heal": "Починка"}
        lbl = mode_names.get(mode_id, mode_id)
        short_dir = self.workdir.name or str(self.workdir)
        if HAS_ADW and hasattr(self, "title_widget"):
            self.title_widget.set_subtitle(f"📁 {short_dir} · {lbl}")

    def update_title_and_status(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "").lower()
        title = str(data.get("title") or "")
        workdir = str(data.get("workdir") or "")

        if workdir:
            self.workdir = Path(workdir)

        short_dir = self.workdir.name or str(self.workdir)
        if HAS_ADW and hasattr(self, "title_widget"):
            self.title_widget.set_title(title or "OpenSwarm")
            self.title_widget.set_subtitle(f"📁 {short_dir}")

        if "run" in status or "busy" in status or status == "running":
            self.spinner.start()
            self.status_label.set_text("⚙️ В работе")
        elif "perm" in status or "confirm" in status:
            self.spinner.stop()
            self.status_label.set_text("⚠️ Подтверждение")
        elif "error" in status or "fail" in status:
            self.spinner.stop()
            self.status_label.set_text("✘ Ошибка")
        else:
            self.spinner.stop()
            self.status_label.set_text("● Готов")

    def toggle_pet(self, force_enabled: bool | None = None) -> bool:
        if force_enabled is None:
            should_run = (self.pet_window is None)
        else:
            should_run = bool(force_enabled)

        if should_run:
            if self.pet_window is None:
                pet_url = f"{self.url}&pet=1"
                if self.cfg.pet_overlay:
                    pet_url += "&overlay=1"
                self.pet_window = OpenSwarmPetWindow(
                    pet_url=pet_url,
                    ontop=self.cfg.pet_ontop,
                    overlay=self.cfg.pet_overlay,
                    safe_render=self.cfg.safe_render,
                    on_raise_main=self.present,
                )
                self.pet_window.present()
                self.btn_pet.add_css_class("suggested-action")
                return True
        else:
            if self.pet_window is not None:
                try:
                    self.pet_window.close()
                except Exception:
                    pass
                self.pet_window = None
                self.btn_pet.remove_css_class("suggested-action")
                return False
        return self.pet_window is not None

    def zoom_step(self, delta: float) -> None:
        cur = self.web_view.get_zoom_level()
        new_val = max(0.7, min(2.5, round(cur + delta, 2)))
        self.set_zoom_level(new_val)

    def set_zoom_level(self, level: float) -> None:
        self.web_view.set_zoom_level(level)
        self.cfg.zoom_level = level
        self.cfg.save()

    def toggle_high_contrast(self, force_enabled: bool | None = None) -> bool:
        if force_enabled is None:
            self.cfg.high_contrast = not self.cfg.high_contrast
        else:
            self.cfg.high_contrast = bool(force_enabled)
        self.cfg.save()

        import json
        is_hc = "true" if self.cfg.high_contrast else "false"
        js = (
            f"document.body.setAttribute('data-high-contrast', '{is_hc}'); "
            f"document.documentElement.classList.toggle('high-contrast-mode', {is_hc});"
        )
        self.web_view.evaluate_javascript(js, -1, None, None, None, None, None)
        return self.cfg.high_contrast

    def _apply_contrast_if_loaded(self, ev: Any) -> None:
        if ev == WebKit.LoadEvent.FINISHED and self.cfg.high_contrast:
            self.toggle_high_contrast(True)

    def show_preferences(self) -> None:
        show_preferences_window(self.window, self.cfg, on_change=self._on_pref_changed)

    def _on_pref_changed(self, key: str, value: Any) -> None:
        if key == "zoom_level":
            self.set_zoom_level(float(value))
        elif key == "high_contrast":
            self.toggle_high_contrast(bool(value))
        elif key in ("pet_ontop", "pet_overlay") and self.pet_window:
            self.toggle_pet(False)
            self.toggle_pet(True)

    def show_shortcuts(self) -> None:
        show_shortcuts_window(self.window)

    def show_about(self) -> None:
        show_about_dialog(self.window, __version__)

    def open_in_browser(self) -> None:
        import webbrowser
        webbrowser.open(self.url)

    def send_desktop_notification(self, title: str, body: str) -> None:
        try:
            note = Gio.Notification.new(title)
            note.set_body(body)
            note.set_icon(Gio.ThemedIcon.new("openswarm"))
            if self.app:
                self.app.send_notification(None, note)
                return
        except Exception:
            pass
        if shutil.which("notify-send"):
            try:
                subprocess.run(["notify-send", "-a", "OpenSwarm", "-i", "openswarm", title, body], timeout=1.0)
            except Exception:
                pass

    def present(self) -> None:
        self.window.present()

    def _on_close_request(self, _win: Any) -> bool:
        width, height = self.window.get_default_size()
        self.cfg.window_width = width
        self.cfg.window_height = height
        self.cfg.window_maximized = self.window.is_maximized()
        self.cfg.save()
        if self.pet_window:
            try:
                self.pet_window.close()
            except Exception:
                pass
        return False
