import base64
import os
import tempfile
import unittest
from unittest import mock

import httpx

from tests import _env  # noqa: F401  (must come first)
from app import auth, auth_store, friendly, radarr, settings_store
from app.config import settings

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}


class SettingsValidationTests(unittest.TestCase):
    def test_media_roots_normalised(self):
        self.assertEqual(settings_store.validate({"media_roots": ["/media2/", " /media4"]})["media_roots"], ["/media2", "/media4"])
        self.assertEqual(settings_store.validate({"media_roots": "/a,/b"})["media_roots"], ["/a", "/b"])

    def test_media_roots_rejects_root_and_relative(self):
        for bad in (["/"], ["media"], [], "x", [3]):
            with self.assertRaises(ValueError):
                settings_store.validate({"media_roots": bad})

    def test_types(self):
        with self.assertRaises(ValueError):
            settings_store.validate({"digest_hour_utc": 30})
        with self.assertRaises(ValueError):
            settings_store.validate({"discord_startup_notice": "yes"})
        with self.assertRaises(ValueError):
            settings_store.validate({"discord_username": "  "})
        self.assertEqual(settings_store.validate({"discord_username": " Bot "})["discord_username"], "Bot")


class FriendlyErrorTests(unittest.TestCase):
    def test_no_secret_echo(self):
        req = httpx.Request("GET", "http://t/api/v2?apikey=SECRET9")
        exc = httpx.HTTPStatusError("x", request=req, response=httpx.Response(401, request=req))
        self.assertIn("rejected", friendly.explain(exc, "Tautulli"))
        self.assertNotIn("SECRET9", friendly.explain(RuntimeError("GET http://t/?apikey=SECRET9"), "Tautulli"))


class SetupEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app

        if not auth_store.is_admin_configured():
            auth_store.set_admin("admin", "correct-horse-battery")
        cls.client = TestClient(app)

    def setUp(self):
        auth._failed_attempts.clear()

    def test_media_roots_detection(self):
        tmp = tempfile.mkdtemp()
        visible = os.path.join(tmp, "Movies")
        os.makedirs(visible)
        with mock.patch.object(radarr, "root_folders", mock.AsyncMock(return_value=[visible + "/", "/not-mounted/Movies"])):
            r = self.client.get("/api/setup/media-roots", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["suggested"], ["/" + [p for p in tmp.split("/") if p][0]])
        self.assertEqual([f["visible"] for f in data["folders"]], [True, False])

    def test_media_roots_requires_login(self):
        self.assertEqual(self.client.get("/api/setup/media-roots").status_code, 401)

    def test_qbit_unreachable_is_friendly(self):
        r = self.client.post("/api/setup/test-qbit", json={"url": "http://127.0.0.1:9", "username": "a", "password": "b"}, headers=AUTH)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Couldn't reach qBittorrent", r.json()["detail"])

    def test_settings_api_validates(self):
        r = self.client.post("/api/settings", json={"media_roots": ["/"]}, headers=AUTH)
        self.assertEqual(r.status_code, 400)
        saved = settings.media_roots
        r = self.client.post("/api/settings", json={"media_roots": ["/media2", "/media4"]}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(settings.media_roots, ["/media2", "/media4"])
        settings.media_roots = saved


if __name__ == "__main__":
    unittest.main()
