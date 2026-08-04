import json
import os

from .config import settings

EDITABLE_KEYS = [
    "radarr_4k_profile_id",
    "radarr_1080p_profile_id",
    "downgrade_threshold_gb",
    "downgrade_size_ratio",
    "max_4k_release_size_gb",
    "poll_interval_seconds",
    "max_download_attempts",
    "downgrade_scan_hour_utc",
    "downgrade_min_age_days",
    "downgrade_recent_watch_days",
    "downgrade_recheck_days",
    "upgrade_retry_cooldown_minutes",
    "keep_original_days",
    "never_terminate_session",
    "min_download_speed_kbps",
    "min_speed_check_after_minutes",
    "library_integrity_check_interval_hours",
    "min_release_seeders",
]

_overrides_path = os.path.join(settings.data_dir, "settings_overrides.json")


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)
    if "plex_allowed_usernames" in overrides:
        settings.plex_allowed_usernames = overrides["plex_allowed_usernames"]
    if "downgrade_excluded_movie_ids" in overrides:
        settings.downgrade_excluded_movie_ids = overrides["downgrade_excluded_movie_ids"]


def save_allowed_usernames(usernames: list[str]) -> list[str]:
    settings.plex_allowed_usernames = usernames
    _persist_all()
    return usernames


def set_downgrade_excluded(movie_id: int, excluded: bool) -> list[int]:
    current = set(settings.downgrade_excluded_movie_ids)
    if excluded:
        current.add(movie_id)
    else:
        current.discard(movie_id)
    settings.downgrade_excluded_movie_ids = sorted(current)
    _persist_all()
    return settings.downgrade_excluded_movie_ids


def _persist_all() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {key: getattr(settings, key) for key in EDITABLE_KEYS}
    payload["plex_allowed_usernames"] = settings.plex_allowed_usernames
    payload["downgrade_excluded_movie_ids"] = settings.downgrade_excluded_movie_ids
    with open(_overrides_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def save_overrides(update: dict) -> dict:
    for key, value in update.items():
        if key in EDITABLE_KEYS:
            setattr(settings, key, value)
    _persist_all()
    return current_editable()


def current_editable() -> dict:
    return {key: getattr(settings, key) for key in EDITABLE_KEYS}
