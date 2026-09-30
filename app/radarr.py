from dataclasses import dataclass
from typing import Any, Optional

import httpx

from .config import settings
from .logger import log, with_retry


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.radarr_url}/api/v3",
        timeout=20,
        headers={"X-Api-Key": settings.radarr_api_key},
    )


async def test_connection(url: str, api_key: str) -> dict:
    """Ad-hoc connectivity check for the setup wizard, using credentials the
    caller just typed in rather than whatever's already saved in settings —
    this runs BEFORE anything is saved, so it can't reuse _client().
    Also returns the quality profile list so the wizard's 4K/1080p pickers
    can be populated from the same round-trip.
    """
    async with httpx.AsyncClient(base_url=f"{url.rstrip('/')}/api/v3", timeout=10, headers={"X-Api-Key": api_key}) as client:
        status_resp = await client.get("/system/status")
        status_resp.raise_for_status()
        version = status_resp.json().get("version")

        profiles_resp = await client.get("/qualityprofile")
        profiles_resp.raise_for_status()
        profiles = [{"id": p["id"], "name": p["name"]} for p in profiles_resp.json()]

    return {"version": version, "quality_profiles": profiles}


async def root_folders() -> list[str]:
    """Radarr's movie root folder paths, as Radarr (and so this container,
    if the volumes match) sees them."""
    async with _client() as client:
        resp = await client.get("/rootfolder")
        resp.raise_for_status()
        return [f["path"] for f in resp.json() if f.get("path")]


@dataclass
class RadarrMovie:
    id: int
    title: str
    tmdb_id: int
    quality_profile_id: int
    has_file: bool
    file_size: int
    poster_url: Optional[str]
    raw: dict[str, Any]


def _to_movie(raw: dict[str, Any]) -> RadarrMovie:
    poster_url = None
    for image in raw.get("images", []):
        if image.get("coverType") == "poster":
            poster_url = image.get("remoteUrl") or image.get("url")
            break
    movie_file = raw.get("movieFile") or {}
    return RadarrMovie(
        id=raw["id"],
        title=raw.get("title", ""),
        tmdb_id=raw.get("tmdbId", 0),
        quality_profile_id=raw.get("qualityProfileId", 0),
        has_file=bool(raw.get("hasFile")),
        file_size=int(movie_file.get("size", 0)),
        poster_url=poster_url,
        raw=raw,
    )


@with_retry(label="Radarr: list movies")
async def list_movies() -> list[RadarrMovie]:
    async with _client() as client:
        resp = await client.get("/movie")
        resp.raise_for_status()
        return [_to_movie(m) for m in resp.json()]


@with_retry(label="Radarr: get movie")
async def get_movie(movie_id: int) -> RadarrMovie:
    async with _client() as client:
        resp = await client.get(f"/movie/{movie_id}")
        resp.raise_for_status()
        return _to_movie(resp.json())


async def find_movie_by_tmdb_id(tmdb_id: int) -> Optional[RadarrMovie]:
    for movie in await list_movies():
        if movie.tmdb_id == tmdb_id:
            return movie
    return None


@with_retry(label="Radarr: update quality profile")
async def set_quality_profile(movie_id: int, quality_profile_id: int) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would set movie {movie_id} qualityProfileId -> {quality_profile_id}")
        return
    async with _client() as client:
        resp = await client.get(f"/movie/{movie_id}")
        resp.raise_for_status()
        movie_json = resp.json()
        movie_json["qualityProfileId"] = quality_profile_id
        put_resp = await client.put(f"/movie/{movie_id}", json=movie_json)
        put_resp.raise_for_status()


@with_retry(label="Radarr: trigger import scan")
async def trigger_import_scan() -> None:
    """Force Radarr to process finished downloads right now instead of waiting
    on its own background interval (RefreshMonitoredDownloads/ProcessMonitoredDownloads,
    the same pair Radarr itself runs periodically — this just doesn't wait for it).
    """
    if settings.dry_run:
        await log("[DRY RUN] would trigger RefreshMonitoredDownloads")
        return
    async with _client() as client:
        resp = await client.post("/command", json={"name": "RefreshMonitoredDownloads"})
        resp.raise_for_status()


@with_retry(label="Radarr: force manual import")
async def force_manual_import(movie_id: int, download_id: str) -> bool:
    """Bypass Radarr's import-time "not an upgrade for existing movie file"
    block — confirmed live this is a SEPARATE guard from the search-time
    "rejected" flag. A deliberate downgrade grabs fine (explicit grab already
    bypasses that one) but then sits in the queue forever with
    trackedDownloadStatus "warning", because Radarr's automatic importer
    refuses to replace a file with a lower-quality one, full stop, regardless
    of profile. This does what clicking "Import" in the Manual Import screen
    does: import it anyway. Returns False if there was nothing importable
    (e.g. already imported, or the download vanished).
    """
    if settings.dry_run:
        await log(f"[DRY RUN] would force manual import for movie {movie_id} (downloadId={download_id})")
        return True

    async with _client() as client:
        resp = await client.get("/manualimport", params={"downloadId": download_id})
        resp.raise_for_status()
        candidates = resp.json()

    match = next((c for c in candidates if c.get("movie", {}).get("id") == movie_id), None)
    if match is None:
        return False

    async with _client() as client:
        resp = await client.post(
            "/command",
            json={
                "name": "ManualImport",
                "importMode": "move",
                "files": [
                    {
                        "path": match["path"],
                        "movieId": movie_id,
                        "quality": match["quality"],
                        "languages": match["languages"],
                        "releaseGroup": match.get("releaseGroup", ""),
                        "indexerFlags": match.get("indexerFlags", 0),
                        "downloadId": download_id,
                    }
                ],
            },
        )
        resp.raise_for_status()
    return True


