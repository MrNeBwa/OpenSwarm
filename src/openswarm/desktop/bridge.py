"""Bi-directional IPC bridge between WebKitGTK and native desktop application."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("openswarm.desktop.bridge")

NATIVE_JS_BRIDGE = """
(function() {
  if (window.OpenSwarmNative) return;

  const callbacks = new Map();
  let nextCallId = 1;

  window.OpenSwarmNative = {
    isNativeDesktop: true,
    platform: "linux",

    callNative(action, payload = {}) {
      return new Promise((resolve, reject) => {
        const id = nextCallId++;
        callbacks.set(id, { resolve, reject });
        try {
          if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.openswarm) {
            window.webkit.messageHandlers.openswarm.postMessage(JSON.stringify({
              id, action, ...payload
            }));
          } else {
            resolve({ ok: false, error: "Native handler unavailable" });
          }
        } catch (e) {
          callbacks.delete(id);
          reject(e);
        }
      });
    },

    _onNativeReply(id, result, error) {
      const cb = callbacks.get(id);
      if (cb) {
        callbacks.delete(id);
        if (error) cb.reject(new Error(error));
        else cb.resolve(result);
      }
    },

    pickDirectory(initialPath = "") {
      return this.callNative("pick_directory", { path: initialPath });
    },

    togglePet(enabled = null) {
      return this.callNative("toggle_pet", { enabled });
    },

    notify(title, body, icon = "openswarm") {
      return this.callNative("notify", { title, body, icon });
    },

    updateStatus(data) {
      return this.callNative("update_status", data);
    },

    openPreferences() {
      return this.callNative("open_preferences");
    },

    openShortcuts() {
      return this.callNative("open_shortcuts");
    },

    openAbout() {
      return this.callNative("open_about");
    },

    setZoom(level) {
      return this.callNative("set_zoom", { level });
    },

    toggleHighContrast(enabled = null) {
      return this.callNative("toggle_contrast", { enabled });
    }
  };

  // Dispatch event so UI knows native capabilities are ready
  window.dispatchEvent(new CustomEvent("openswarm-native-ready", { detail: window.OpenSwarmNative }));
})();
"""

HIGH_CONTRAST_CSS = """
/* OpenSwarm Desktop Accessibility High-Contrast Mode */
body[data-high-contrast="true"],
.high-contrast-mode {
  --bg: #000000 !important;
  --panel: #0d0d0d !important;
  --panel2: #141414 !important;
  --card: #181818 !important;
  --text: #ffffff !important;
  --text2: #e0e0e0 !important;
  --line: #444444 !important;
  --line2: #666666 !important;
  --accent: #58a6ff !important;
  --accent2: #79c0ff !important;
}

