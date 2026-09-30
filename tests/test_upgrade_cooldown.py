import datetime
import os
import tempfile
import unittest

from tests import _env  # noqa: F401  (must come first)
from app.config import settings
from app.jobs import JobStore


def _ago(minutes: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).isoformat()


class UpgradeCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = JobStore(os.path.join(tempfile.mkdtemp(), "jobs.json"))
        settings.upgrade_retry_cooldown_minutes = 30
        settings.upgrade_no_release_cooldown_hours = 12

    async def test_never_checked(self):
        self.assertTrue(await self.store.should_check_upgrade(1))

    async def test_normal_cooldown(self):
        await self.store.record_upgrade_check(1)
        self.assertFalse(await self.store.should_check_upgrade(1))
        self.store.upgrade_checks["1"]["at"] = _ago(31)
        self.assertTrue(await self.store.should_check_upgrade(1))

    async def test_no_release_uses_long_cooldown(self):
        await self.store.record_upgrade_check(2, no_release_found=True)
        self.store.upgrade_checks["2"]["at"] = _ago(60)
        self.assertFalse(await self.store.should_check_upgrade(2))
        self.store.upgrade_checks["2"]["at"] = _ago(12 * 60 + 1)
        self.assertTrue(await self.store.should_check_upgrade(2))

    async def test_legacy_string_entries_still_work(self):
        self.store.upgrade_checks["3"] = _ago(5)
        self.assertFalse(await self.store.should_check_upgrade(3))
        self.store.upgrade_checks["3"] = _ago(45)
        self.assertTrue(await self.store.should_check_upgrade(3))


if __name__ == "__main__":
    unittest.main()
