import httpx

from .config import settings
from .logger import with_retry


async def test_connection(url: str, api_key: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard (Tautulli is optional,
    so this is only called if the user actually filled the fields in)."""
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10) as client:
        resp = await client.get("/api/v2", params={"apikey": api_key, "cmd": "status"})
        resp.raise_for_status()
        data = resp.json()
    if data.get("response", {}).get("result") != "success":
        raise ValueError(data.get("response", {}).get("message") or "Tautulli reported an error")
    return {"ok": True}


@with_retry(label="Tautulli: list users", attempts=1)
async def list_known_users() -> list[str]:
    """Every Plex username Tautulli has ever seen watch something on this server.

    Used to populate the "who can trigger 4K upgrades" picker in the UI so the
    user selects from real accounts instead of typing usernames by hand.
    Returns an empty list (rather than raising) if Tautulli isn't configured,
    since this is a UI convenience, not something either workflow depends on.
    """
    if not settings.tautulli_url or not settings.tautulli_api_key:
        return []

    async with httpx.AsyncClient(base_url=settings.tautulli_url, timeout=10) as client:
        resp = await client.get(
            "/api/v2",
            params={"apikey": settings.tautulli_api_key, "cmd": "get_users"},
        )
        resp.raise_for_status()
        data = resp.json()

    users = data.get("response", {}).get("data", [])
    names = sorted(
        {u.get("username") or u.get("friendly_name") for u in users if u.get("username") or u.get("friendly_name")}
    )
    return names


@with_retry(label="Tautulli: check watch history", attempts=1)
async def has_been_watched(title: str, year: int | None = None, threshold: float = 0.75) -> bool:
    """Whether Tautulli has any play of this movie past the "watched" threshold.

    Matches by title (Tautulli's own search) and year (Radarr always has a
    year; guards against two different movies sharing a title). watched_status
    is Tautulli's own quartile of its configured watched-percent setting — 1.0
    is "fully watched", 0.75 is close enough to count. Returns True (fail
    open) if Tautulli isn't configured, since gating an entire workflow on an
    optional integration being present isn't the intent — see workflows.py's
    caller for why this matters.
    """
    if not settings.tautulli_url or not settings.tautulli_api_key:
        return True

    async with httpx.AsyncClient(base_url=settings.tautulli_url, timeout=10) as client:
        resp = await client.get(
            "/api/v2",
            params={
                "apikey": settings.tautulli_api_key,
                "cmd": "get_history",
                "media_type": "movie",
                "search": title,
                "length": 50,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    records = data.get("response", {}).get("data", {}).get("data", [])
    for record in records:
        if year is not None and record.get("year") not in (year, None):
            continue
        if record.get("title", "").strip().lower() != title.strip().lower():
            continue
        if float(record.get("watched_status", 0) or 0) >= threshold:
            return True
    return False
