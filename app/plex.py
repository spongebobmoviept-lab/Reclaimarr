import re
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import httpx

from .config import settings
from .logger import with_retry

_JSON_HEADERS = {"Accept": "application/json"}


@dataclass
class PlexSession:
    session_key: str
    machine_identifier: str
    title: str
    media_type: str
    rating_key: str
    guid: str
    view_offset_ms: int
    duration_ms: int
    resolution: str
    username: str
    file_path: str
    file_size: int

    @property
    def watched_fraction(self) -> float:
        if not self.duration_ms:
            return 0.0
        return self.view_offset_ms / self.duration_ms


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.plex_url, timeout=15, headers=_JSON_HEADERS)


async def test_connection(url: str, token: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard — uses whatever the
    user just typed in rather than the saved settings, since this runs
    before anything is saved.
    """
    async with httpx.AsyncClient(base_url=url.rstrip("/"), timeout=10, headers=_JSON_HEADERS) as client:
        resp = await client.get("/library/sections", params={"X-Plex-Token": token})
        resp.raise_for_status()
        data = resp.json()
    sections = data.get("MediaContainer", {}).get("Directory", [])
    movie_libraries = [s.get("title") for s in sections if s.get("type") == "movie"]
    return {"library_count": len(sections), "movie_libraries": movie_libraries}


@with_retry(label="Plex: list sessions")
async def list_sessions() -> list[PlexSession]:
    async with _client() as client:
        resp = await client.get("/status/sessions", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()

    sessions = []
    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    for item in metadata:
        media = (item.get("Media") or [{}])[0]
        part = (media.get("Part") or [{}])[0]
        player = item.get("Player") or {}
        user = item.get("User") or {}

        sessions.append(
            PlexSession(
                session_key=str(item.get("sessionKey", "")),
                machine_identifier=player.get("machineIdentifier", ""),
                title=item.get("title", ""),
                media_type=item.get("type", ""),
                rating_key=str(item.get("ratingKey", "")),
                guid=item.get("guid", ""),
                view_offset_ms=int(item.get("viewOffset", 0)),
                duration_ms=int(item.get("duration", 0)),
                resolution=str(media.get("videoResolution", "")),
                username=user.get("title", ""),
                file_path=part.get("file", ""),
                file_size=int(part.get("size", 0)),
            )
        )
    return sessions


@with_retry(label="Plex: terminate session")
async def terminate_session(machine_identifier: str, reason: str) -> None:
    if settings.dry_run or settings.never_terminate_session:
        why = "DRY RUN" if settings.dry_run else "NEVER_TERMINATE_SESSION"
        await log(f"[{why}] would terminate Plex session {machine_identifier} with message: {reason!r}")
        return
    async with _client() as client:
        resp = await client.delete(
            "/status/sessions/terminate",
            params={
                "sessionId": machine_identifier,
                "reason": reason,
                "X-Plex-Token": settings.plex_token,
            },
        )
        resp.raise_for_status()


@with_retry(label="Plex: get tmdb id")
async def get_tmdb_id(rating_key: str) -> Optional[int]:
    """Look up a movie's TMDB id via full metadata.

    /status/sessions only returns Plex's internal guid (plex://movie/...) with
    no external ids attached. The Guid list with imdb://, tmdb://, tvdb:// only
    shows up on the full /library/metadata/<ratingKey> response, so matching a
    live session to a Radarr movie requires this extra lookup.
    """
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()

    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return None
    for guid_entry in metadata[0].get("Guid", []) or []:
        match = re.search(r"tmdb://(\d+)", guid_entry.get("id", ""))
        if match:
            return int(match.group(1))
    return None


@with_retry(label="Plex: refresh item")
async def refresh_item(rating_key: str) -> None:
    """Ask Plex to rescan this specific item's files for changes.

    Fallback for when Radarr's own Plex "Connect" notification isn't set up
    (or misfires) — without it, Plex only notices a newly imported file on its
    own scan schedule, which could be a long wait. This targets just the one
    item instead of a full library scan.
    """
    async with _client() as client:
        resp = await client.put(
            f"/library/metadata/{rating_key}/refresh",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()


@with_retry(attempts=1, label="Plex: refresh item (post-swap)")
async def refresh_item_fast(rating_key: str) -> None:
    """Same call as refresh_item, but a single attempt with no backoff.

    Right after a downgrade/upgrade file swap, the item's rating key is
    known to be about to 404 (Plex hasn't rescanned yet) — confirmed live
    across an entire batch run, every single processed movie hit this on
    the very first attempt, every time. The normal 5s/15s retry-then-give-up
    dance before falling back to re-resolve-by-title was pure wasted time
    (~20s/movie); the caller already re-resolves on any failure here, so
    there's nothing to gain from retrying the same about-to-be-stale key.
    """
    async with _client() as client:
        resp = await client.put(
            f"/library/metadata/{rating_key}/refresh",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()


@with_retry(label="Plex: get added date")
async def get_added_at(rating_key: str) -> Optional[int]:
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()
    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return None
    added_at = metadata[0].get("addedAt")
    return int(added_at) if added_at else None


@with_retry(label="Plex: restore added date")
async def restore_added_at(rating_key: str, added_at: int) -> None:
    """Lock the item's "added" date back to its original value.

    Swapping a movie's file (up/downgrade) makes Plex treat the rescan as
    fresh content and bump it to the top of Recently Added — confirmed live
    across the whole downgrade batch, every processed movie reappeared
    there despite having been in the library for months. Plex's edit-field
    API lets a field be explicitly set-and-locked so a later scan can't
    re-derive/re-bump it.
    """
    async with _client() as client:
        resp = await client.put(
            f"/library/metadata/{rating_key}",
            params={
                "type": 1,
                "addedAt.value": added_at,
                "addedAt.locked": 1,
                "X-Plex-Token": settings.plex_token,
            },
        )
        resp.raise_for_status()


@with_retry(label="Plex: refresh movies section")
async def refresh_movies_section() -> None:
    """Ask Plex to rescan the whole movie library section for changes.

    Deliberately NOT scoped to one item's rating key — confirmed live
    (2012, repeatedly) that a rating key can go stale or even get
    reassigned entirely after a file swap, and refreshing by a now-wrong
    key does nothing useful. A section-wide refresh has no such dependency:
    Plex re-scans every folder, and matches primarily by folder/filename
    against its existing metadata (same folder = same movie), which is
    exactly the signal needed for it to recognize a swapped file as another
    version of the SAME item rather than a disconnected new one. Heavier
    than a single-item refresh, but Plex's own rescan is incremental
    (only re-reads what actually changed), so this is cheap in practice.
    """
    async with _client() as client:
        resp = await client.get("/library/sections", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        sections = resp.json().get("MediaContainer", {}).get("Directory", [])
    movie_sections = [s.get("key") for s in sections if s.get("type") == "movie"]
    if not movie_sections:
        return
    async with _client() as client:
        for section_key in movie_sections:
            resp = await client.get(f"/library/sections/{section_key}/refresh", params={"X-Plex-Token": settings.plex_token})
            resp.raise_for_status()


@with_retry(label="Plex: has 4K version")
async def has_4k_version(rating_key: str) -> bool:
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()

    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return False
    for media in metadata[0].get("Media", []):
        if str(media.get("videoResolution")) == "4k" or str(media.get("videoResolution")) == "2160":
            return True
    return False


@with_retry(label="Plex: confirm media playable")
async def confirm_media_playable(rating_key: str, max_size_bytes: Optional[int] = None) -> bool:
    """Resolution-agnostic version of has_4k_version, for the downgrade path.

    Confirms Plex has successfully picked up *a* valid, playable file for this
    item after a swap — duration > 0 means Plex actually parsed real media,
    not a zero-byte or still-importing placeholder. If max_size_bytes is
    given, also requires Plex's reported file size to be at or under it, as
    extra confirmation this is really the smaller replacement and not the
    original still lingering under the tracked path.
    """
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()

    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return False
    for media in metadata[0].get("Media", []):
        if int(media.get("duration", 0)) <= 0:
            continue
        if max_size_bytes is None:
            return True
        part_size = int((media.get("Part") or [{}])[0].get("size", 0))
        if part_size and part_size <= max_size_bytes:
            return True
    return False


@with_retry(label="Plex: days since last viewed")
async def days_since_last_viewed(rating_key: str) -> Optional[float]:
    """Plex tracks watch state itself (viewCount, lastViewedAt) — no need to
    go through Tautulli for this. Directly from Plex's own metadata.

    Returns None if it's never been watched at all, otherwise how many days
    ago the most recent view was. A movie being actively rewatched keeps
    getting a fresh (small) value here, which is exactly what lets it
    naturally keep deferring downgrade eligibility without any separate
    "popular movie" tracking — recency alone does the job.
    """
    async with _client() as client:
        resp = await client.get(
            f"/library/metadata/{rating_key}",
            params={"X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()

    metadata = data.get("MediaContainer", {}).get("Metadata", [])
    if not metadata:
        return None
    last_viewed_at = metadata[0].get("lastViewedAt")
    if not last_viewed_at:
        return None
    return (time.time() - int(last_viewed_at)) / 86400


async def sessions_for_rating_key(rating_key: str) -> list[PlexSession]:
    sessions = await list_sessions()
    return [s for s in sessions if s.rating_key == rating_key]


@with_retry(label="Plex: find rating key by tmdb id")
async def find_rating_key_by_title(title: str, tmdb_id: int) -> Optional[str]:
    """Resolve a Radarr movie to its Plex ratingKey when it's not currently playing.

    Used for admin-triggered upgrades on a movie nobody's actively watching
    (e.g. testing, or a manual "upgrade this now" action) — the normal
    session-driven path already has the ratingKey for free from Plex's own
    session data, this is only needed when there's no session to read it from.
    """
    async with _client() as client:
        resp = await client.get(
            "/search",
            params={"query": title, "X-Plex-Token": settings.plex_token},
        )
        resp.raise_for_status()
        data = resp.json()

    for item in data.get("MediaContainer", {}).get("Metadata", []) or []:
        if item.get("type") != "movie":
            continue
        rating_key = str(item.get("ratingKey", ""))
        if not rating_key:
            continue
        if await get_tmdb_id(rating_key) == tmdb_id:
            return rating_key
    return None


@with_retry(label="Plex: list accounts", attempts=1)
async def list_known_users() -> list[str]:
    """Every account Plex itself knows has access to this server.

    This is the authoritative source for the upgrade allow-list picker —
    unlike Tautulli's user list, it doesn't depend on Tautulli being
    configured or having already logged a play from that person.
    """
    async with _client() as client:
        resp = await client.get("/accounts", params={"X-Plex-Token": settings.plex_token})
        resp.raise_for_status()
        data = resp.json()
    accounts = data.get("MediaContainer", {}).get("Account", [])
    return sorted({a.get("name") for a in accounts if a.get("name")})


async def is_movie_being_watched(rating_key: str) -> bool:
    return len(await sessions_for_rating_key(rating_key)) > 0


async def is_movie_being_watched_by_title(title: str) -> bool:
    """Fallback matcher for callers (Workflow 2) that only have a Radarr title, not a Plex ratingKey."""
    sessions = await list_sessions()
    normalized = title.strip().lower()
    return any(s.media_type == "movie" and s.title.strip().lower() == normalized for s in sessions)