*:focus-visible {
  outline: 3px solid #58a6ff !important;
  outline-offset: 2px !important;
}
"""


class DesktopBridge:
    def __init__(self, window: Any) -> None:
        self.window = window
        self._handlers: dict[str, Callable[[int, dict[str, Any]], None]] = {
            "pick_directory": self._handle_pick_directory,
            "toggle_pet": self._handle_toggle_pet,
            "notify": self._handle_notify,
            "update_status": self._handle_update_status,
            "open_preferences": self._handle_open_preferences,
            "open_shortcuts": self._handle_open_shortcuts,
            "open_about": self._handle_open_about,
            "set_zoom": self._handle_set_zoom,
            "toggle_contrast": self._handle_toggle_contrast,
        }

    def register(self, user_content_manager: Any) -> None:
        try:
            import gi
            gi.require_version("WebKit", "6.0")
            from gi.repository import WebKit

            user_content_manager.register_script_message_handler("openswarm")
            user_content_manager.connect("script-message-received::openswarm", self._on_message)

            # Inject the JS bridge early
            script = WebKit.UserScript.new(
                NATIVE_JS_BRIDGE,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START,
                [],
                [],
            )
            user_content_manager.add_script(script)
        except Exception as exc:
            logger.warning("Could not register WebKit script handler: %s", exc)

    def _on_message(self, _manager: Any, js_value: Any) -> None:
        try:
            raw_str = js_value.to_string() if hasattr(js_value, "to_string") else str(js_value)
            data = json.loads(raw_str)
            if not isinstance(data, dict):
                return
            msg_id = int(data.get("id", 0))
            action = str(data.get("action", ""))
            handler = self._handlers.get(action)
            if handler:
                handler(msg_id, data)
            else:
                logger.debug("Unhandled native bridge action: %s", action)
                self.reply(msg_id, {"ok": False, "error": f"Unknown action: {action}"})
        except Exception as exc:
            logger.error("Error processing native bridge message: %s", exc)

    def reply(self, msg_id: int, result: Any, error: str | None = None) -> None:
        if not msg_id or not hasattr(self.window, "web_view"):
            return
        res_json = json.dumps(result)
        err_json = json.dumps(error) if error else "null"
        js = f"window.OpenSwarmNative && window.OpenSwarmNative._onNativeReply({msg_id}, {res_json}, {err_json});"
        try:
            self.window.web_view.evaluate_javascript(js, -1, None, None, None, None, None)
        except Exception as exc:
            logger.debug("evaluate_javascript reply failed: %s", exc)

    def _handle_pick_directory(self, msg_id: int, data: dict[str, Any]) -> None:
        from .dialogs import show_folder_chooser

        def on_picked(path_str: str | None) -> None:
            if path_str:
                self.reply(msg_id, {"ok": True, "path": path_str})
                if hasattr(self.window, "on_directory_picked"):
                    self.window.on_directory_picked(path_str)
            else:
                self.reply(msg_id, {"ok": False, "cancelled": True})

        init_path = str(data.get("path") or "")
        show_folder_chooser(self.window, init_path, on_picked)

    def _handle_toggle_pet(self, msg_id: int, data: dict[str, Any]) -> None:
        enabled = data.get("enabled")
        if hasattr(self.window, "toggle_pet"):
            st = self.window.toggle_pet(enabled)
            self.reply(msg_id, {"ok": True, "active": st})
        else:
            self.reply(msg_id, {"ok": False, "error": "Pet toggle not supported on this window"})

    def _handle_notify(self, msg_id: int, data: dict[str, Any]) -> None:
        title = str(data.get("title") or "OpenSwarm")
        body = str(data.get("body") or "")
        if hasattr(self.window, "send_desktop_notification"):
            self.window.send_desktop_notification(title, body)
        self.reply(msg_id, {"ok": True})

    def _handle_update_status(self, msg_id: int, data: dict[str, Any]) -> None:
        if hasattr(self.window, "update_title_and_status"):
            self.window.update_title_and_status(data)
        self.reply(msg_id, {"ok": True})

    def _handle_open_preferences(self, msg_id: int, _data: dict[str, Any]) -> None:
        if hasattr(self.window, "show_preferences"):
            self.window.show_preferences()
        self.reply(msg_id, {"ok": True})

    def _handle_open_shortcuts(self, msg_id: int, _data: dict[str, Any]) -> None:
        if hasattr(self.window, "show_shortcuts"):
            self.window.show_shortcuts()
        self.reply(msg_id, {"ok": True})

    def _handle_open_about(self, msg_id: int, _data: dict[str, Any]) -> None:
        if hasattr(self.window, "show_about"):
            self.window.show_about()
        self.reply(msg_id, {"ok": True})

    def _handle_set_zoom(self, msg_id: int, data: dict[str, Any]) -> None:
        level = float(data.get("level", 1.0))
        if hasattr(self.window, "set_zoom_level"):
            self.window.set_zoom_level(level)
        self.reply(msg_id, {"ok": True, "zoom": level})

    def _handle_toggle_contrast(self, msg_id: int, data: dict[str, Any]) -> None:
        enabled = data.get("enabled")
        if hasattr(self.window, "toggle_high_contrast"):
            res = self.window.toggle_high_contrast(enabled)
            self.reply(msg_id, {"ok": True, "high_contrast": res})
        else:
            self.reply(msg_id, {"ok": False})
