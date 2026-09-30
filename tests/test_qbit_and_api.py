import base64
import json
import os
import tempfile
import unittest

from tests import _env  # noqa: F401  (must come first)
from app import auth_store, jobs, qbittorrent
from app.config import settings

HASH = "a" * 40


class QbitHashTests(unittest.TestCase):
    def test_hash_validation(self):
        self.assertTrue(qbittorrent._valid_hash(HASH))
        self.assertTrue(qbittorrent._valid_hash("B" * 64))
        self.assertFalse(qbittorrent._valid_hash("SABnzbd_nzo_abc123"))
        self.assertFalse(qbittorrent._valid_hash(HASH + "&value=false"))
        self.assertFalse(qbittorrent._valid_hash(""))


class ActiveHashesFileTests(unittest.TestCase):
    def tearDown(self):
        settings.qbit_active_hashes_file = ""

    def test_off_by_default_is_noop(self):
        settings.qbit_active_hashes_file = ""
        jobs._save_active_hashes({HASH: {"movie_id": 1}})
        self.assertEqual(jobs._load_active_hashes(), {})

    def test_roundtrip(self):
        settings.qbit_active_hashes_file = os.path.join(tempfile.mkdtemp(), "active.json")
        jobs._save_active_hashes({HASH: {"movie_id": 1}})
        with open(settings.qbit_active_hashes_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {HASH: {"movie_id": 1}})
        self.assertIn(HASH, jobs._load_active_hashes())


class PauseUpgradeEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app

        if not auth_store.is_admin_configured():
            auth_store.set_admin("admin", "correct-horse-battery")
        cls.client = TestClient(app)
        cls.auth = {"Authorization": "Basic " + base64.b64encode(b"admin:correct-horse-battery").decode()}

    def test_requires_login(self):
        r = self.client.post("/api/movies/5/pause-upgrade", json={"minutes": 60})
        self.assertEqual(r.status_code, 401)

    def test_rejects_bad_minutes(self):
        for bad in ("60", -1, 0, 100000, True, 1.5):
            r = self.client.post("/api/movies/5/pause-upgrade", json={"minutes": bad}, headers=self.auth)
            self.assertEqual(r.status_code, 400, bad)

    def test_pauses(self):
        from app import workflows

        r = self.client.post("/api/movies/5/pause-upgrade", json={"minutes": 60}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(workflows._is_upgrade_paused(5))


if __name__ == "__main__":
    unittest.main()
