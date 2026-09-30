"""Optional direct qBittorrent control for active upgrade jobs.

When QBIT_URL is set, Reclaimarr force-starts + top-priorities an upgrade's
download the moment its torrent hash is known, so a 4K grab for something
someone is watching right now doesn't sit in the normal queue behind the
regular backlog. The force flag is released again as soon as the job ends,
so the torrent falls back under your normal seeding ratio/time rules.

If you run another tool that also manages qBittorrent's queue (anything
that calls setForceStart/topPrio on its own), point both at the same
QBIT_ACTIVE_HASHES_FILE: Reclaimarr lists the hashes it currently owns
there so the other tool can leave them alone. See jobs.py.

Every call here is best-effort: a qBittorrent hiccup never breaks the
upgrade job itself -- this is a speed boost, not a requirement.
"""

import re

import httpx

from .config import settings
from .logger import log

_HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _valid_hash(info_hash: str) -> bool:
    return bool(_HASH_RE.match((info_hash or "").lower()))


async def _post(client: httpx.AsyncClient, path: str, data: dict) -> None:
    resp = await client.post(path, data=data)
    resp.raise_for_status()


async def _session() -> httpx.AsyncClient:
    """Log in and return a client carrying the SID cookie. Caller closes it."""
    client = httpx.AsyncClient(
        base_url=settings.qbit_url,
        timeout=15,
        # qBittorrent's CSRF protection rejects requests whose Referer/Origin
        # doesn't match its own host -- send its own URL.
        headers={"Referer": settings.qbit_url},
    )
    try:
        resp = await client.post(
            "/api/v2/auth/login",
            data={"username": settings.qbit_username, "password": settings.qbit_password},
        )
        resp.raise_for_status()
        if resp.text.strip() != "Ok." and "SID" not in client.cookies:
            raise RuntimeError("qBittorrent login was rejected (check QBIT_USERNAME/QBIT_PASSWORD)")
    except Exception:
        await client.aclose()
        raise
    return client


async def test_connection(url: str, username: str, password: str) -> dict:
    """Setup-wizard check: logs in with the given credentials and reads the
    app version. Raises on failure."""
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10, headers={"Referer": url.rstrip("/")}) as client:
        resp = await client.post("/api/v2/auth/login", data={"username": username, "password": password})
        resp.raise_for_status()
        if resp.text.strip() == "Fails.":
            raise PermissionError("qBittorrent rejected that username or password.")
        version = await client.get("/api/v2/app/version")
        version.raise_for_status()
    return {"version": version.text.strip()}


async def force_priority(info_hash: str) -> bool:
    """setForceStart(true) + topPrio on one torrent. Returns True on success,
    False (logged, non-fatal) on any failure or when qBittorrent isn't
    configured."""
    if not settings.qbit_url:
        return False
    info_hash = (info_hash or "").lower()
    if not _valid_hash(info_hash):
        await log(f"qbittorrent: ignoring malformed torrent hash {info_hash[:16]!r}")
        return False
    try:
        client = await _session()
        try:
            await _post(client, "/api/v2/torrents/setForceStart", {"hashes": info_hash, "value": "true"})
            # topPrio only works with queueing enabled; a 409 there is harmless.
            try:
                await _post(client, "/api/v2/torrents/topPrio", {"hashes": info_hash})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409:
                    raise
        finally:
            await client.aclose()
        await log(f"qbittorrent: force-started + top-priority'd {info_hash[:8]}")
        return True
    except Exception as exc:  # noqa: BLE001
        await log(f"qbittorrent: couldn't force-priority {info_hash[:8]} (non-fatal): {exc}")
        return False


async def unforce(info_hash: str) -> None:
    """Release the force-start flag once a job ends -- never leave a torrent
    permanently force-started (and so exempt from normal seed ratio/time
    cleanup) after the reason for forcing it is gone."""
    if not settings.qbit_url:
        return
    info_hash = (info_hash or "").lower()
    if not _valid_hash(info_hash):
        return
    try:
        client = await _session()
        try:
            await _post(client, "/api/v2/torrents/setForceStart", {"hashes": info_hash, "value": "false"})
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        await log(f"qbittorrent: couldn't un-force {info_hash[:8]} (non-fatal): {exc}")
