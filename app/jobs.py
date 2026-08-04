import asyncio
import datetime
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

from . import preserve, radarr
from .config import settings
from .logger import log

JobKind = Literal["upgrade", "downgrade"]
JobStatus = Literal["searching", "downloading", "importing", "done", "failed", "skipped"]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Job:
    movie_id: int
    kind: JobKind
    status: JobStatus
    plex_rating_key: str = ""
    plex_added_at: Optional[int] = None
    original_quality_profile_id: int = 0
    original_file_size: int = 0
    original_file_path: str = ""
    preserved_path: str = ""
    history_baseline_id: int = 0
    attempt_count: int = 0
    tried_release_guids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


class JobStore:
    """Single shared store for both workflows, persisted to jobs.json.

    active_jobs is keyed by str(movie_id) so an upgrade job and a downgrade
    job can never coexist for the same movie — whichever workflow gets there
    first holds the key until it finishes, fails, or is reverted on startup.
    """

    MAX_HISTORY = 300

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self.active_jobs: dict[str, Job] = {}
        self.downgrade_checks: dict[str, str] = {}
        self.upgrade_checks: dict[str, str] = {}
        self.history: list[dict[str, Any]] = []
        # path -> {"since": when WE moved it, "kind": upgrade/downgrade,
        # "movie_id": ..., "original_file_path": ...}. Filesystem timestamps
        # (mtime, ctime) are NOT reliable for "since" — confirmed live that
        # both reflect the original file's age rather than the move,
        # especially over NFS, which caused real files to be auto-deleted
        # within minutes instead of after 7 days. This is the actual source
        # of truth now. kind matters because the two workflows have opposite
        # retention policies: downgrade deletes the preserved (big) original
        # once the small replacement is confirmed; upgrade keeps the
        # preserved (small) original permanently and instead reverts the big
        # 4K version back to it after KEEP_ORIGINAL_DAYS, per explicit
        # instruction — 4K is a temporary trial, not a permanent swap.
        self.preserved_files: dict[str, dict] = {}

    async def load(self) -> None:
        if not os.path.exists(self._path):
            return
        with open(self._path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.active_jobs = {k: Job(**v) for k, v in raw.get("active_jobs", {}).items()}
        self.downgrade_checks = raw.get("downgrade_checks", {})
        self.upgrade_checks = raw.get("upgrade_checks", {})
        self.history = raw.get("history", [])
        self.preserved_files = raw.get("preserved_files", {})

    async def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {
            "active_jobs": {k: asdict(v) for k, v in self.active_jobs.items()},
            "downgrade_checks": self.downgrade_checks,
            "upgrade_checks": self.upgrade_checks,
            "history": self.history,
            "preserved_files": self.preserved_files,
        }
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, self._path)

    async def try_claim(self, movie_id: int, kind: JobKind, **fields: Any) -> Optional[Job]:
        """Claim the job slot for a movie. Returns None if already claimed by either workflow."""
        key = str(movie_id)
        async with self._lock:
            if key in self.active_jobs:
                return None
            job = Job(movie_id=movie_id, kind=kind, status="searching", **fields)
            self.active_jobs[key] = job
            await self._save()
            return job

    async def get(self, movie_id: int) -> Optional[Job]:
        return self.active_jobs.get(str(movie_id))

    async def update(self, movie_id: int, **fields: Any) -> None:
        async with self._lock:
            key = str(movie_id)
            job = self.active_jobs.get(key)
            if job is None:
                return
            for name, value in fields.items():
                setattr(job, name, value)
            job.updated_at = _now()
            await self._save()

    async def release(self, movie_id: int) -> None:
        async with self._lock:
            self.active_jobs.pop(str(movie_id), None)
            await self._save()

    async def is_claimed(self, movie_id: int) -> bool:
        return str(movie_id) in self.active_jobs

    async def record_preserved_file(self, path: str, kind: JobKind, movie_id: int, original_file_path: str) -> None:
        async with self._lock:
            self.preserved_files[path] = {
                "since": _now(),
                "kind": kind,
                "movie_id": movie_id,
                "original_file_path": original_file_path,
            }
            await self._save()

    async def forget_preserved_file(self, path: str) -> None:
        async with self._lock:
            self.preserved_files.pop(path, None)
            await self._save()

    def preserved_since(self, path: str) -> Optional[str]:
        entry = self.preserved_files.get(path)
        if entry is None:
            return None
        if isinstance(entry, str):  # legacy format, predates kind tracking
            return entry
        return entry.get("since")

    def preserved_info(self, path: str) -> Optional[dict]:
        """Full record (kind/movie_id/original_file_path) for a preserved
        file, or None if it predates that tracking (legacy plain-timestamp
        format) or isn't recorded at all. Callers must treat a None result
        as "unknown kind" and fall back to the safe (downgrade-style, never
        guess) behavior.
        """
        entry = self.preserved_files.get(path)
        if entry is None or isinstance(entry, str):
            return None
        return entry

    async def record_history(
        self,
        movie_id: int,
        movie_title: str,
        kind: JobKind,
        outcome: str,
        detail: str,
        poster_url: Optional[str] = None,
    ) -> None:
        """Append a completed job's outcome for the History tab. Capped at
        MAX_HISTORY entries (oldest dropped) since active_jobs itself only
        ever holds in-flight work — this is the only record of what happened.
        """
        async with self._lock:
            self.history.insert(
                0,
                {
                    "movie_id": movie_id,
                    "movie_title": movie_title,
                    "kind": kind,
                    "outcome": outcome,
                    "detail": detail,
                    "poster_url": poster_url,
                    "at": _now(),
                },
            )
            self.history = self.history[: self.MAX_HISTORY]
            await self._save()

    async def record_downgrade_check(self, movie_id: int) -> None:
        async with self._lock:
            self.downgrade_checks[str(movie_id)] = _now()
            await self._save()

    async def should_check_downgrade(self, movie_id: int) -> bool:
        last = self.downgrade_checks.get(str(movie_id))
        if last is None:
            return True
        last_dt = datetime.datetime.fromisoformat(last)
        age = datetime.datetime.now(datetime.timezone.utc) - last_dt
        return age.days >= settings.downgrade_recheck_days

    async def record_upgrade_check(self, movie_id: int) -> None:
        async with self._lock:
            self.upgrade_checks[str(movie_id)] = _now()
            await self._save()

    async def should_check_upgrade(self, movie_id: int) -> bool:
        """Cooldown between upgrade attempts for the same movie.

        Confirmed live: a Plex session that never properly closes (a stuck
        "paused forever" session, say) would otherwise make the poll loop
        re-claim and re-search the same movie every cycle indefinitely —
        hammering indexers and repeatedly renaming an already-preserved file.
        This caps retries to once per UPGRADE_RETRY_COOLDOWN_MINUTES.
        """
        last = self.upgrade_checks.get(str(movie_id))
        if last is None:
            return True
        last_dt = datetime.datetime.fromisoformat(last)
        age = datetime.datetime.now(datetime.timezone.utc) - last_dt
        return age.total_seconds() >= settings.upgrade_retry_cooldown_minutes * 60

    async def recover_on_startup(self) -> list[tuple[int, JobKind]]:
        """Re-validate every in-flight job against live Radarr state.

        A job surviving a crash is only trustworthy if Radarr shows real
        evidence of progress (a queue entry or an import). Anything else is
        reverted (quality profile restored, job dropped) rather than resumed
        on a guess. Returns (movie_id, kind) pairs whose monitor task the
        caller should restart — this store has no reference to the workflow
        functions themselves, to avoid a circular import.
        """
        to_resume: list[tuple[int, JobKind]] = []

        for key, job in list(self.active_jobs.items()):
            try:
                in_queue = await radarr.queue_has_movie(job.movie_id)
                new_event = await radarr.new_history_event_type(job.movie_id, job.history_baseline_id)
                imported = new_event == "downloadFolderImported"
            except Exception as exc:  # noqa: BLE001
                await log(f"jobs: recovery check failed for movie {job.movie_id}: {exc} — leaving job as-is for next cycle")
                continue

            if imported:
                await log(f"jobs: recovered job for movie {job.movie_id} — import already completed, marking done")
                if job.original_quality_profile_id:
                    try:
                        await radarr.set_quality_profile(job.movie_id, job.original_quality_profile_id)
                    except Exception as exc:  # noqa: BLE001
                        await log(f"jobs: failed to revert quality profile for movie {job.movie_id}: {exc}")
                await self.update(job.movie_id, status="done")
                await self.release(job.movie_id)
                continue

            if in_queue:
                await log(f"jobs: recovered job for movie {job.movie_id} — still in Radarr queue, resuming monitor")
                await self.update(job.movie_id, status="downloading")
                to_resume.append((job.movie_id, job.kind))
                continue

            await log(
                f"jobs: reverting stale job for movie {job.movie_id} "
                f"(no queue entry, no import evidence after restart)"
            )
            if job.original_quality_profile_id:
                try:
                    await radarr.set_quality_profile(job.movie_id, job.original_quality_profile_id)
                except Exception as exc:  # noqa: BLE001
                    await log(f"jobs: failed to revert quality profile for movie {job.movie_id}: {exc}")
            # Confirmed live (Under Siege): a job reverted here without this
            # left the movie with NO file at all — its original was already
            # moved to the vault before the crash/restart, and this path
            # never knew to move it back. A movie must never end a "revert"
            # with zero playable copies.
            if job.preserved_path and job.original_file_path:
                try:
                    restored = await preserve.revert_no_gain(job.preserved_path, "", job.original_file_path)
                    if restored:
                        await self.forget_preserved_file(job.preserved_path)
                        await radarr.rescan_movie(job.movie_id)
                        await log(f"jobs: restored movie {job.movie_id}'s original file after reverting its stale job")
                    else:
                        await log(f"jobs: WARNING — movie {job.movie_id}'s stale job reverted but its original ('{job.preserved_path}') could not be auto-restored — check manually")
                except Exception as exc:  # noqa: BLE001
                    await log(f"jobs: WARNING — failed to restore movie {job.movie_id}'s original after stale revert: {exc} — check '{job.preserved_path}' manually")
            await self.release(job.movie_id)

        return to_resume


store = JobStore(settings.jobs_file)
