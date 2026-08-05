import asyncio
import datetime
import os
import random
from typing import Optional

from . import discord, plex, preserve, radarr
from .config import settings
from .jobs import store
from .logger import log

# Plex's session-terminate "reason" field shows on the viewer's screen, and
# gets ugly/truncated if it runs long — kept each pun short on purpose. One
# is picked at random per upgrade so regulars don't see the same line twice
# in a row. {title} is filled in; the practical bit (restart, original safe)
# is appended separately so it stays consistent and dynamic (keep_original_days).
_UPGRADE_PUN_LINES = [
    "Hey! We upgraded {title} to 4K while you weren't looking.",
    "Good news: we just upgraded {title} to 4K.",
    "Surprise! {title} is now in glorious 4K.",
    "Heads up — {title} just got bumped up to 4K.",
    "We saw you enjoying {title}, so we upgraded it to 4K.",
    "{title} just leveled up: 4K unlocked.",
    "Ding! Your movie got an upgrade — {title} is now in 4K.",
    "{title} got the royal treatment: 4K, delivered.",
    "Plot twist: we upgraded {title} to 4K mid-watch.",
    "{title} is now 4K. You're welcome.",
]

def _job_is_stalled(job) -> bool:
    """A job (of either kind) running longer than MAX_JOB_DURATION_HOURS is
    treated as stuck — e.g. a dead torrent at 0 seeders that Radarr never
    marks as failed on its own. Without this, the monitor loop would wait
    forever and permanently hold a downgrade concurrency slot.
    """
    created = datetime.datetime.fromisoformat(job.created_at)
    age = datetime.datetime.now(datetime.timezone.utc) - created
    return age.total_seconds() >= settings.max_job_duration_hours * 3600


