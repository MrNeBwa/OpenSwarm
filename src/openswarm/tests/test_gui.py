import http.client, io, json, os, threading
from contextlib import redirect_stdout
from unittest import mock

from openswarm import gui
from .helpers import Env


def get(port, path, token="tok"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"X-Token": token}); r = c.getresponse(); body = r.read(); c.close()
    return r.status, body


class Gui(Env):
    def test_server_thread_start_and_clean_stop(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        st, body = get(port, "/api/state"); self.assertEqual(st, 200); self.assertIn("modes", json.loads(body))
        st, page = get(port, "/?token=tok"); self.assertEqual(st, 200); self.assertIn(b'id="orbcv"', page)  # the sphere ships in the page
        self.assertEqual(get(port, "/api/state", token="bad")[0], 403)
        h.stop()
        self.assertFalse(h.thread.is_alive())
        with self.assertRaises(OSError): get(port, "/api/state")

    def test_backend_choice(self):
        with mock.patch.object(gui, "gtk_available", return_value=True), mock.patch.object(gui, "chromium_available", return_value=True):
            self.assertEqual(gui.choose_backend("auto"), "gtk"); self.assertEqual(gui.choose_backend("chromium"), "chromium")
            self.assertEqual(gui.choose_backend("browser"), "browser")
        with mock.patch.object(gui, "gtk_available", return_value=False), mock.patch.object(gui, "chromium_available", return_value=True):
            self.assertEqual(gui.choose_backend("auto"), "chromium"); self.assertEqual(gui.choose_backend("gtk"), "chromium")
        with mock.patch.object(gui, "gtk_available", return_value=False), mock.patch.object(gui, "chromium_available", return_value=False):
            self.assertEqual(gui.choose_backend("auto"), "browser")

    def test_check_flag_reports_without_starting_anything(self):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(gui, "gtk_available", return_value=False), mock.patch.object(gui, "chromium_available", return_value=False):
            self.assertEqual(gui.gui_main(["--check"]), 0)
        out = buf.getvalue(); self.assertIn("python-gobject", out); self.assertIn("would use: browser", out)

    def test_desktop_entry_and_icon(self):
        files = gui.install_desktop()
        icon, entry = files
        self.assertTrue(icon.read_text().startswith("<svg")); text = entry.read_text()
        self.assertTrue(text.startswith("[Desktop Entry]")); self.assertIn(" gui\n", text); self.assertIn("Icon=openswarm", text)
        self.assertIn(f"StartupWMClass={gui.APP_ID}", text); self.assertEqual(entry.name, f"{gui.APP_ID}.desktop")
        old = entry.parent / "openswarm.desktop"; old.write_text("old")
        gui.install_desktop(); self.assertFalse(old.exists())  # legacy entry replaced

    def test_gui_main_falls_back_when_gtk_window_fails(self):
        opened = []
        with mock.patch.object(gui, "gtk_available", return_value=True), mock.patch.object(gui, "chromium_available", return_value=False), \
             mock.patch.object(gui, "run_gtk", side_effect=RuntimeError("no display")), \
             mock.patch.object(gui.webbrowser, "open", side_effect=lambda u: opened.append(u)), \
             mock.patch.object(gui, "wait_for_process_or_ctrl_c", return_value=None), \
             mock.patch("openswarm.gui.discover_models", new=mock.AsyncMock(return_value=[])):
            with redirect_stdout(io.StringIO()) as buf:
                rc = gui.gui_main(["--dir", str(self.plain("wd2")), "--no-memory"])
        self.assertEqual(rc, 0); self.assertEqual(len(opened), 1); self.assertIn("?token=", opened[0])
        self.assertIn("falling back", buf.getvalue())
        self.assertFalse(any(t.name == "openswarm-server" and t.is_alive() for t in threading.enumerate()))
