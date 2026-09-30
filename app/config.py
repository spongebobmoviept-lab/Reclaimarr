import os

APP_VERSION = "1.1.0"


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes")


def _normalize_media_roots(raw: str) -> list[str]:
    """Parse MEDIA_ROOTS (comma-separated container paths) into a clean list.

    Every root must be an absolute path and must not be "/" itself -- a root
    of "/" would make every path on the container "allowed", defeating the
    whole point of the guard in preserve.py. Invalid entries are dropped.
    """
    roots: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part or not part.startswith("/"):
            continue
        norm = os.path.normpath(part)
        if norm in ("/", "//") or norm in roots:
            continue
        roots.append(norm)
    return roots


class Settings:
    def __init__(self) -> None:
        # Blank by default (not required at startup) so a fresh, unconfigured
        # copy of the app can boot and serve the first-run setup wizard
        # instead of crashing before FastAPI even starts. connections_store
        # overrides (set via the wizard or the Connections settings page)
        # fill these in live, no restart needed.
        self.plex_url = os.environ.get("PLEX_URL", "")
        self.plex_token = os.environ.get("PLEX_TOKEN", "")

        self.radarr_url = os.environ.get("RADARR_URL", "")
        self.radarr_api_key = os.environ.get("RADARR_API_KEY", "")

        # Optional: force-start + top-priority an upgrade's download directly
        # in qBittorrent the moment its torrent hash is known, so a 4K grab
        # for something being watched right now doesn't wait behind the
        # normal queue -- see app/qbittorrent.py. Empty QBIT_URL skips the
        # priority boost entirely; the upgrade itself works either way.
        self.qbit_url = os.environ.get("QBIT_URL", "").strip().rstrip("/")
        self.qbit_username = os.environ.get("QBIT_USERNAME", "admin")
        self.qbit_password = os.environ.get("QBIT_PASSWORD", "")
        # Optional coordination file for an external qBittorrent queue
        # manager/optimizer script: Reclaimarr writes the hashes it has
        # force-started here as JSON ({"<hash>": {"movie_id": .., "since": ..}})
        # so that other tool can leave them alone. Empty (the default) turns
        # the file off completely.
        self.qbit_active_hashes_file = os.environ.get("QBIT_ACTIVE_HASHES_FILE", "").strip()

        # Container paths Reclaimarr is allowed to rename/move/delete files
        # under -- normally the same path(s) Radarr uses for its movie root
        # folders. Comma-separated; each root gets its own safety vault
        # (<root>/reclaimarr-vault) so preserving a file is always a same-
        # filesystem rename. Anything outside these roots is never touched.
        self.media_roots = _normalize_media_roots(os.environ.get("MEDIA_ROOTS", "/media2"))

        self.tautulli_url = os.environ.get("TAUTULLI_URL", "")
        self.tautulli_api_key = os.environ.get("TAUTULLI_API_KEY", "")

        self.radarr_4k_profile_id = _env_int("RADARR_4K_PROFILE_ID", 0)
        self.radarr_1080p_profile_id = _env_int("RADARR_1080P_PROFILE_ID", 0)

        self.downgrade_threshold_gb = _env_float("DOWNGRADE_THRESHOLD_GB", 15)
        self.downgrade_size_ratio = _env_float("DOWNGRADE_SIZE_RATIO", 0.5)
        self.max_4k_release_size_gb = _env_float("MAX_4K_RELEASE_SIZE_GB", 80)

        self.poll_interval_seconds = _env_int("POLL_INTERVAL_SECONDS", 60)
        self.download_monitor_interval_seconds = _env_int("DOWNLOAD_MONITOR_INTERVAL_SECONDS", 120)
        self.upgrade_search_timeout_seconds = _env_int("UPGRADE_SEARCH_TIMEOUT_SECONDS", 600)
        self.max_download_attempts = _env_int("MAX_DOWNLOAD_ATTEMPTS", 3)

        self.downgrade_scan_hour_utc = _env_int("DOWNGRADE_SCAN_HOUR_UTC", 3)
        # Eligibility gate for the routine scan: a movie must have sat at
        # full quality for at least this many days since being ADDED to the
        # library — not whether it's been watched. Explicit instruction:
        # "idc if its watched, if it's added and been a while, downgrade it
        # no matter what" — gives fresh adds (e.g. just-released movies
        # someone wants to watch in full quality) a grace period without
        # requiring a watch event that might never come.
        self.downgrade_min_age_days = _env_int("DOWNGRADE_MIN_AGE_DAYS", 30)
        # A movie watched within this many days gets deferred, regardless of
        # how old it is — actively/frequently rewatched movies naturally
        # keep re-triggering this and staying at full quality on their own,
        # with no separate "popular movie" tracking needed.
        self.downgrade_recent_watch_days = _env_int("DOWNGRADE_RECENT_WATCH_DAYS", 14)
        self.downgrade_recheck_days = _env_int("DOWNGRADE_RECHECK_DAYS", 7)
        self.downgrade_scan_delay_seconds = _env_float("DOWNGRADE_SCAN_DELAY_SECONDS", 3)
        self.downgrade_max_concurrent = _env_int("DOWNGRADE_MAX_CONCURRENT", 3)
        self.upgrade_retry_cooldown_minutes = _env_int("UPGRADE_RETRY_COOLDOWN_MINUTES", 30)
        # Much longer than the normal retry cooldown above — a brand-new
        # release with no 4K version on any indexer yet isn't going to
        # suddenly appear within the next 30 minutes, so there's no point
        # burning another multi-minute indexer search that often for it.
        self.upgrade_no_release_cooldown_hours = _env_int("UPGRADE_NO_RELEASE_COOLDOWN_HOURS", 12)
        self.max_job_duration_hours = _env_float("MAX_JOB_DURATION_HOURS", 6)

        # A dead/near-dead torrent can otherwise sit occupying a concurrency
        # slot for the full MAX_JOB_DURATION_HOURS before the stall timeout
        # gives up on it. Checking average speed lets a job move on to the
        # next candidate release much sooner. 0 disables the check entirely.
        self.min_download_speed_kbps = _env_float("MIN_DOWNLOAD_SPEED_KBPS", 500)
        # Periodic safety net, independent of the upgrade/downgrade
        # workflows: catches any movie Radarr thinks has a file that doesn't
        # actually exist on disk (e.g. after Radarr's own background cleanup
        # removed an original), or anything genuinely missing, and searches for a
        # replacement. Runs regardless of ENABLE_UPGRADE_WORKFLOW/
        # ENABLE_DOWNGRADE_WORKFLOW since it never renames or deletes
        # anything — it only ever searches.
        self.library_integrity_check_interval_hours = _env_float("LIBRARY_INTEGRITY_CHECK_INTERVAL_HOURS", 6)
        # If one integrity pass finds more missing files than this, it assumes
        # the library mount isn't up (e.g. right after a reboot) rather than
        # mass data loss, and skips acting for that pass.
        self.integrity_sanity_limit = _env_int("INTEGRITY_SANITY_LIMIT", 8)
        # Wait this long after startup before the first integrity pass, so
        # network mounts and dependent services have time to come up.
        self.integrity_startup_delay_seconds = _env_int("INTEGRITY_STARTUP_DELAY_SECONDS", 180)

        # A "smallest file that clears a 1-seeder minimum" pick is exactly
        # how you end up grabbing near-dead torrents — confirmed live
        # repeatedly in practice. Raising this floor trades a little size for
        # actually finishing in a reasonable time.
        self.min_release_seeders = _env_int("MIN_RELEASE_SEEDERS", 10)
        # Grace period before speed is judged at all — a fresh torrent often
        # starts at zero while finding peers, and killing it instantly would
        # be worse than just waiting a normal amount for the stall timeout.
        self.min_speed_check_after_minutes = _env_int("MIN_SPEED_CHECK_AFTER_MINUTES", 15)
        self.downgrade_target_resolutions = ["1080", "720"]

        # Movies the user has explicitly opted out of ever downgrading.
        self.downgrade_excluded_movie_ids: list[int] = []

        # How long a preserved original (the file kept-in-place before a
        # grab, so Radarr's own import cleanup never touches it) sticks
        # around before Reclaimarr auto-deletes it. A manual "delete now" in
        # the UI can always override this early.
        self.keep_original_days = _env_int("KEEP_ORIGINAL_DAYS", 7)
        self.preserve_cleanup_interval_seconds = _env_int("PRESERVE_CLEANUP_INTERVAL_SECONDS", 3600)

        # Blank by default — an unconfigured instance has no admin login yet
        # until the setup wizard creates one (stored hashed in auth_store's
        # overrides file, not here). A non-blank AUTH_PASSWORD env var still
        # works as before, for existing deployments that set it directly.
        self.auth_username = os.environ.get("AUTH_USERNAME", "admin")
        self.auth_password = os.environ.get("AUTH_PASSWORD", "")

        self.dry_run = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

        # Optional — reuses the same Discord webhook already configured in
        # Tautulli, if the user wants Reclaimarr's own events posted there too.
        # Empty means notifications are silently skipped.
        self.discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        # Hour (UTC) the once-a-day Discord summary posts at. Independent of
        # DOWNGRADE_SCAN_HOUR_UTC — the digest still runs (reporting a quiet
        # day) even with the downgrade workflow disabled.
        self.digest_hour_utc = _env_int("DIGEST_HOUR_UTC", 13)
        # Display name the Discord webhook posts under.
        self.discord_username = os.environ.get("DISCORD_USERNAME", "Reclaimarr").strip() or "Reclaimarr"
        # Post a short "Reclaimarr Online" notice each time the app starts.
        self.discord_startup_notice = _env_bool("DISCORD_STARTUP_NOTICE", False)

        # Independent of DRY_RUN: when true, terminate_session is always a
        # no-op/log-only, even with dry_run off. Lets you test a real Radarr
        # search+grab against a live library while a movie is actively
        # playing, without any risk of that playback getting interrupted.
        self.never_terminate_session = os.environ.get("NEVER_TERMINATE_SESSION", "false").strip().lower() in ("1", "true", "yes")

        # Comma-separated Plex usernames allowed to trigger Workflow 1. Empty
        # means everyone can trigger it. Sessions from any other user are left
        # completely alone — useful both for testing (set to just your own
        # username) and production (exclude other households/guests sharing
        # the server from triggering paid-for downloads).
        self.plex_allowed_usernames = [
            u.strip() for u in os.environ.get("PLEX_ALLOWED_USERNAMES", "").split(",") if u.strip()
        ]

        # Independent on/off switches per workflow, e.g. to test the upgrade
        # path alone without the downgrade scan touching the whole library.
        self.enable_upgrade_workflow = os.environ.get("ENABLE_UPGRADE_WORKFLOW", "true").strip().lower() in ("1", "true", "yes")
        self.enable_downgrade_workflow = os.environ.get("ENABLE_DOWNGRADE_WORKFLOW", "true").strip().lower() in ("1", "true", "yes")

        self.app_port = _env_int("APP_PORT", 8585)

        self.data_dir = os.environ.get("DATA_DIR", "/data")
        self.jobs_file = os.path.join(self.data_dir, "jobs.json")
        self.log_file = os.path.join(self.data_dir, "log.txt")


settings = Settings()