@with_retry(label="Radarr: rescan movie")
async def rescan_movie(movie_id: int) -> None:
    """Ask Radarr to re-scan this movie's folder and re-adopt whatever file
    is sitting at its expected path as the tracked movieFile.

    Deliberately NOT used during normal preserve-then-grab flow — confirmed
    live that doing so there makes Radarr re-adopt the renamed (kept-suffix)
    original, defeating the whole point of renaming it out of the way. This
    is only safe here, for the no-gain revert path, because by the time it's
    called the original has already been renamed BACK to its normal path —
    re-adoption is exactly what should happen.
    """
    if settings.dry_run:
        await log(f"[DRY RUN] would trigger RescanMovie for movie {movie_id}")
        return
    async with _client() as client:
        resp = await client.post("/command", json={"name": "RescanMovie", "movieIds": [movie_id]})
        resp.raise_for_status()


@with_retry(label="Radarr: trigger search")
async def trigger_search(movie_id: int) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would trigger MoviesSearch for movie {movie_id}")
        return
    async with _client() as client:
        resp = await client.post("/command", json={"name": "MoviesSearch", "movieIds": [movie_id]})
        resp.raise_for_status()


@dataclass
class RadarrRelease:
    guid: str
    indexer_id: int
    title: str
    size: int
    resolution: str
    seeders: int
    rejected: bool


@with_retry(label="Radarr: get releases")
async def get_releases(movie_id: int) -> list[RadarrRelease]:
    async with _client() as client:
        resp = await client.get("/release", params={"movieId": movie_id})
        resp.raise_for_status()
        releases = []
        for r in resp.json():
            quality = (r.get("quality") or {}).get("quality") or {}
            releases.append(
                RadarrRelease(
                    guid=r.get("guid", ""),
                    indexer_id=r.get("indexerId", 0),
                    title=r.get("title", ""),
                    size=int(r.get("size", 0)),
                    resolution=str(quality.get("resolution", "")),
                    seeders=int((r.get("seeders") or 0)),
                    rejected=bool(r.get("rejected")),
                )
            )
        return releases


@with_retry(label="Radarr: grab release")
async def grab_release(guid: str, indexer_id: int) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would grab release guid={guid} indexerId={indexer_id}")
        return
    async with _client() as client:
        resp = await client.post("/release", json={"guid": guid, "indexerId": indexer_id})
        resp.raise_for_status()


@with_retry(label="Radarr: get queue")
async def get_queue() -> list[dict[str, Any]]:
    async with _client() as client:
        resp = await client.get("/queue")
        resp.raise_for_status()
        return resp.json().get("records", [])


async def queue_has_movie(movie_id: int) -> bool:
    return any(record.get("movieId") == movie_id for record in await get_queue())


async def queue_records_for_movie(movie_id: int) -> list[dict[str, Any]]:
    return [r for r in await get_queue() if r.get("movieId") == movie_id]


@with_retry(label="Radarr: remove queue item")
async def remove_queue_item(queue_id: int, remove_from_client: bool = True) -> None:
    if settings.dry_run:
        await log(f"[DRY RUN] would remove queue item {queue_id} (removeFromClient={remove_from_client})")
        return
    async with _client() as client:
        resp = await client.delete(
            f"/queue/{queue_id}",
            params={"removeFromClient": str(remove_from_client).lower(), "blocklist": "false"},
        )
        resp.raise_for_status()


@with_retry(label="Radarr: get movie history")
async def get_movie_history(movie_id: int) -> list[dict[str, Any]]:
    async with _client() as client:
        resp = await client.get("/history/movie", params={"movieId": movie_id})
        resp.raise_for_status()
        return resp.json()


async def latest_history_id(movie_id: int) -> int:
    """Highest history event id for a movie right now, used as a baseline.

    A movie that's ever been imported before (i.e. almost every movie in the
    library) already has a "downloadFolderImported" event sitting in its
    history. Checking for that event type alone — with no baseline — would
    make a brand-new job think it finished instantly. Every job records this
    baseline at claim time so only *new* events (id greater than it) count.
    """
    history = await get_movie_history(movie_id)
    return max((h.get("id", 0) for h in history), default=0)


async def new_history_event_type(movie_id: int, since_id: int) -> Optional[str]:
    """Most recent event type with id > since_id, or None if nothing new happened yet."""
    history = await get_movie_history(movie_id)
    new_events = [h for h in history if h.get("id", 0) > since_id]
    if not new_events:
        return None
    return max(new_events, key=lambda h: h.get("id", 0)).get("eventType")
