import hashlib
import hmac
import json
import os
import secrets as secrets_mod

from .config import settings

_overrides_path = os.path.join(settings.data_dir, "auth_overrides.json")
_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS).hex()


def _load() -> dict | None:
    if not os.path.exists(_overrides_path):
        return None
    with open(_overrides_path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_admin_configured() -> bool:
    """True once either a wizard-created login exists, or the classic
    AUTH_PASSWORD env var is set (an existing deployment upgraded to this
    version) — either is a valid, already-secured instance."""
    return _load() is not None or bool(settings.auth_password)


def set_admin(username: str, password: str) -> None:
    salt = secrets_mod.token_bytes(16)
    payload = {"username": username, "salt": salt.hex(), "hash": _hash_password(password, salt)}
    os.makedirs(settings.data_dir, exist_ok=True)
    tmp_path = _overrides_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, _overrides_path)


def verify_admin(username: str, password: str) -> bool:
    stored = _load()
    if stored is not None:
        salt = bytes.fromhex(stored["salt"])
        candidate = _hash_password(password, salt)
        return hmac.compare_digest(candidate, stored["hash"]) and hmac.compare_digest(username, stored["username"])
    # No wizard-created login — fall back to the classic env-var credentials,
    # for instances that set AUTH_PASSWORD directly instead of using setup.
    if not settings.auth_password:
        return False
    return hmac.compare_digest(username, settings.auth_username) and hmac.compare_digest(password, settings.auth_password)


def is_setup_complete() -> bool:
    """Gates whether "/" serves the real app or redirects to the wizard.

    Tautulli/Discord and the 4K/1080p profile picks are nice-to-haves,
    editable later from the normal Settings page — only an admin login plus
    Plex and Radarr being reachable are required to consider setup done.
    """
    if not is_admin_configured():
        return False
    if not (settings.plex_url and settings.plex_token):
        return False
    if not (settings.radarr_url and settings.radarr_api_key):
        return False
    return True