def _download_too_slow(queue_record: dict) -> bool:
    """A near-dead torrent would otherwise sit holding a concurrency slot for
    the full MAX_JOB_DURATION_HOURS before the stall timeout gives up on it.
    Radarr's queue doesn't expose a raw speed field, but size/sizeleft/added
    together give the average bytes/sec since the download started — good
    enough to catch "this is never finishing" much sooner than waiting out
    the full stall window. Disabled entirely when min_download_speed_kbps is 0.
    """
    if not settings.min_download_speed_kbps:
        return False
    added_str = queue_record.get("added")
    if not added_str:
        return False
    try:
        added = datetime.datetime.fromisoformat(added_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    elapsed_seconds = (datetime.datetime.now(datetime.timezone.utc) - added).total_seconds()
    if elapsed_seconds < settings.min_speed_check_after_minutes * 60:
        return False
    size = queue_record.get("size", 0) or 0
    sizeleft = queue_record.get("sizeleft", 0) or 0
    downloaded = size - sizeleft
    if downloaded <= 0:
        return True
    avg_speed_kbps = (downloaded / elapsed_seconds) / 1024
    return avg_speed_kbps < settings.min_download_speed_kbps


# Per-movie wake signal so the UI's "force check now" button can skip the
# rest of the upgrade-search wait instead of the monitor sleeping through the
# full UPGRADE_SEARCH_TIMEOUT_SECONDS before trying a manual release search.
_force_events: dict[int, asyncio.Event] = {}


def request_force_check(movie_id: int) -> bool:
    event = _force_events.get(movie_id)
    if event is None:
        return False
    event.set()
    return True


def resume_job_monitor(movie_id: int, kind: str) -> None:
    """Restart the appropriate background monitor for a job recovered on startup."""
    if kind == "upgrade":
        asyncio.create_task(_monitor_download(movie_id))
    elif kind == "downgrade":
        asyncio.create_task(_resume_downgrade_monitor(movie_id))


async def _resume_downgrade_monitor(movie_id: int) -> None:
    semaphore = _get_downgrade_semaphore()
    await semaphore.acquire()
    await _downgrade_job_monitor(movie_id, semaphore)


async def _pick_release(
    releases: list[radarr.RadarrRelease],
    resolution: str,
    tried_guids: list[str],
    max_size_bytes: float | None = None,
    exclude_terms: tuple[str, ...] = ("remux",),
    prefer_terms: tuple[str, ...] = (),
    sort_smallest_first: bool = False,
    ignore_rejected: bool = False,
    min_seeders: int = 0,
) -> radarr.RadarrRelease | None:
    """ignore_rejected exists because Radarr's "rejected" flag mixes two very
    different things: real quality/availability problems (dead torrent,
    banned format) and Radarr's own automatic-search opinion ("existing file
    already meets cutoff", which will ALWAYS be true for a deliberate
    downgrade — no profile setting changes that, since it just means "this
    movie already has something at least this good"). Confirmed live: this
    is exactly why downgrade releases that Radarr calls "rejected" still grab
    and download successfully when done explicitly (proven on Pawn Sacrifice)
    — Radarr's automatic search opinion doesn't apply to our own picked
    release. min_seeders replaces the real protection "rejected" used to
    provide (skip dead torrents) with an explicit check we control ourselves.
    """
    candidates = [
        r
        for r in releases
        if (ignore_rejected or not r.rejected)
        and r.seeders >= min_seeders
        and r.resolution == resolution
        and r.guid not in tried_guids
        and not any(term in r.title.lower() for term in exclude_terms)
        and (max_size_bytes is None or r.size <= max_size_bytes)
    ]
    if not candidates:
        return None

    def sort_key(r: radarr.RadarrRelease):
        preferred = 0 if any(term in r.title.lower() for term in prefer_terms) else 1
        size_key = r.size if sort_smallest_first else -r.seeders
        return (preferred, size_key)

    candidates.sort(key=sort_key)
    return candidates[0]


async def upgrade_poll_loop() -> None:
    """Workflow 1: watch Plex sessions, trigger a 4K upgrade when a non-4K movie starts playing."""
    while True:
        try:
            await _upgrade_scan_once()
        except Exception as exc:  # noqa: BLE001
            await log(f"upgrade loop: unexpected error: {exc}")
        await asyncio.sleep(settings.poll_interval_seconds)


async def _upgrade_scan_once() -> None:
    sessions = await plex.list_sessions()
    for session in sessions:
        try:
            await try_process_session_for_upgrade(session)
        except Exception as exc:  # noqa: BLE001
            # Confirmed live: one session with a bad/stale Plex rating key
            # (e.g. a mid-playback library rescan invalidating it) crashed
            # the whole scan before it reached any of the other sessions —
            # silently blocking every other household member's upgrade
            # check, not just the broken one. One bad session must never
            # take down the rest.
            await log(f"upgrade loop: failed to process session for '{session.title}' (user {session.username}): {exc} — skipping this session for now")


async def try_process_session_for_upgrade(session: plex.PlexSession) -> str:
    """Evaluate one Plex session for a possible 4K upgrade and claim/start the job if eligible.

    Shared by the automatic poll loop and the UI's manual "trigger now" button
    so both paths apply exactly the same eligibility rules — the button just
    skips waiting for the next poll tick, it doesn't bypass any safety check.
    Returns a short reason string for why nothing happened, or "started".
    """
    if session.media_type != "movie":
        return "not a movie"
    if session.resolution.lower() in ("2160", "4k"):
        # Confirmed live (Cars): Plex doesn't consistently lowercase this —
        # an exact-match check here let an already-4K session get re-claimed
        # for another pointless upgrade the moment resolution came back as
        # "4K" instead of "4k".
        return "already 4K"
    if settings.plex_allowed_usernames and session.username not in settings.plex_allowed_usernames:
        return "user not in allowed list"
    tmdb_id = await plex.get_tmdb_id(session.rating_key)
    if tmdb_id is None:
        return "no tmdb id found for this movie in Plex"

    movie = await radarr.find_movie_by_tmdb_id(tmdb_id)
    if movie is None:
        return "movie not found in Radarr"

    if not await store.should_check_upgrade(movie.id):
        return "recently attempted, cooling down before retrying automatically"

    return await _claim_and_start_upgrade(movie, plex_rating_key=session.rating_key)


async def force_upgrade_movie(movie_id: int) -> str:
    """Admin/manual trigger for a movie that isn't currently playing (e.g. testing).

    Resolves the Plex ratingKey via a title/tmdb search instead of reading it
    off a live session, then runs through the exact same claim-and-start path
    as the normal session-driven trigger — this is not a separate, looser code
    path, just a different way of supplying the same inputs.
    """
    movie = await radarr.get_movie(movie_id)
    if await store.is_claimed(movie.id):
        return "already has an active job"

    rating_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
    if rating_key is None:
        return "movie not found in Plex library (has it been added/scanned yet?)"

    return await _claim_and_start_upgrade(movie, plex_rating_key=rating_key)


async def _claim_and_start_upgrade(movie: radarr.RadarrMovie, plex_rating_key: str) -> str:
    if await store.is_claimed(movie.id):
        return "already has an active job"

    added_at = None
    try:
        added_at = await plex.get_added_at(plex_rating_key)
    except Exception as exc:  # noqa: BLE001
        await log(f"upgrade: couldn't read {movie.title}'s current Plex added-date (non-critical): {exc}")

    job = await store.try_claim(
        movie.id,
        "upgrade",
        plex_rating_key=plex_rating_key,
        plex_added_at=added_at,
        original_quality_profile_id=movie.quality_profile_id,
        history_baseline_id=await radarr.latest_history_id(movie.id),
    )
    if job is None:
        return "already has an active job"

    await log(f"upgrade: claiming job for {movie.title} — searching for 4K")

    # Deliberately NOT moving the original file here. Confirmed live: an
    # upgrade job is claimed precisely because someone is watching THIS
    # exact file RIGHT NOW — moving it out from under an active Plex stream
    # (even to a safe vault path) can error/lock up the player long before
    # the 4K replacement is anywhere near ready, which defeats the entire
    # "watch normally, then get a clean handoff" point of this feature. The
    # move now happens in _preserve_upgrade_original_if_needed, called only
    # once the download is actually finished and about to be imported — the
    # same moment we're about to interrupt them anyway.
    old_file_path = (movie.raw.get("movieFile") or {}).get("path")
    if old_file_path and preserve.KEEP_SUFFIX in os.path.basename(old_file_path):
        await log(f"upgrade: {movie.title}'s tracked file is already suffixed as a kept original ('{old_file_path}') — needs manual cleanup before it's safe to upgrade, aborting")
        await store.release(movie.id)
        return "tracked file has a broken kept-suffix name — needs manual cleanup first"

    if await radarr.queue_has_movie(movie.id):
        await log(f"upgrade: {movie.title} already has a Radarr queue entry — adopting it instead of re-grabbing")
        await store.update(movie.id, status="downloading")
    else:
        await radarr.set_quality_profile(movie.id, settings.radarr_4k_profile_id)
        await radarr.trigger_search(movie.id)

    asyncio.create_task(_upgrade_job_monitor(movie.id))
    return "started"


async def _upgrade_job_monitor(movie_id: int) -> None:
    deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=settings.upgrade_search_timeout_seconds
    )
    force_event = asyncio.Event()
    _force_events[movie_id] = force_event

    try:
        # Wait for a grab (queue entry), a forced check from the UI, or the timeout.
        while datetime.datetime.now(datetime.timezone.utc) < deadline:
            job = await store.get(movie_id)
            if job is None:
                return
            if await radarr.queue_has_movie(movie_id):
                await store.update(movie_id, status="downloading")
                break
            remaining = (deadline - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            wait_for = min(settings.poll_interval_seconds, max(remaining, 0))
            try:
                await asyncio.wait_for(force_event.wait(), timeout=wait_for)
                if force_event.is_set():
                    await log(f"upgrade: forced check requested for movie {movie_id} — skipping the wait")
                    force_event.clear()
                    await _manual_grab_4k(movie_id)
                    break
            except asyncio.TimeoutError:
                continue
        else:
            await _manual_grab_4k(movie_id)
    finally:
        _force_events.pop(movie_id, None)

    await _monitor_download(movie_id)


async def _manual_grab_4k(movie_id: int) -> bool:
    job = await store.get(movie_id)
    if job is None:
        return False
    if job.attempt_count >= settings.max_download_attempts:
        await _give_up(movie_id, "exhausted manual grab attempts")
        return False

    # Re-check right before grabbing — Radarr's own automatic search (triggered
    # at claim time) may have grabbed something in the gap since the last poll,
    # especially if this was reached via the "force check now" button skipping
    # the normal wait. Confirmed live: without this, both a manual grab and
    # Radarr's own automatic grab landed simultaneously for the same movie.
    if await radarr.queue_has_movie(movie_id):
        await log(f"upgrade: movie {movie_id} already grabbed by Radarr's own search — adopting it instead")
        await store.update(movie_id, status="downloading")
        return True

    releases = await radarr.get_releases(movie_id)
    max_size = settings.max_4k_release_size_gb * (1024**3)
    # Remux allowed here (unlike downgrade) — explicit preference, since a
    # 4K upgrade should get the best quality available, not the smallest.
    # Still excludes actual disc images (iso/bd-disk/dvdr) since those
    # aren't directly playable files. Still bounded by max_4k_release_size_gb.
    #
    # ignore_rejected=True for the same reason as downgrade: confirmed live
    # on 2012 that every single 2160p candidate — a plainly obvious upgrade
    # from an existing 480p file — came back "rejected" with reason
    # "Existing file and the Quality profile does not allow upgrades".
    # That's Radarr's own automatic-search opinion (based on its still-stale
    # view of the file we already moved to the vault), not a real problem;
    # our own explicit grab doesn't need Radarr's permission to proceed.
    upgrade_exclude_terms = ("iso", "bd-disk", "brdisk", "dvdr", "video_ts")
    release = await _pick_release(
        releases, "2160", job.tried_release_guids, max_size_bytes=max_size,
        exclude_terms=upgrade_exclude_terms, ignore_rejected=True, min_seeders=settings.min_release_seeders,
    )
    if release is None:
        # Prefer a well-seeded release, but a rare 4K release with only a
        # couple of seeders is still better than refusing the upgrade outright.
        release = await _pick_release(
            releases, "2160", job.tried_release_guids, max_size_bytes=max_size,
            exclude_terms=upgrade_exclude_terms, ignore_rejected=True, min_seeders=1,
        )
    if release is None:
        available = sorted({r.resolution for r in releases if r.resolution} or {"none"})
        await _give_up(movie_id, f"no 4K release found — only {', '.join(available)} available on your indexers")
        return False

    await radarr.grab_release(release.guid, release.indexer_id)
    tried = job.tried_release_guids + [release.guid]
    await store.update(movie_id, attempt_count=job.attempt_count + 1, tried_release_guids=tried, status="downloading")
    await log(f"upgrade: manually grabbed release '{release.title}' for movie {movie_id}")
    return True


async def _dedupe_queue(movie_id: int) -> Optional[dict]:
    """Self-heal a duplicate grab (e.g. Radarr's own automatic search landing
    the same movie our manual grab already claimed — confirmed to happen live,
    not just theoretical) by keeping the one release that matches our own
    policy (non-Remux preferred, then smallest) and removing the rest, from
    both Radarr's tracking and the download client, so it doesn't silently
    waste bandwidth/disk on a redundant copy. Returns the surviving queue
    record (or None if nothing's queued), so callers don't need a second fetch.
    """
    records = await radarr.queue_records_for_movie(movie_id)
    if not records:
        return None
    if len(records) == 1:
        return records[0]

    def sort_key(r: dict) -> tuple[bool, int]:
        return ("remux" in r.get("title", "").lower(), r.get("size", 0))

    records.sort(key=sort_key)
    keep, *extras = records
    await log(
        f"upgrade: {len(records)} simultaneous queue entries found for movie {movie_id} "
        f"(duplicate grab) — keeping '{keep.get('title')}', removing {len(extras)} redundant download(s)"
    )
    for extra in extras:
        await radarr.remove_queue_item(extra["id"])
    return keep


async def _preserve_upgrade_original_if_needed(movie_id: int) -> None:
    """Move the original file out of Radarr's visible path — but only now,
    right before the 4K download is actually imported, not back when the
    job was first claimed.

    Confirmed live: moving it at claim time meant an actively-playing
    stream lost its underlying file the moment a search started, minutes
    before the 4K replacement was anywhere near ready — locking up
    playback instead of the intended "watch normally, then get a clean
    handoff" experience. This still has to happen before Radarr's own
    import runs (same reasoning as ever: Radarr's own cleanup can destroy a
    renamed-in-place original), just as late as it safely can. Idempotent —
    no-ops if already done, since this gets called on every poll cycle
    while the download looks finished.
    """
    job = await store.get(movie_id)
    if job is None or job.preserved_path:
        return
    movie = await radarr.get_movie(movie_id)
    old_file_path = (movie.raw.get("movieFile") or {}).get("path")
    if not old_file_path:
        return
    if preserve.KEEP_SUFFIX in os.path.basename(old_file_path):
        await log(f"upgrade: {movie.title}'s tracked file is already suffixed as a kept original ('{old_file_path}') — needs manual cleanup, leaving it in place")
        return
    kept_path = await preserve.keep_in_place(old_file_path)
    if kept_path is None:
        await log(f"upgrade: could not preserve the original file for {movie.title} before import — leaving it in place, import may be blocked until this is resolved manually")
        return
    await store.update(movie_id, preserved_path=kept_path, original_file_path=old_file_path)
    await store.record_preserved_file(kept_path, "upgrade", movie_id, old_file_path)
    await log(f"upgrade: {movie.title}'s 4K download is ready to import — moving the original out of the way now")


async def _nudge_import_if_ready(movie_id: int, queue_record: Optional[dict]) -> None:
    """Force Radarr to import right now if the download looks finished.

    Radarr processes finished downloads on its own background schedule, which
    can leave a fully-downloaded file sitting untouched for a while — worse,
    a movie that already has an existing file sometimes needs this nudge
    rather than ever picking itself up automatically. Only fires once per
    "looks done" observation; harmless to call repeatedly since it's a no-op
    if there's nothing to import.

    trackedDownloadStatus "warning" specifically means Radarr's automatic
    importer is refusing to proceed — confirmed live this is Radarr's
    "not an upgrade for existing movie file" guard, which fires on every
    deliberate downgrade regardless of profile. RefreshMonitoredDownloads
    alone will never clear that; it needs an explicit forced manual import.
    """
    if queue_record is None:
        return
    sizeleft = queue_record.get("sizeleft", 0) or 0
    status = str(queue_record.get("status", "")).lower()
    tracked_status = str(queue_record.get("trackedDownloadStatus", "")).lower()
    download_id = queue_record.get("downloadId")

    if tracked_status == "warning" and sizeleft == 0 and download_id:
        await log(f"downgrade: movie {movie_id} download finished but Radarr's auto-import is blocking it (lower quality than existing) — forcing manual import")
        await radarr.force_manual_import(movie_id, download_id)
    elif sizeleft == 0 or status == "completed":
        await radarr.trigger_import_scan()


async def _monitor_download(movie_id: int) -> None:
    while True:
        try:
            job = await store.get(movie_id)
            if job is None:
                return
            if _job_is_stalled(job):
                await _give_up(movie_id, f"stalled — no result after {settings.max_job_duration_hours}h, likely a dead download")
                return
            queue_record = await _dedupe_queue(movie_id)
            if queue_record:
                sizeleft = queue_record.get("sizeleft", 0) or 0
                status = str(queue_record.get("status", "")).lower()
                if sizeleft == 0 or status == "completed":
                    await _preserve_upgrade_original_if_needed(movie_id)
            await _nudge_import_if_ready(movie_id, queue_record)

            if queue_record and _download_too_slow(queue_record):
                await log(
                    f"upgrade: movie {movie_id}'s download has been under "
                    f"{settings.min_download_speed_kbps:.0f} KB/s for over {settings.min_speed_check_after_minutes}m "
                    f"— abandoning it and trying another release instead of waiting it out"
                )
                await radarr.remove_queue_item(queue_record["id"], remove_from_client=True)
                if not await _manual_grab_4k(movie_id):
                    return
            else:
                event = await radarr.new_history_event_type(movie_id, job.history_baseline_id)
                if event == "downloadFolderImported":
                    await store.update(movie_id, status="importing")
                    await _finish_upgrade(movie_id)
                    return
                if event == "downloadFailed":
                    await log(f"upgrade: download failed for movie {movie_id} — trying next release")
                    if not await _manual_grab_4k(movie_id):
                        return
        except Exception as exc:  # noqa: BLE001
            # A transient API timeout here used to kill this whole task
            # silently, orphaning the job with the concurrency slot never
            # freed and nothing watching it anymore — confirmed live on the
            # downgrade side under heavy load. Retry next cycle instead.
            await log(f"upgrade: monitor check failed for movie {movie_id}: {exc} — will retry next cycle")
        await asyncio.sleep(settings.download_monitor_interval_seconds)


async def _finish_upgrade(movie_id: int) -> None:
    # One section-wide refresh up front, before any per-item checks — gives
    # Plex its best shot at matching the new file to the SAME existing
    # library entry (by folder/filename, its most reliable signal) instead
    # of ever treating it as a disconnected new movie with no resume
    # position. Confirmed live (2012, repeatedly) that relying only on a
    # specific item's rating key isn't reliable enough on its own for this.
    try:
        await plex.refresh_movies_section()
    except Exception as exc:  # noqa: BLE001
        await log(f"upgrade: section refresh failed for movie {movie_id}: {exc} — continuing anyway")

    # In-place bounded wait rather than rescheduling via create_task — the
    # recursive version had no stall bound and could wait forever if Plex
    # never confirmed the 4K version. Same fix already applied on the
    # downgrade side after confirming that exact failure mode live.
    while True:
        job = await store.get(movie_id)
        if job is None:
            return
        if _job_is_stalled(job):
            await _give_up(movie_id, f"Plex never confirmed the 4K version within {settings.max_job_duration_hours}h")
            return

        movie = await radarr.get_movie(movie_id)
        if not job.plex_rating_key:
            break
        # Fallback in case Radarr's own Plex "Connect" notification isn't set
        # up (or misfires) — nudge Plex to notice the new file itself rather
        # than waiting on its own scan schedule.
        try:
            await plex.refresh_item_fast(job.plex_rating_key)
            has_4k = await plex.has_4k_version(job.plex_rating_key)
        except Exception as exc:  # noqa: BLE001
            # Same stale-rating-key issue confirmed on the downgrade side —
            # a key captured hours ago at claim time can 404 permanently if
            # Plex re-matches the item in the meantime. Re-resolve instead
            # of retrying the same broken key forever.
            await log(f"upgrade: Plex check failed for {movie.title} (rating key {job.plex_rating_key} may be stale): {exc} — re-resolving")
            new_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
            if new_key and new_key != job.plex_rating_key:
                await store.update(movie_id, plex_rating_key=new_key)
                await log(f"upgrade: re-resolved Plex rating key for {movie.title}: {job.plex_rating_key} -> {new_key}")
            await asyncio.sleep(settings.download_monitor_interval_seconds)
            continue
        if has_4k:
            break
        await log(f"upgrade: import reported for {movie.title} but Plex hasn't confirmed a 4K version yet — waiting")
        await asyncio.sleep(settings.download_monitor_interval_seconds)

    await log(f"upgrade: {movie.title} confirmed available in 4K in Plex")
    if job.plex_added_at:
        try:
            await plex.restore_added_at(job.plex_rating_key, job.plex_added_at)
        except Exception as exc:  # noqa: BLE001
            await log(f"upgrade: couldn't restore {movie.title}'s original added-date in Plex (cosmetic only): {exc}")
    size_gb = movie.file_size / (1024**3) if movie.file_size else 0.0
    pun = random.choice(_UPGRADE_PUN_LINES).format(title=movie.title)
    message = (
        f"{pun} Restart to watch it — your original's untouched for {settings.keep_original_days}d."
    )
    # Matching by title, not job.plex_rating_key — confirmed live (Ron's Gone
    # Wrong) that a key captured even moments earlier can already be wrong
    # again by now under heavy rating-key churn, silently sending nobody the
    # notification while still logging as if it worked. Title stays stable
    # through all of that, and this loop only logs success for sessions it
    # actually found and terminated.
    notified_sessions = await plex.sessions_for_title(movie.title)
    if notified_sessions:
        for session in notified_sessions:
            await plex.terminate_session(session.machine_identifier, message)
        await log(f"upgrade: notified {len(notified_sessions)} active session(s) for {movie.title} via a clean stop (both versions are safe, nothing was deleted)")
    else:
        await log(f"upgrade: {movie.title} — no active Plex session found to notify (viewer may have already stopped watching)")

    if job.original_quality_profile_id:
        await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)

    await store.update(movie_id, status="done")
    await store.release(movie_id)
    await log(
        f"upgrade: done for {movie.title}. Both versions available in Plex; the 4K sticks around for "
        f"{settings.keep_original_days} days, then automatically reverts back to the original to save space "
        f"(the original itself is kept permanently)."
    )
    await store.record_history(
        movie_id, movie.title, "upgrade", "done",
        f"Now available in 4K ({size_gb:.1f} GB). Original kept permanently; 4K reverts after {settings.keep_original_days} days.",
        movie.poster_url,
    )
    await discord.upgrade_ready(movie.title, movie.poster_url, pun)


