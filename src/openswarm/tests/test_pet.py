import http.client
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from openswarm import gui, main, web
from .helpers import Env


def get(port, path, token="tok"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"X-Token": token})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def post_json(port, path, data, token="tok"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(data).encode("utf-8")
    c.request("POST", path, body=body, headers={"X-Token": token, "Content-Type": "application/json"})
    r = c.getresponse()
    resp_body = r.read()
    c.close()
    return r.status, json.loads(resp_body) if resp_body else {}


class PetTests(Env):
    def test_main_cli_pet_subcommand(self):
        with mock.patch("openswarm.gui.gui_main", return_value=0) as mocked_gui:
            with self.assertRaises(SystemExit) as cm:
                main.main(["pet", "--check"])
            self.assertEqual(cm.exception.code, 0)
            mocked_gui.assert_called_once_with(["--pet", "--check"])

    def test_gui_main_pet_flag_appends_url(self):
        opened = []
        with mock.patch.object(gui, "gtk_available", return_value=False), \
             mock.patch.object(gui, "chromium_available", return_value=False), \
             mock.patch.object(gui.webbrowser, "open", side_effect=lambda u: opened.append(u)), \
             mock.patch.object(gui, "wait_for_process_or_ctrl_c", return_value=None), \
             mock.patch("openswarm.gui.discover_models", new=mock.AsyncMock(return_value=[])):
            with redirect_stdout(io.StringIO()):
                rc = gui.gui_main(["--pet", "--dir", str(self.plain("wd_pet")), "--no-memory"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(opened), 1)
        self.assertIn("&pet=1", opened[0])

    def test_web_ui_contains_pet_components(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            st, page = get(port, "/?token=tok")
            self.assertEqual(st, 200)
            html = page.decode("utf-8")
            self.assertIn('id="swarm-pet"', html)
            self.assertIn('id="pet-standalone-view"', html)
            self.assertIn('id="petmodal"', html)
            self.assertIn('PET_CONFIG', html)
            self.assertIn('getPetSVG', html)
            self.assertIn('raiseAppWindow', html)
            self.assertIn('pet-skin-card', html)
            self.assertIn('/pet', html)
        finally:
            h.stop()

    def test_window_raise_endpoint(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            st, res = post_json(port, "/api/window/raise", {})
            self.assertEqual(st, 200)
            self.assertTrue(res.get("ok"))
            self.assertIn("raised", res)
        finally:
            h.stop()

    def test_window_pin_endpoint(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            st, res = post_json(port, "/api/window/pin", {"pinned": True})
            self.assertEqual(st, 200)
            self.assertTrue(res.get("ok"))
            self.assertIn("pinned", res)

            st, res2 = post_json(port, "/api/window/pin", {"pinned": False})
            self.assertEqual(st, 200)
            self.assertTrue(res2.get("ok"))
        finally:
            h.stop()

    def test_screen_capture_endpoint(self):
        wd = self.plain("wd_screen")
        h = gui.start_server(self.cfg, wd, token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            def fake_run(cmd, *args, **kwargs):
                target = Path(cmd[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")
                class Res:
                    returncode = 0
                return Res()

            with mock.patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}" if x in ("grim", "grimblast", "import", "maim", "scrot") else None), \
                 mock.patch("subprocess.run", side_effect=fake_run):
                st, res = post_json(port, "/api/screen/capture", {})
                self.assertEqual(st, 200)
                self.assertTrue(res.get("ok"))
                self.assertIn("path", res)
                self.assertTrue(res["path"].endswith(".png"))
                screen_file = wd / res["path"]
                self.assertTrue(screen_file.is_file())
                self.assertEqual(screen_file.read_bytes(), b"\x89PNG\r\n\x1a\nfakeimage")
        finally:
            h.stop()

    def test_screen_capture_no_tools(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            with mock.patch("shutil.which", return_value=None):
                st, res = post_json(port, "/api/screen/capture", {})
                self.assertEqual(st, 200)
                self.assertFalse(res.get("ok"))
                self.assertIn("error", res)
        finally:
            h.stop()

    def test_gui_main_overlay_and_ontop_flags(self):
        opened = []
        with mock.patch.object(gui, "gtk_available", return_value=False), \
             mock.patch.object(gui, "chromium_available", return_value=False), \
             mock.patch.object(gui.webbrowser, "open", side_effect=lambda u: opened.append(u)), \
             mock.patch.object(gui, "wait_for_process_or_ctrl_c", return_value=None), \
             mock.patch("openswarm.gui.discover_models", new=mock.AsyncMock(return_value=[])):
            with redirect_stdout(io.StringIO()):
                rc = gui.gui_main(["--pet", "--overlay", "--ontop", "--dir", str(self.plain("wd_pet_over")), "--no-memory"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(opened), 1)
        self.assertIn("&pet=1", opened[0])
        self.assertIn("&overlay=1", opened[0])

    def test_web_ui_pet_overlay_elements(self):
        h = gui.start_server(self.cfg, self.plain("wd"), token="tok")
        port = int(h.url.split(":")[2].split("/")[0])
        try:
            st, page = get(port, "/?token=tok")
            self.assertEqual(st, 200)
            html = page.decode("utf-8")
            self.assertIn('id="pet-stand-pin-btn"', html)
            self.assertIn('id="pet-stand-screen-btn"', html)
            self.assertIn('id="pet-stand-voice"', html)
            self.assertIn('id="pet-always-ontop"', html)
            self.assertIn('id="pet-overlay-mode"', html)
            self.assertIn('captureAndAskScreen', html)
            self.assertIn('togglePetStandPin', html)
            self.assertIn('togglePetVoice', html)
        finally:
            h.stop()
