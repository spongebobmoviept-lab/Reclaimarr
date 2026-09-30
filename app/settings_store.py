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
    "upgrade_no_release_cooldown_hours",
    "digest_hour_utc",
    "discord_username",
    "discord_startup_notice",
    "integrity_sanity_limit",
    "media_roots",
]

_STR_KEYS = {"discord_username"}
_LIST_KEYS = {"media_roots"}
_BOOL_KEYS = {"never_terminate_session", "discord_startup_notice"}


def validate(update: dict) -> dict:
    """Checks and normalises a settings update. Raises ValueError with a
    plain-English message; returns the cleaned update."""
    from .config import _normalize_media_roots

    clean = {}
    for key, value in update.items():
        if key in _LIST_KEYS:
            if isinstance(value, str):
                value = [v for v in value.split(",")]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("Media folders must be a list of container paths like /media2")
            roots = _normalize_media_roots(",".join(value))
            if not roots:
                raise ValueError("Add at least one media folder: an absolute container path like /media2 (not / itself)")
            clean[key] = roots
        elif key in _STR_KEYS:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} can't be empty")
            clean[key] = value.strip()[:80]
        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be on or off")
            clean[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} must be a number of 0 or more")
            if key.endswith("_hour_utc") and value > 23:
                raise ValueError(f"{key} must be an hour between 0 and 23")
            clean[key] = value
    return clean

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