async def _give_up(movie_id: int, reason: str) -> None:
    job = await store.get(movie_id)
    if job is None:
        return
    movie = await radarr.get_movie(movie_id)
    await log(f"upgrade: giving up on {movie.title} — {reason}. Reverting quality profile.")
    if job.original_quality_profile_id:
        await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)
    # The original gets renamed out of the way at claim time, before any 4K
    # search even runs — if the search then finds nothing (or stalls), the
    # movie would otherwise be left with no visible file at all, hidden
    # under a "-reclaimarr-kept" name, relying on the 7-day timer to ever
    # surface it again. Put it back now instead.
    if await _restore_original(movie_id, job):
        await log(f"upgrade: restored {movie.title}'s original file after giving up")
    elif job.preserved_path:
        await log(f"upgrade: giving up on {movie.title} but couldn't auto-restore the original — leaving it for manual review")
    await store.update(movie_id, status="failed")
    await store.release(movie_id)
    await store.record_upgrade_check(movie_id)
    await store.record_history(movie_id, movie.title, "upgrade", "failed", reason, movie.poster_url)
    await discord.upgrade_unavailable(movie.title, reason, movie.poster_url)


async def force_downgrade_movie(movie_id: int) -> None:
    """Test/admin trigger: run the downgrade evaluation for one specific
    movie right now, instead of waiting for it to come up in a full library
    scan. Reuses the exact same candidate logic as the real scan, but with
    the cooldown ignored and every outcome (even routine skips) recorded to
    History — you asked for this one explicitly, so you should see why it
    did or didn't happen, not just silence.
    """
    global _stop_requested
    _stop_requested = False
    movie = await radarr.get_movie(movie_id)
    scan_status.update(running=True, current_movie=movie.title, last_skip_reason=None)
    try:
        await _downgrade_scan_candidates([movie], verbose_history=True, ignore_cooldown=True, ignore_age_requirement=True)
    finally:
        scan_status["running"] = False


