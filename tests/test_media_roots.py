import os
import tempfile
import unittest

from tests import _env  # noqa: F401  (must come first)
from app import preserve
from app.config import _normalize_media_roots, settings


class NormalizeMediaRootsTests(unittest.TestCase):
    def test_parses_comma_list(self):
        self.assertEqual(_normalize_media_roots("/media2, /media4"), ["/media2", "/media4"])

    def test_drops_relative_root_and_duplicates(self):
        self.assertEqual(_normalize_media_roots("media,/,/a/,/a,,"), ["/a"])

    def test_empty(self):
        self.assertEqual(_normalize_media_roots(""), [])


class PreserveRootsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root_a = os.path.join(self.tmp, "a")
        self.root_b = os.path.join(self.tmp, "b")
        os.makedirs(os.path.join(self.root_a, "Movie (2000)"))
        os.makedirs(os.path.join(self.root_b, "Other (2001)"))
        self._saved = settings.media_roots
        settings.media_roots = [self.root_a, self.root_b]

    def tearDown(self):
        settings.media_roots = self._saved

    def test_allowed_and_outside(self):
        self.assertTrue(preserve._is_within_allowed_root(os.path.join(self.root_a, "Movie (2000)", "m.mkv")))
        self.assertTrue(preserve._is_within_allowed_root(os.path.join(self.root_b, "Other (2001)", "o.mkv")))
        self.assertFalse(preserve._is_within_allowed_root(os.path.join(self.tmp, "c", "x.mkv")))
        # Prefix trick must not match ("/tmp/x/a-evil" is not under "/tmp/x/a")
        self.assertFalse(preserve._is_within_allowed_root(self.root_a + "-evil/x.mkv"))

    def test_each_root_gets_its_own_vault(self):
        src_b = os.path.join(self.root_b, "Other (2001)", "o.mkv")
        vault = preserve._vault_path(src_b)
        self.assertTrue(vault.startswith(os.path.join(self.root_b, preserve.VAULT_DIR_NAME)))
        self.assertTrue(preserve._is_within_vault(vault))
        self.assertEqual(len(preserve.vault_roots()), 2)

    def test_vault_path_outside_roots_raises(self):
        with self.assertRaises(ValueError):
            preserve._vault_path(os.path.join(self.tmp, "c", "x.mkv"))

    def test_nested_roots_pick_most_specific(self):
        inner = os.path.join(self.root_a, "Movie (2000)")
        settings.media_roots = [self.root_a, inner]
        self.assertEqual(preserve._vault_root_for(os.path.join(inner, "m.mkv")), os.path.join(inner, preserve.VAULT_DIR_NAME))


if __name__ == "__main__":
    unittest.main()
