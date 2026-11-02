import http.client
import io
import json
from contextlib import redirect_stdout
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