async def run_downgrade_scan_now(ignore_cooldown: bool = False) -> None:
    """Manual trigger for testing — runs one downgrade scan pass immediately
    instead of waiting for DOWNGRADE_SCAN_HOUR_UTC.

    ignore_cooldown bypasses the per-movie DOWNGRADE_RECHECK_DAYS window —
    useful for a genuine full-library sweep when the normal daily scan has
    already touched most of the library recently and would otherwise skip
    almost everything as "checked recently, cooling down".
    """
    try:
        await _downgrade_scan_once(ignore_cooldown=ignore_cooldown)
    except Exception as exc:  # noqa: BLE001
        await log(f"downgrade scan (manual trigger): unexpected error: {exc}")
        raise


async def downgrade_daily_loop() -> None:
    """Workflow 2: once a day, look for large already-watched files to replace with smaller encodes."""
    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        target = now.replace(hour=settings.downgrade_scan_hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        try:
            await _downgrade_scan_once()
        except Exception as exc:  # noqa: BLE001
            await log(f"downgrade loop: unexpected error: {exc}")


_downgrade_semaphore: Optional[asyncio.Semaphore] = None


def _get_downgrade_semaphore() -> asyncio.Semaphore:
    """Caps how many downgrade downloads run at once (DOWNGRADE_MAX_CONCURRENT,
    default 3). Most libraries are mostly large files, so scanning could
    otherwise kick off dozens of simultaneous downloads in one pass — this
    makes the daily scan start new ones only as slots free up.
    """
    global _downgrade_semaphore
    if _downgrade_semaphore is None:
        _downgrade_semaphore = asyncio.Semaphore(settings.downgrade_max_concurrent)
    return _downgrade_semaphore


async def _pick_downgrade_release(releases: list[radarr.RadarrRelease], movie_file_size: int, tried_guids: list[str]) -> Optional[radarr.RadarrRelease]:
    """Try 1080p first, fall back to 720p — older movies especially often
    only have a smaller/older release available, not a fresh 1080p encode.

    Seeders are tried as a floor (settings.min_release_seeders) first, but
    that's a preference, not a hard requirement — some movies genuinely only
    have low-seeder releases available at all, and a slow release is still
    better than skipping the movie entirely. If nothing clears the healthy
    floor, fall back to accepting whatever's actually available (down to 1
    seeder) rather than reporting "no suitable release" for a movie that's
    just rare.
    """
    max_size = movie_file_size * settings.downgrade_size_ratio
    exclude_terms = ("remux", "iso", "bd-disk", "brdisk", "dvdr", "video_ts")
    for min_seeders in (settings.min_release_seeders, 1):
        for resolution in settings.downgrade_target_resolutions:
            release = await _pick_release(
                releases,
                resolution,
                tried_guids,
                max_size_bytes=max_size,
                # remux already excluded by default; also exclude disc images
                # (iso/bd-disk) and DVD structures — since ignore_rejected=True
                # below means we no longer get Radarr's own "BR-DISK is not
                # wanted" protection for free, we need these ourselves now.
                exclude_terms=exclude_terms,
                prefer_terms=("x265", "hevc", "web-dl"),
                sort_smallest_first=True,
                ignore_rejected=True,
                min_seeders=min_seeders,
            )
            if release is not None:
                return release
        if min_seeders == 1:
            break
    return None


scan_status: dict = {
    "running": False,
    "checked": 0,
    "total_candidates": 0,
    "current_movie": None,
    "last_skip_reason": None,
    "started_at": None,
    "stopped": False,
}

_stop_requested = False
_paused_scan: dict = {"candidates": None, "resume_index": 0}


def request_stop_downgrade_scan() -> bool:
    """Stop the scan after the movie currently being evaluated (not mid-movie
    — never interrupts partway through an actual grab/preserve step, only
    between candidates), leaving it resumable from that exact point.
    """
    global _stop_requested
    if not scan_status["running"]:
        return False
    _stop_requested = True
    return True


def can_resume_downgrade_scan() -> bool:
    return _paused_scan["candidates"] is not None


async def resume_downgrade_scan() -> bool:
    if _paused_scan["candidates"] is None:
        return False
    candidates = _paused_scan["candidates"]
    start_index = _paused_scan["resume_index"]
    _paused_scan["candidates"] = None
    await _run_downgrade_candidates(candidates, start_index=start_index)
    return True


async def _downgrade_scan_once(ignore_cooldown: bool = False) -> None:
    threshold_bytes = settings.downgrade_threshold_gb * (1024**3)
    movies = await radarr.list_movies()
    candidates = [m for m in movies if m.has_file and m.file_size > threshold_bytes]
    await _run_downgrade_candidates(candidates, ignore_cooldown=ignore_cooldown)


async def _run_downgrade_candidates(candidates: list[radarr.RadarrMovie], start_index: int = 0, ignore_cooldown: bool = False) -> None:
    global _stop_requested
    _stop_requested = False
    scan_status.update(
        running=True,
        checked=start_index,
        total_candidates=len(candidates),
        current_movie=None,
        last_skip_reason=None,
        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        stopped=False,
    )
    try:
        await _downgrade_scan_candidates(candidates, start_index=start_index, ignore_cooldown=ignore_cooldown)
    finally:
        scan_status["running"] = False
        scan_status["current_movie"] = None


async def _downgrade_scan_candidates(
    candidates: list[radarr.RadarrMovie],
    verbose_history: bool = False,
    ignore_cooldown: bool = False,
    ignore_age_requirement: bool = False,
    start_index: int = 0,
) -> None:
    """verbose_history/ignore_cooldown/ignore_age_requirement are on for
    an explicit forced test of a handful of movies — you picked these movies
    yourself and want to actually see the release search happen, not wait on
    the normal age/recent-watch gates. Off for the routine full-library scan.

    Eligibility (routine scan) is purely time-based, not watched-status —
    explicit instruction: "idc if its watched, if it's added and been a
    while, downgrade it no matter what" — a movie must have sat at full
    quality for DOWNGRADE_MIN_AGE_DAYS since being added, AND not have been
    watched within DOWNGRADE_RECENT_WATCH_DAYS (a movie being actively
    rewatched keeps deferring itself this way, with no separate "popular
    movie" tracking needed).

    "currently being watched" and "excluded" are never bypassed, force or
    not — one's a hard safety rule (never touch a live stream), the other is
    a deliberate choice you made elsewhere that a bulk action shouldn't
    silently override.

    Checked for a stop request between each movie (never mid-grab) — if
    found, saves exactly where it left off so resume_downgrade_scan can
    continue with this same candidate list instead of starting over.
    """
    for index in range(start_index, len(candidates)):
        if _stop_requested:
            _paused_scan["candidates"] = candidates
            _paused_scan["resume_index"] = index
            scan_status["stopped"] = True
            await log(f"downgrade: scan stopped by user at {index}/{len(candidates)} — resumable")
            return

        movie = candidates[index]
        scan_status["checked"] = index + 1
        scan_status["current_movie"] = movie.title

        def skip(reason: str) -> None:
            scan_status["last_skip_reason"] = f"{movie.title}: {reason}"
            if verbose_history:
                asyncio.create_task(store.record_history(movie.id, movie.title, "downgrade", "skipped", reason, movie.poster_url))

        if movie.id in settings.downgrade_excluded_movie_ids:
            skip("kept at full quality (excluded)")
            continue
        if await store.is_claimed(movie.id):
            skip("already has an active job")
            continue
        if not ignore_cooldown and not await store.should_check_downgrade(movie.id):
            skip("checked recently, cooling down")
            continue

        await store.record_downgrade_check(movie.id)

        if not ignore_age_requirement:
            added_str = movie.raw.get("added")
            if added_str:
                added_dt = datetime.datetime.fromisoformat(added_str.replace("Z", "+00:00"))
                age_days = (datetime.datetime.now(datetime.timezone.utc) - added_dt).total_seconds() / 86400
                if age_days < settings.downgrade_min_age_days:
                    skip(f"added {age_days:.0f} days ago, needs {settings.downgrade_min_age_days} days before it's eligible")
                    continue
            rating_key_for_watch_check = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
            if rating_key_for_watch_check:
                last_viewed_days = await plex.days_since_last_viewed(rating_key_for_watch_check)
                if last_viewed_days is not None and last_viewed_days < settings.downgrade_recent_watch_days:
                    skip(f"watched {last_viewed_days:.0f} days ago — deferring for {settings.downgrade_recent_watch_days} days since last watch")
                    continue

        if await plex.is_movie_being_watched_by_title(movie.title):
            skip("currently being watched")
            continue

        rating_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
        added_at = None
        if rating_key:
            try:
                added_at = await plex.get_added_at(rating_key)
            except Exception as exc:  # noqa: BLE001
                await log(f"downgrade: couldn't read {movie.title}'s current Plex added-date (non-critical): {exc}")

        job = await store.try_claim(
            movie.id,
            "downgrade",
            plex_rating_key=rating_key or "",
            plex_added_at=added_at,
            original_quality_profile_id=movie.quality_profile_id,
            original_file_size=movie.file_size,
            history_baseline_id=await radarr.latest_history_id(movie.id),
        )
        if job is None:
            continue

        # Pace indexer queries so a huge library with many large-file candidates
        # doesn't fire release searches back-to-back through Jackett.
        await asyncio.sleep(settings.downgrade_scan_delay_seconds)

        # Switch profile before grabbing (Radarr should know the target
        # profile for this movie going forward). Note this ISN'T what makes
        # release-picking work — _pick_downgrade_release ignores Radarr's
        # "rejected" flag entirely now, since that flag will always say
        # "existing file meets cutoff" for a deliberate downgrade regardless
        # of profile. Confirmed live on Casino and Fight Club: real, good
        # candidates were being thrown away by trusting that flag.
        await radarr.set_quality_profile(movie.id, settings.radarr_1080p_profile_id)
        releases = await radarr.get_releases(movie.id)
        release = await _pick_downgrade_release(releases, movie.file_size, tried_guids=[])
        if release is None:
            reason = "no suitable smaller release found (checked 1080p and 720p — likely no seeders, or genuinely nothing smaller exists)"
            scan_status["last_skip_reason"] = f"{movie.title}: {reason}"
            await log(f"downgrade: {movie.title} — {reason}, skipping")
            await radarr.set_quality_profile(movie.id, job.original_quality_profile_id)
            await store.release(movie.id)
            await store.record_history(movie.id, movie.title, "downgrade", "skipped", reason, movie.poster_url)
            continue

        # Blocks here until a slot frees up — this is what actually throttles
        # concurrent downloads, not just concurrent evaluation.
        semaphore = _get_downgrade_semaphore()
        await semaphore.acquire()

        await log(f"downgrade: replacing {movie.title} with smaller release '{release.title}'")

        old_file_path = (movie.raw.get("movieFile") or {}).get("path")
        if old_file_path and preserve.KEEP_SUFFIX in os.path.basename(old_file_path):
            # Radarr is tracking an already-kept-suffixed file as this movie's
            # real file — a leftover broken state (confirmed live on Pawn
            # Sacrifice) where keep_in_place would silently no-op instead of
            # actually protecting anything, since it refuses to double-rename.
            # Bail out rather than risk Radarr's own import cleanup deleting
            # what it thinks is just "the old file" but is actually the only copy.
            await log(f"downgrade: {movie.title}'s tracked file is already suffixed as a kept original ('{old_file_path}') — this needs manual cleanup before it's safe to downgrade, skipping")
            await radarr.set_quality_profile(movie.id, job.original_quality_profile_id)
            await store.release(movie.id)
            await store.record_history(movie.id, movie.title, "downgrade", "skipped", "Tracked file has a broken kept-suffix name — needs manual cleanup first.", movie.poster_url)
            semaphore.release()
            continue
        if old_file_path:
            kept_path = await preserve.keep_in_place(old_file_path)
            if kept_path is None:
                await log(f"downgrade: could not preserve the original file for {movie.title} — aborting rather than risk it")
                await radarr.set_quality_profile(movie.id, job.original_quality_profile_id)
                await store.release(movie.id)
                semaphore.release()
                continue
            await store.update(movie.id, preserved_path=kept_path, original_file_path=old_file_path)
            await store.record_preserved_file(kept_path, "downgrade", movie.id, old_file_path)
            # No Radarr rescan here either — see the matching note in
            # _claim_and_start_upgrade for why that's deliberate.

        try:
            await radarr.grab_release(release.guid, release.indexer_id)
        except Exception as exc:  # noqa: BLE001
            # Confirmed live (Anchorman 2): a grab can fail for reasons that
            # have nothing to do with the release itself (a transient
            # Radarr/indexer 404) — and by this point the original has
            # already been moved out of Radarr's library. Without this,
            # the exception would propagate out of the whole scan loop
            # (killing every remaining candidate for the night), leave the
            # movie with zero playable files, AND permanently leak this
            # concurrency slot since the semaphore would never be released.
            await log(f"downgrade: grab failed for {movie.title} after moving the original aside: {exc} — restoring original and moving on")
            # Every step below is its own try/except on purpose — confirmed
            # live that _restore_original's own internal Plex call can 404
            # and raise, which (before this guard) escaped THIS except block
            # and killed the whole scan a second time, right as it was
            # recovering from the first crash. Nothing here may be allowed
            # to propagate: store.release/semaphore.release/continue must
            # always run, no matter what goes wrong during cleanup.
            try:
                if await _restore_original(movie.id, job):
                    await log(f"downgrade: restored {movie.title}'s original file after the failed grab")
                else:
                    await log(f"downgrade: WARNING — {movie.title}'s original could not be auto-restored after the failed grab — check '{job.preserved_path}' manually")
            except Exception as restore_exc:  # noqa: BLE001
                await log(f"downgrade: WARNING — restoring {movie.title}'s original after the failed grab raised its own error: {restore_exc} — check '{job.preserved_path}' manually")
            try:
                await radarr.set_quality_profile(movie.id, job.original_quality_profile_id)
            except Exception as profile_exc:  # noqa: BLE001
                await log(f"downgrade: couldn't revert {movie.title}'s quality profile after the failed grab: {profile_exc}")
            await store.release(movie.id)
            await store.record_history(movie.id, movie.title, "downgrade", "failed", f"Grab failed and was rolled back: {exc}", movie.poster_url)
            semaphore.release()
            continue
        await store.update(movie.id, status="downloading", tried_release_guids=[release.guid])
        asyncio.create_task(_downgrade_job_monitor(movie.id, semaphore))


async def preserve_cleanup_loop() -> None:
    """Periodically delete preserved originals once they've aged past
    KEEP_ORIGINAL_DAYS — unless someone's actively watching that exact file
    right now, in which case defer rather than force them out mid-playback.
    A manual delete via the UI can always happen sooner; this is just the
    automatic backstop so preserved files don't pile up forever unmanaged.
    """
    while True:
        try:
            await _preserve_cleanup_once()
        except Exception as exc:  # noqa: BLE001
            await log(f"cleanup loop: unexpected error: {exc}")
        await asyncio.sleep(settings.preserve_cleanup_interval_seconds)


async def library_integrity_loop() -> None:
    """Periodic safety net, independent of the upgrade/downgrade workflows
    and their on/off switches: finds any movie Radarr thinks has a file that
    doesn't actually exist on disk (the exact state Elvis and Rain Man were
    left in after Radarr's own background process deleted their originals
    out from under an in-progress job), or anything genuinely missing, and
    triggers a normal search for it. Never renames, deletes, or preserves
    anything — just makes sure nothing silently stays broken.
    """
    while True:
        try:
            await _library_integrity_check_once()
        except Exception as exc:  # noqa: BLE001
            await log(f"integrity check: unexpected error: {exc}")
        await asyncio.sleep(settings.library_integrity_check_interval_hours * 3600)


async def _library_integrity_check_once() -> None:
    # Deliberately NOT every movie in the library — most of a real library
    # has plenty of legitimately-not-yet-downloaded "wanted" movies, which is
    # completely normal and already Radarr's own job to search for. Checking
    # all of them here would (and, confirmed live, did) blast a search for
    # the entire wanted list, hammering indexers for things nobody asked
    # Reclaimarr to manage. Only movies Reclaimarr itself has ever touched —
    # via a job, past or present — were ever exposed to the preserve/rename
    # mechanism, so those are the only ones where "no file" is a regression
    # rather than just a normal unacquired library entry.
    touched_ids = {int(entry["movie_id"]) for entry in store.history}
    touched_ids |= {int(k) for k in store.active_jobs.keys()}
    if not touched_ids:
        return

    queue = await radarr.get_queue()
    queued_movie_ids = {r.get("movieId") for r in queue}

    problems: list[tuple[radarr.RadarrMovie, bool]] = []
    for movie_id in touched_ids:
        if movie_id in queued_movie_ids or await store.is_claimed(movie_id):
            # Already being downloaded, or an active Reclaimarr job is
            # already handling it — nothing for this check to do.
            continue
        movie = await radarr.get_movie(movie_id)
        file_path = (movie.raw.get("movieFile") or {}).get("path", "")
        phantom = bool(movie.has_file and file_path and not os.path.exists(file_path))
        really_missing = not movie.has_file
        if really_missing or phantom:
            problems.append((movie, phantom))

    if not problems:
        return

    await log(f"integrity check: found {len(problems)} movie(s) with no real file on disk — searching for small replacements")
    for movie, phantom in problems:
        reason = "Radarr's record points at a file that no longer exists on disk" if phantom else "no file at all"
        await log(f"integrity check: '{movie.title}' — {reason} — searching for a small replacement")
        if phantom:
            await radarr.rescan_movie(movie.id)
        grabbed = await _grab_small_replacement(movie)
        if not grabbed:
            await log(f"integrity check: no small release found for {movie.title} — leaving it missing rather than grabbing something huge; check manually")
        await asyncio.sleep(settings.downgrade_scan_delay_seconds)


async def _grab_small_replacement(movie: radarr.RadarrMovie) -> bool:
    """Used only by the integrity check to restore a missing file.

    Confirmed live (13 Hours, 21 Bridges): a plain Radarr auto-search here
    grabs whatever the quality profile considers "best" — which for a movie
    this check is fixing (i.e. one Reclaimarr already managed, almost always
    via a downgrade) silently re-acquires it huge again, undoing the
    downgrade without anyone asking for that. Always picks small instead,
    same exclusions as a normal downgrade pick, just without a ratio-based
    size ceiling since there's no current file left to compare against.
    """
    releases = await radarr.get_releases(movie.id)
    exclude_terms = ("remux", "iso", "bd-disk", "brdisk", "dvdr", "video_ts")
    for min_seeders in (settings.min_release_seeders, 1):
        for resolution in settings.downgrade_target_resolutions:
            release = await _pick_release(
                releases, resolution, [], exclude_terms=exclude_terms,
                prefer_terms=("x265", "hevc", "web-dl"), sort_smallest_first=True,
                ignore_rejected=True, min_seeders=min_seeders,
            )
            if release is not None:
                await radarr.grab_release(release.guid, release.indexer_id)
                await log(
                    f"integrity check: grabbed small replacement '{release.title}' "
                    f"({round(release.size / (1024**3), 1)}GB) for {movie.title}"
                )
                return True
    return False


async def _find_movie_for_kept_path(kept_path: str) -> Optional[radarr.RadarrMovie]:
    """Match a preserved file back to its movie by folder NAME, not by any
    job record — needed because a job can be lost (crash, redeploy) while
    the move to the vault and the eventual re-import both still went through
    fine. Matches by folder name rather than full directory path since the
    kept file's vault directory is a sibling of the movie's own folder, not
    the same path (see preserve.VAULT_ROOT).
    """
    kept_folder_name = os.path.basename(os.path.dirname(kept_path))
    for movie in await radarr.list_movies():
        current_path = (movie.raw.get("movieFile") or {}).get("path", "")
        if current_path and os.path.basename(os.path.dirname(current_path)) == kept_folder_name:
            return movie
    return None


async def _try_early_cleanup(kept: dict) -> bool:
    """Delete a preserved original as soon as its replacement is confirmed
    safe and playable, instead of waiting for the KEEP_ORIGINAL_DAYS backstop.

    This covers the case where the job that did the swap no longer exists to
    run its own confirm-then-delete step (e.g. it was lost across a crash or
    redeploy) — without this, a preserved file with no matching job would
    just sit there untouched until the age-based cleanup below finally kicks
    in, even though the replacement already proved itself.

    Downgrade only. Upgrade's preserved (small) original is a permanent
    keeper, per explicit instruction — it's the big 4K version that's
    temporary here, handled separately in _preserve_cleanup_once.
    """
    info = store.preserved_info(kept["path"])
    if info is not None and info.get("kind") == "upgrade":
        return False
    movie = await _find_movie_for_kept_path(kept["path"])
    if movie is None or not movie.has_file:
        return False
    current_path = (movie.raw.get("movieFile") or {}).get("path", "")
    if not current_path or current_path == kept["path"] or not os.path.exists(current_path):
        return False
    rating_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
    if rating_key is None:
        return False
    if await plex.is_movie_being_watched(rating_key):
        return False
    if not await plex.confirm_media_playable(rating_key, max_size_bytes=movie.file_size):
        return False
    preserve.delete_kept_file(kept["path"])
    await store.forget_preserved_file(kept["path"])
    await log(
        f"cleanup: '{kept['name']}' replacement confirmed playable "
        f"({round(movie.file_size / (1024**3), 2)}GB) — deleting preserved original early"
    )
    return True


async def _preserve_cleanup_once() -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    for kept in preserve.list_kept_files():
        info = store.preserved_info(kept["path"])
        if info is not None and info.get("kind") == "upgrade":
            # Opposite retention policy from downgrade, per explicit
            # instruction: the small original stays forever; the big 4K
            # version is the temporary one and gets reverted away after
            # KEEP_ORIGINAL_DAYS instead.
            try:
                await _try_revert_expired_upgrade(kept, info, now)
            except Exception as exc:  # noqa: BLE001
                await log(f"cleanup: upgrade-revert check failed for {kept['path']}: {exc}")
            continue

        try:
            if await _try_early_cleanup(kept):
                continue
        except Exception as exc:  # noqa: BLE001
            await log(f"cleanup: early-confirm check failed for {kept['path']}: {exc}")
        preserved_at = store.preserved_since(kept["path"])
        if preserved_at is None:
            # We have no record of when WE preserved this file — could be one
            # we didn't rename ourselves, or a lost record. Confirmed live
            # that guessing from filesystem timestamps causes real data loss
            # (both mtime and ctime reflect the original file's age, not the
            # rename, especially over NFS). Refusing to delete without our
            # own recorded timestamp is the safe default.
            continue
        age_days = (now - datetime.datetime.fromisoformat(preserved_at)).total_seconds() / 86400
        if age_days < settings.keep_original_days:
            continue
        if await _is_file_being_played(kept["path"]):
            await log(
                f"cleanup: '{kept['name']}' is past its {settings.keep_original_days}-day window "
                f"but someone's watching it right now — deferring, not forcing them out"
            )
            continue
        try:
            preserve.delete_kept_file(kept["path"])
            await store.forget_preserved_file(kept["path"])
            await log(f"cleanup: auto-deleted preserved original '{kept['name']}' after {age_days:.1f} days")
        except Exception as exc:  # noqa: BLE001
            await log(f"cleanup: failed to delete {kept['path']}: {exc}")


async def _try_revert_expired_upgrade(kept: dict, info: dict, now: datetime.datetime) -> None:
    """After KEEP_ORIGINAL_DAYS, an upgrade's 4K version reverts back to the
    permanently-kept small original instead of the 4K sticking around —
    explicit instruction: 4K is a temporary trial, not a permanent swap.
    Mirrors the no-gain revert: delete the current (big) file, move the
    preserved (small) original back into place.
    """
    preserved_at = info.get("since")
    movie_id = info.get("movie_id")
    original_file_path = info.get("original_file_path")
    if not preserved_at or not movie_id or not original_file_path:
        return
    age_days = (now - datetime.datetime.fromisoformat(preserved_at)).total_seconds() / 86400
    if age_days < settings.keep_original_days:
        return
    if await store.is_claimed(movie_id):
        return  # an active job already owns this movie — don't interfere

    movie = await radarr.get_movie(movie_id)
    current_path = (movie.raw.get("movieFile") or {}).get("path", "")
    if current_path and await _is_file_being_played(current_path):
        await log(
            f"cleanup: {movie.title}'s 4K version is past its {settings.keep_original_days}-day "
            f"window but someone's watching it right now — deferring"
        )
        return

    reverted = await preserve.revert_no_gain(kept["path"], current_path, original_file_path)
    if not reverted:
        await log(f"cleanup: failed to revert {movie.title}'s expired 4K version — check manually ('{kept['path']}')")
        return

    await store.forget_preserved_file(kept["path"])
    await radarr.rescan_movie(movie_id)
    rating_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
    if rating_key:
        try:
            await plex.refresh_item_fast(rating_key)
        except Exception as exc:  # noqa: BLE001
            await log(f"cleanup: Plex refresh failed for {movie.title} after reverting the 4K version (non-critical): {exc}")
    await log(f"cleanup: {movie.title}'s 4K version reverted back to the original after {age_days:.1f} days — 4K removed to save space")
    await store.record_history(
        movie_id, movie.title, "upgrade", "reverted",
        f"4K version removed after {settings.keep_original_days} days — reverted to the original.", movie.poster_url,
    )


async def _is_file_being_played(file_path: str) -> bool:
    sessions = await plex.list_sessions()
    return any(s.file_path == file_path for s in sessions)


async def _downgrade_job_monitor(movie_id: int, semaphore: asyncio.Semaphore) -> None:
    try:
        while True:
            try:
                job = await store.get(movie_id)
                if job is None:
                    return
                if _job_is_stalled(job):
                    await _give_up_downgrade(movie_id, f"stalled — no result after {settings.max_job_duration_hours}h, likely a dead download")
                    return

                # Safety re-check: don't let the destructive swap land while
                # someone started watching after this job was claimed.
                movie = await radarr.get_movie(movie_id)
                if await plex.is_movie_being_watched_by_title(movie.title):
                    await log(f"downgrade: {movie.title} started being watched mid-job — deferring to next scan")
                    if job.original_quality_profile_id:
                        await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)
                    await store.release(movie_id)
                    return

                queue_record = await _dedupe_queue(movie_id)
                await _nudge_import_if_ready(movie_id, queue_record)

                if queue_record and _download_too_slow(queue_record):
                    await log(
                        f"downgrade: {movie.title}'s download has been under "
                        f"{settings.min_download_speed_kbps:.0f} KB/s for over {settings.min_speed_check_after_minutes}m "
                        f"— abandoning it and trying another release instead of waiting it out"
                    )
                    await radarr.remove_queue_item(queue_record["id"], remove_from_client=True)
                    if not await _retry_downgrade_release(movie_id):
                        return
                else:
                    event = await radarr.new_history_event_type(movie_id, job.history_baseline_id)
                    if event == "downloadFolderImported":
                        # job.original_file_size, NOT movie.file_size — by the time an
                        # import is detected, Radarr has already updated the movie
                        # record to the NEW size, so re-fetching here would compare
                        # the new size against itself and always report "no gain".
                        # Confirmed live: this was producing incorrect NO GAIN results
                        # for what were actually successful downgrades.
                        finished = await _finish_downgrade(movie_id, job.original_file_size)
                        if finished:
                            return
                        # Otherwise a no-gain result just triggered a fresh grab —
                        # keep this same monitor loop (and its concurrency slot)
                        # watching rather than exiting.
                    if event == "downloadFailed":
                        await log(f"downgrade: download failed for {movie.title} — trying next release")
                        if not await _retry_downgrade_release(movie_id):
                            return
            except Exception as exc:  # noqa: BLE001
                # A transient API timeout/hiccup here used to kill this whole
                # task silently, orphaning the job — confirmed live, it held a
                # concurrency slot and a movie in limbo with nothing watching
                # it anymore. Log and retry next cycle instead of abandoning it.
                await log(f"downgrade: monitor check failed for movie {movie_id}: {exc} — will retry next cycle")

            await asyncio.sleep(settings.download_monitor_interval_seconds)
    finally:
        semaphore.release()


