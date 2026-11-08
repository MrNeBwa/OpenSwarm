"""Unit tests for OpenSwarm Native Desktop Application."""

import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from openswarm import desktop, gui, main
from openswarm.desktop.config import DesktopConfig
from openswarm.desktop.bridge import DesktopBridge
from openswarm.desktop.installer import install_desktop, APP_ID, PET_APP_ID
from .helpers import Env


class DesktopApplicationTests(Env):
    def test_desktop_config_load_save(self) -> None:
        cfg = DesktopConfig()
        self.assertEqual(cfg.color_scheme, "default")
        self.assertEqual(cfg.zoom_level, 1.0)
        self.assertFalse(cfg.high_contrast)

        cfg.color_scheme = "dark"
        cfg.zoom_level = 1.25
        cfg.high_contrast = True
        cfg.save()

        loaded = DesktopConfig.load()
        self.assertEqual(loaded.color_scheme, "dark")
        self.assertEqual(loaded.zoom_level, 1.25)
        self.assertTrue(loaded.high_contrast)

    def test_desktop_installer_creates_files(self) -> None:
        installed = install_desktop()
        self.assertEqual(len(installed), 2)
        icon, entry = installed
        self.assertTrue(icon.is_file())
        self.assertTrue(entry.is_file())

        text = entry.read_text(encoding="utf-8")
        self.assertIn("[Desktop Entry]", text)
        self.assertIn(f"StartupWMClass={APP_ID}", text)
        self.assertIn("Icon=openswarm", text)
        self.assertIn("Actions=NewChat;Pet;Settings;", text)
        self.assertIn("gui", text)

        launcher = Path.home() / ".local" / "bin" / "openswarm-desktop"
        self.assertTrue(launcher.is_file())
        self.assertTrue(os.access(launcher, os.X_OK))

    def test_desktop_bridge_registration_and_dispatch(self) -> None:
        fake_window = mock.MagicMock()
        fake_window.web_view = mock.MagicMock()
        bridge = DesktopBridge(fake_window)

        # Test notify dispatch
        bridge._on_message(None, mock.MagicMock(to_string=lambda: json.dumps({"id": 1, "action": "notify", "title": "Test", "body": "Hello"})))
        fake_window.send_desktop_notification.assert_called_once_with("Test", "Hello")

        # Test set_zoom dispatch
        bridge._on_message(None, mock.MagicMock(to_string=lambda: json.dumps({"id": 2, "action": "set_zoom", "level": 1.3})))
        fake_window.set_zoom_level.assert_called_once_with(1.3)

        # Test toggle_pet dispatch
        bridge._on_message(None, mock.MagicMock(to_string=lambda: json.dumps({"id": 3, "action": "toggle_pet", "enabled": True})))
        fake_window.toggle_pet.assert_called_once_with(True)

    def test_desktop_main_check_and_install_flags(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = desktop.desktop_main(["--check"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("Desktop Window Backends", out)

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            rc2 = desktop.desktop_main(["--install"])
        self.assertEqual(rc2, 0)
        self.assertIn("Installed desktop application integration", buf2.getvalue())

    def test_main_cli_desktop_subcommand(self) -> None:
        with mock.patch("openswarm.gui.gui_main", return_value=0) as mocked_gui:
            with self.assertRaises(SystemExit) as cm:
                main.main(["desktop", "--check"])
            self.assertEqual(cm.exception.code, 0)
            mocked_gui.assert_called_once_with(["--check"])

    def test_gui_main_delegates_to_desktop(self) -> None:
        with mock.patch("openswarm.desktop.app.run_desktop_gtk") as mock_run_gtk:
            with mock.patch.object(gui, "gtk_available", return_value=True), \
                 mock.patch.object(gui, "start_server") as mock_start:
                mock_start.return_value = mock.MagicMock(url="http://127.0.0.1:8787/?token=test")
                with redirect_stdout(io.StringIO()):
                    rc = gui.gui_main(["--dir", str(self.plain("wd_test")), "--no-memory"])
                self.assertEqual(rc, 0)
                mock_run_gtk.assert_called_once()
