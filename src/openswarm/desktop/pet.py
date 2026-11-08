"""Native Floating Desktop Companion / Pet Window.
Supports Wayland LayerShell (Gtk4LayerShell) for rock-solid floating above tiling WMs (Hyprland, Sway),
plus standard X11 floating fallback with draggable handle.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger("openswarm.desktop.pet")

PET_APP_ID = "dev.openswarm.pet"

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
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell
    HAS_LAYER_SHELL = True
except Exception:
    HAS_LAYER_SHELL = False


def ensure_hyprland_pet_rules(ontop: bool = True, overlay: bool = True) -> None:
    """Configures Hyprland floating and pinning rules so pet never gets tiled."""
    if not shutil.which("hyprctl"):
        return
    pin_lua = "true" if ontop else "false"
    lua_rule = (
        "if _G._openswarm_pet_rule then pcall(function() _G._openswarm_pet_rule:set_enabled(false) end) end; "
        f"_G._openswarm_pet_rule = hl.window_rule({{ "
        f"name = 'openswarm-pet-float', "
        f"match = {{ class = '^{PET_APP_ID}$' }}, "
        f"float = true, pin = {pin_lua}, "
        f"size = '340 420', move = 'monitor_w-364 monitor_h-444', "
        f"border_size = {0 if overlay else 1}, no_shadow = {'true' if overlay else 'false'}, no_blur = {'true' if overlay else 'false'} "
        f"}})"
    )
    try:
        r = subprocess.run(["hyprctl", "eval", lua_rule], capture_output=True, text=True, timeout=1.2)
        if r.returncode == 0 and "error" not in (r.stdout or "").lower():
            return
    except Exception:
        pass

    # Legacy Hyprland fallback (<0.57)
    try:
        rules = [
            f"float,class:^({PET_APP_ID})$",
            f"size 340 420,class:^({PET_APP_ID})$",
            f"move 100%-364 100%-444,class:^({PET_APP_ID})$",
        ]
        if ontop:
            rules.append(f"pin,class:^({PET_APP_ID})$")
        if overlay:
            rules.append(f"noborder,class:^({PET_APP_ID})$")
            rules.append(f"noshadow,class:^({PET_APP_ID})$")
        for rl in rules:
            subprocess.run(["hyprctl", "keyword", "windowrulev2", rl], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.8)
    except Exception:
        pass


def apply_hyprland_pet_state(ontop: bool = True, _overlay: bool = True) -> bool:
    """Enforces floating & pinning on an already-open window."""
    if not shutil.which("hyprctl"):
        return False
    pin_lua = "true" if ontop else "false"
    lua_code = (
        f"local ontop = {pin_lua}; "
        f"for _, sel in ipairs({{'class:{PET_APP_ID}', 'title:OpenSwarm Pet'}}) do "
        "  local w = hl.get_window(sel); "
        "  if w then "
        "    if not w.floating then hl.dispatch(hl.dsp.window.float({ action = 'set', window = sel })) end; "
        "    if ontop and not w.pinned then hl.dispatch(hl.dsp.window.pin({ window = sel })) "
        "    elseif (not ontop) and w.pinned then hl.dispatch(hl.dsp.window.pin({ window = sel })) end; "
        "    hl.dispatch(hl.dsp.window.resize({ x = 340, y = 420, relative = false, window = sel })); "
        "    hl.dispatch(hl.dsp.window.bring_to_top()); "
        "    return 'ok' "
        "  end "
        "end"
    )
    try:
        r = subprocess.run(["hyprctl", "eval", lua_code], capture_output=True, text=True, timeout=1.2)
        if r.returncode == 0 and "error" not in (r.stdout or "").lower():
            return True
    except Exception:
        pass
    return False


class OpenSwarmPetWindow:
    """Standalone native desktop companion widget."""

    def __init__(
        self,
        pet_url: str,
        ontop: bool = True,
        overlay: bool = True,
        safe_render: bool = True,
        on_raise_main: Any = None,
    ) -> None:
        if not HAS_GTK:
            raise RuntimeError("GTK4 and WebKitGTK are required for native PetWindow")

        self.pet_url = pet_url
        self.ontop = ontop
        self.overlay = overlay
        self.on_raise_main = on_raise_main

        if safe_render:
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
            os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")

        ensure_hyprland_pet_rules(ontop=ontop, overlay=overlay)

        self.window = Gtk.Window(title="OpenSwarm Pet")
        self.window.set_default_size(340, 420)
        try:
            Gtk.Window.set_default_icon_name("openswarm")
        except Exception:
            pass

        self._is_layer_shell = False
        if HAS_LAYER_SHELL and Gtk4LayerShell.is_supported():
            try:
                Gtk4LayerShell.init_for_window(self.window)
                layer = Gtk4LayerShell.Layer.OVERLAY if ontop else Gtk4LayerShell.Layer.TOP
                Gtk4LayerShell.set_layer(self.window, layer)
                Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.BOTTOM, True)
                Gtk4LayerShell.set_anchor(self.window, Gtk4LayerShell.Edge.RIGHT, True)
                Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.BOTTOM, 24)
                Gtk4LayerShell.set_margin(self.window, Gtk4LayerShell.Edge.RIGHT, 24)
                Gtk4LayerShell.set_keyboard_mode(self.window, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
                Gtk4LayerShell.set_namespace(self.window, "openswarm-pet")
                self._is_layer_shell = True
            except Exception as exc:
                logger.warning("Gtk4LayerShell init failed: %s", exc)

        if overlay:
            self.window.set_decorated(False)
            try:
                self.window.add_css_class("pet-overlay")
                css = (
                    "window.pet-overlay, window.pet-overlay > * { "
                    "background-color: rgba(15, 17, 26, 0.01); border: none; box-shadow: none; outline: none; }"
                )
                provider = Gtk.CssProvider()
                provider.load_from_data(css.encode("utf-8"))
                Gtk.StyleContext.add_provider_for_display(
                    Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            except Exception:
                pass

        settings = WebKit.Settings()
        try:
            settings.set_javascript_can_access_clipboard(True)
            settings.set_enable_smooth_scrolling(True)
            settings.set_enable_developer_extras(False)
            settings.set_enable_webaudio(True)
            settings.set_enable_media_stream(True)
            if safe_render:
                settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.NEVER)
        except Exception:
            pass

        self.web_view = WebKit.WebView(settings=settings)
        try:
            bg = Gdk.RGBA()
            bg.parse("rgba(15,17,26,0.01)" if overlay else "#14151b")
            self.web_view.set_background_color(bg)
        except Exception:
            pass

        self.web_view.load_uri(pet_url)

        # Allow dragging the pet window around
        handle = Gtk.WindowHandle()
        handle.set_child(self.web_view)
        self.window.set_child(handle)

        if not self._is_layer_shell:
            self._start_hyprland_pin_thread()

    def _start_hyprland_pin_thread(self) -> None:
        def _pin() -> None:
            for delay in (0.3, 0.9, 1.8):
                time.sleep(delay)
                try:
                    apply_hyprland_pet_state(ontop=self.ontop, _overlay=self.overlay)
                except Exception:
                    pass
                if shutil.which("swaymsg"):
                    try:
                        subprocess.run(
                            ["swaymsg", f'[app_id="{PET_APP_ID}"] floating enable, sticky enable, border none'],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=0.8,
                        )
                    except Exception:
                        pass
        threading.Thread(target=_pin, daemon=True).start()

    def present(self) -> None:
        self.window.present()

    def close(self) -> None:
        self.window.close()