async def _retry_downgrade_release(movie_id: int, reference_size: Optional[int] = None) -> bool:
    """Same retry-with-next-candidate pattern as the upgrade path: try another
    release (falling back from 1080p to 720p as options run out) up to
    MAX_DOWNLOAD_ATTEMPTS before giving up on this cycle.

    reference_size lets a caller pin the "must be smaller than this" target
    to the original file's size explicitly — needed when this is called
    after a no-gain import, since by then Radarr's own movie.file_size
    already reflects the (useless) file that was just imported, not the
    original being compared against.
    """
    job = await store.get(movie_id)
    if job is None:
        return False
    movie = await radarr.get_movie(movie_id)

    if job.attempt_count >= settings.max_download_attempts:
        await _give_up_downgrade(movie_id, "exhausted retry attempts")
        return False

    releases = await radarr.get_releases(movie_id)
    size_for_comparison = reference_size if reference_size is not None else movie.file_size
    release = await _pick_downgrade_release(releases, size_for_comparison, job.tried_release_guids)
    if release is None:
        await _give_up_downgrade(movie_id, "no more suitable releases (checked 1080p and 720p)")
        return False

    await radarr.grab_release(release.guid, release.indexer_id)
    tried = job.tried_release_guids + [release.guid]
    await store.update(movie_id, attempt_count=job.attempt_count + 1, tried_release_guids=tried, status="downloading")
    await log(f"downgrade: retried with release '{release.title}' for {movie.title}")
    return True


