import json
import os

from .config import settings

# Plain fields: safe to show as-is (addresses, not credentials).
PLAIN_KEYS = ["plex_url", "radarr_url", "tautulli_url", "qbit_url", "qbit_username"]
# Secret fields: never returned in full — masked on read, only overwritten
# when the caller actually sends a new (non-empty) value. discord_webhook_url
# is a bearer-style URL (anyone
# with it can post to the channel), so it's treated as a secret too.
SECRET_KEYS = ["plex_token", "radarr_api_key", "tautulli_api_key", "discord_webhook_url", "qbit_password"]

ALL_KEYS = PLAIN_KEYS + SECRET_KEYS

_overrides_path = os.path.join(settings.data_dir, "connections_overrides.json")


def load_overrides() -> None:
    if not os.path.exists(_overrides_path):
        return
    with open(_overrides_path, "r", encoding="utf-8") as f:
        overrides = json.load(f)
    for key, value in overrides.items():
        if key in ALL_KEYS:
            setattr(settings, key, value)


def _persist_all() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    payload = {key: getattr(settings, key) for key in ALL_KEYS}
    tmp_path = _overrides_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _overrides_path)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•••••" + value[-4:]


def current_display() -> dict:
    """Never returns a secret in full — masked fields only ever show a
    trailing-4-character hint, matching the display convention used by
    every major service's own "your key is ****1234" UI.
    """
    result = {}
    for key in PLAIN_KEYS:
        result[key] = {"value": getattr(settings, key, ""), "is_secret": False}
    for key in SECRET_KEYS:
        value = getattr(settings, key, "")
        result[key] = {"value": _mask(value), "is_secret": True, "is_set": bool(value)}
    return result


def save_overrides(update: dict) -> dict:
    """Empty string for a secret field means "leave it alone" — the frontend
    never has the real value to send back, so an empty submission must never
    be interpreted as "clear this credential."
    """
    for key, value in update.items():
        if key not in ALL_KEYS:
            continue
        if key in SECRET_KEYS and not value:
            continue
        setattr(settings, key, value)
    _persist_all()
    return current_display()