async def _restore_original(movie_id: int, job) -> bool:
    """Put the preserved original back as the active file and remove
    whatever partial/no-gain file might currently be at the tracked path.

    Used whenever a downgrade attempt is abandoned — without this, the
    movie would be left with its only real file hidden under a
    '-reclaimarr-kept' name indefinitely (until the 7-day backstop deleted
    what was, by then, the movie's ONLY copy).
    """
    if not job.preserved_path or not job.original_file_path:
        return False
    movie = await radarr.get_movie(movie_id)
    current_path = (movie.raw.get("movieFile") or {}).get("path", "")
    ok = await preserve.revert_no_gain(job.preserved_path, current_path, job.original_file_path)
    if not ok:
        return False
    await store.forget_preserved_file(job.preserved_path)
    await radarr.rescan_movie(movie_id)
    if job.plex_rating_key:
        # Best-effort only — confirmed live (Anchorman 2) that this refresh
        # can 404 on the same stale-rating-key pattern as every other file
        # swap tonight, and letting that escape here defeats the entire
        # point of _restore_original: the file revert above already
        # succeeded and is what actually matters, Plex noticing promptly is
        # just a nice-to-have. A caller mid-exception-recovery must not be
        # handed a brand new exception out of its own cleanup step.
        try:
            await plex.refresh_item_fast(job.plex_rating_key)
        except Exception as exc:  # noqa: BLE001
            await log(f"jobs: restored movie {movie_id}'s file but Plex refresh failed (non-critical): {exc}")
    return True


async def _give_up_downgrade(movie_id: int, reason: str) -> None:
    job = await store.get(movie_id)
    if job is None:
        return
    movie = await radarr.get_movie(movie_id)
    await log(f"downgrade: giving up on {movie.title} — {reason}. Reverting quality profile.")
    if job.original_quality_profile_id:
        await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)
    if await _restore_original(movie_id, job):
        await log(f"downgrade: restored {movie.title}'s original file after giving up")
    elif job.preserved_path:
        await log(f"downgrade: giving up on {movie.title} but couldn't auto-restore the original — leaving both files for manual review")
    await store.update(movie_id, status="failed")
    await store.release(movie_id)
    await store.record_history(movie_id, movie.title, "downgrade", "failed", reason, movie.poster_url)
    await discord.downgrade_failed(movie.title, reason, movie.poster_url)


async def _finish_downgrade(movie_id: int, old_size: int) -> bool:
    """Waits in-place (not via recursive task rescheduling) for Plex to
    confirm the new file, specifically so the caller's concurrency slot stays
    held for the whole wait — confirmed live that rescheduling via
    create_task released the slot immediately, letting more than the
    configured limit run "at once" during this phase. Bounded by the same
    MAX_JOB_DURATION_HOURS as the download itself so it can't wait forever
    if Plex never confirms.

    Returns True once the job has reached a terminal state (done, failed, or
    given up) — False means a no-gain result just triggered a fresh grab and
    the caller's monitor loop must keep watching rather than exit and free
    up its concurrency slot.
    """
    # One section-wide refresh per import event — see the matching note in
    # _finish_upgrade for why this is more reliable than refreshing a single
    # (possibly stale) rating key: Plex matches primarily by folder/filename,
    # so this gives it the best shot at recognizing the swapped file as the
    # SAME movie rather than a disconnected new one.
    try:
        await plex.refresh_movies_section()
    except Exception as exc:  # noqa: BLE001
        await log(f"downgrade: section refresh failed for movie {movie_id}: {exc} — continuing anyway")

    while True:
        job = await store.get(movie_id)
        if job is None:
            return True
        if _job_is_stalled(job):
            await log(f"downgrade: Plex never confirmed the new file for movie {movie_id} within {settings.max_job_duration_hours}h — giving up waiting")
            if job.original_quality_profile_id:
                await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)
            if await _restore_original(movie_id, job):
                await log(f"downgrade: restored movie {movie_id}'s original file after the Plex-confirm wait stalled")
            await store.update(movie_id, status="failed")
            await store.release(movie_id)
            return True
        movie = await radarr.get_movie(movie_id)

        if not job.plex_rating_key:
            break
        try:
            await plex.refresh_item_fast(job.plex_rating_key)
            confirmed = await plex.confirm_media_playable(job.plex_rating_key, max_size_bytes=old_size)
        except Exception as exc:  # noqa: BLE001
            # A rating key captured hours ago at claim time can go stale if
            # Plex re-matches/rescans the item in the meantime (confirmed
            # live: this produced a permanent 404 that the generic retry
            # logic just kept re-hitting forever, since the same broken key
            # can never succeed). Re-resolve by title instead of endlessly
            # retrying a call that will never work.
            await log(f"downgrade: Plex check failed for {movie.title} (rating key {job.plex_rating_key} may be stale): {exc} — re-resolving")
            new_key = await plex.find_rating_key_by_title(movie.title, movie.tmdb_id)
            if new_key and new_key != job.plex_rating_key:
                await store.update(movie_id, plex_rating_key=new_key)
                await log(f"downgrade: re-resolved Plex rating key for {movie.title}: {job.plex_rating_key} -> {new_key}")
            await asyncio.sleep(settings.download_monitor_interval_seconds)
            continue
        if confirmed:
            break
        await log(f"downgrade: import reported for {movie.title} but Plex hasn't confirmed the new file plays yet — waiting")
        await asyncio.sleep(settings.download_monitor_interval_seconds)

    old_gb = old_size / (1024**3)
    new_gb = movie.file_size / (1024**3)
    if movie.file_size >= old_size:
        # A downgrade that doesn't actually shrink anything is useless —
        # remove that grab and try a different candidate release before
        # giving up entirely, same retry pattern as a failed download.
        no_gain_path = (movie.raw.get("movieFile") or {}).get("path", "")
        await preserve.remove_no_gain_file(no_gain_path, job.original_file_path)
        await log(f"downgrade: grabbed release for {movie.title} ({new_gb:.1f} GB) wasn't smaller than the original ({old_gb:.1f} GB) — trying a different release")
        await store.record_history(
            movie_id, movie.title, "downgrade", "no_gain",
            f"Release ({new_gb:.1f} GB) wasn't smaller than the original ({old_gb:.1f} GB) — trying another candidate.",
            movie.poster_url,
        )
        # Whatever happens next (grabs another release, or exhausts
        # candidates and gives up + restores the original) is handled
        # entirely inside here.
        retried = await _retry_downgrade_release(movie_id, reference_size=job.original_file_size)
        return not retried
    else:
        # Plex has already confirmed the new file plays fine (the check
        # above), so the preserved original has done its job as a safety net
        # — no need to hold it for the full 7-day window on top of that.
        # Deleted now, not on a timer, per explicit instruction: the backup
        # exists purely in case something fails, not as a fixed grace period.
        if job.plex_added_at:
            try:
                await plex.restore_added_at(job.plex_rating_key, job.plex_added_at)
            except Exception as exc:  # noqa: BLE001
                await log(f"downgrade: couldn't restore {movie.title}'s original added-date in Plex (cosmetic only): {exc}")
        if job.preserved_path:
            try:
                preserve.delete_kept_file(job.preserved_path)
                await store.forget_preserved_file(job.preserved_path)
                await log(f"downgrade: confirmed working — deleted preserved original for {movie.title} now instead of waiting")
            except Exception as exc:  # noqa: BLE001
                await log(f"downgrade: confirmed working, but failed to delete preserved original for {movie.title}: {exc}")
        await log(f"downgrade: replaced {movie.title} — {old_gb:.1f}GB -> {new_gb:.1f}GB.")
        await store.record_history(
            movie_id, movie.title, "downgrade", "done",
            f"{old_gb:.1f} GB → {new_gb:.1f} GB (saved {old_gb - new_gb:.1f} GB).", movie.poster_url,
        )
        await discord.downgrade_done(movie.title, old_gb, new_gb, movie.poster_url)

    if job.original_quality_profile_id:
        await radarr.set_quality_profile(movie_id, job.original_quality_profile_id)

    await store.update(movie_id, status="done")
    await store.release(movie_id)
    return True
