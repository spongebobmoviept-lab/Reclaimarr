import asyncio
import datetime
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth_store, connections_store, discord, friendly, plex, preserve, qbittorrent, radarr, settings_store, tautulli, workflows
from .auth import check_credentials, require_login, security
from .config import APP_VERSION, settings
from .jobs import store
from .logger import log

_background_tasks: list[asyncio.Task] = []

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.makedirs(settings.data_dir, exist_ok=True)
    settings_store.load_overrides()
    connections_store.load_overrides()
    await store.load()
    to_resume = await store.recover_on_startup()
    for movie_id, kind in to_resume:
        workflows.resume_job_monitor(movie_id, kind)

    if settings.media_roots:
        await log(f"reclaimarr: media roots: {', '.join(settings.media_roots)}")
    else:
        await log("reclaimarr: WARNING — MEDIA_ROOTS is empty or invalid; no file will ever be moved or deleted")

    if settings.dry_run:
        await log("reclaimarr: DRY RUN mode — mutating Plex/Radarr calls will be logged, not executed")

    if settings.enable_upgrade_workflow:
        allowlist_note = f" (allowed users: {', '.join(settings.plex_allowed_usernames)})" if settings.plex_allowed_usernames else ""
        await log("reclaimarr: starting upgrade poll loop" + allowlist_note)
        _background_tasks.append(asyncio.create_task(workflows.upgrade_poll_loop()))
    else:
        await log("reclaimarr: upgrade workflow disabled (ENABLE_UPGRADE_WORKFLOW=false)")

    if settings.enable_downgrade_workflow:
        await log("reclaimarr: starting downgrade daily loop")
        _background_tasks.append(asyncio.create_task(workflows.downgrade_daily_loop()))
    else:
        await log("reclaimarr: downgrade workflow disabled (ENABLE_DOWNGRADE_WORKFLOW=false)")

    await log(f"reclaimarr: starting preserved-original cleanup loop (auto-delete after {settings.keep_original_days} days)")
    _background_tasks.append(asyncio.create_task(workflows.preserve_cleanup_loop()))

    await log(f"reclaimarr: starting library integrity check loop (every {settings.library_integrity_check_interval_hours}h, runs regardless of workflow on/off switches)")
    _background_tasks.append(asyncio.create_task(workflows.library_integrity_loop()))

    await log(f"reclaimarr: starting daily Discord digest loop (posts at {settings.digest_hour_utc:02d}:00 UTC)")
    _background_tasks.append(asyncio.create_task(workflows.daily_digest_loop()))

    if settings.discord_startup_notice:
        await discord.startup_online()

    yield
    for task in _background_tasks:
        task.cancel()


app = FastAPI(title="Reclaimarr", version=APP_VERSION, lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": APP_VERSION}


@app.get("/favicon.svg")
async def favicon() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "favicon.svg"), media_type="image/svg+xml")


# Explicit no-cache on every static asset — this app changes frequently
# during setup/tuning, and a browser silently serving a stale cached app.js
# (no error, just old code running) is a much worse failure mode than the
# tiny cost of re-fetching these small files on every load.
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    # Checked before auth on purpose: a fresh, unconfigured instance has no
    # working login yet, so gating "/" behind require_login here would just
    # show the browser's native Basic Auth popup with no credentials that
    # could possibly work — a dead end for a first-time visitor. Route them
    # to the wizard instead, which is what actually creates that login.
    if not auth_store.is_setup_complete():
        return RedirectResponse(url="/setup")
    credentials = await security(request)
    check_credentials(request, credentials)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_NO_CACHE_HEADERS)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "setup.html"), headers=_NO_CACHE_HEADERS)


@app.get("/api/setup/status")
async def api_setup_status() -> JSONResponse:
    """Public on purpose — this is the very first call the page makes,
    before any login exists, to decide whether to show the wizard at all.
    """
    return JSONResponse(
        {
            "setup_complete": auth_store.is_setup_complete(),
            "admin_configured": auth_store.is_admin_configured(),
            "plex_configured": bool(settings.plex_url and settings.plex_token),
            "radarr_configured": bool(settings.radarr_url and settings.radarr_api_key),
        }
    )


@app.post("/api/setup/admin")
async def api_setup_admin(body: dict) -> JSONResponse:
    """Creates the one and only admin login. Deliberately not behind
    require_login — there's no login yet to require. Guarded instead by
    "only works once": the moment an admin exists (wizard-created or the
    classic AUTH_PASSWORD env var), this refuses, so nobody else on the LAN
    can hijack an instance after the fact.
    """
    if auth_store.is_admin_configured():
        raise HTTPException(status_code=403, detail="An admin login already exists for this instance")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="Username is required and password must be at least 8 characters")
    auth_store.set_admin(username, password)
    await log(f"setup: admin login created for '{username}'")
    return JSONResponse({"ok": True})


@app.post("/api/setup/test-plex")
async def api_setup_test_plex(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await plex.test_connection(body.get("url", ""), body.get("token", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Plex"))
    return JSONResponse(result)


@app.post("/api/setup/test-radarr")
async def api_setup_test_radarr(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await radarr.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Radarr"))
    return JSONResponse(result)


@app.post("/api/setup/test-tautulli")
async def api_setup_test_tautulli(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    try:
        result = await tautulli.test_connection(body.get("url", ""), body.get("api_key", ""))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Tautulli"))
    return JSONResponse(result)


@app.post("/api/setup/test-qbit")
async def api_setup_test_qbit(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    url = (body.get("url") or "").strip()
    password = body.get("password") or settings.qbit_password
    if not url:
        raise HTTPException(status_code=400, detail="Enter qBittorrent's Web UI address.")
    try:
        result = await qbittorrent.test_connection(url, body.get("username") or "admin", password)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "qBittorrent"))
    return JSONResponse(result)


@app.get("/api/setup/media-roots")
async def api_setup_media_roots(_: str = Depends(require_login)) -> JSONResponse:
    """Radarr's root folders, whether each one is visible inside this
    container, and the media roots Reclaimarr would use for them — so the
    wizard can set MEDIA_ROOTS without anyone hand-editing a file.

    The suggested root is the folder's top-level directory (/media2 for
    /media2/Movies), so the safety vault (<root>/reclaimarr-vault) sits
    outside Radarr's own root folder and never shows up in its library.
    """
    try:
        folders = await radarr.root_folders()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=friendly.explain(exc, "Radarr"))
    result = []
    suggested: list[str] = []
    for path in folders:
        norm = os.path.normpath(path.rstrip("/") or "/")
        parts = [p for p in norm.split("/") if p]
        top = "/" + parts[0] if parts else ""
        visible = os.path.isdir(norm)
        result.append({"path": norm, "visible": visible, "root": top})
        if visible and top and top not in suggested:
            suggested.append(top)
    return JSONResponse({"folders": result, "suggested": suggested, "current": settings.media_roots})


@app.get("/style.css")
async def style(_: str = Depends(require_login)) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "style.css"), media_type="text/css", headers=_NO_CACHE_HEADERS)


@app.get("/app.js")
async def app_js(_: str = Depends(require_login)) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "app.js"), media_type="application/javascript", headers=_NO_CACHE_HEADERS)


@app.get("/api/movies")
async def api_movies(_: str = Depends(require_login)) -> JSONResponse:
    movies = await radarr.list_movies()
    results = []
    for movie in movies:
        job = await store.get(movie.id)
        movie_file_quality = (movie.raw.get("movieFile") or {}).get("quality") or {}
        resolution = (movie_file_quality.get("quality") or {}).get("resolution")
        results.append(
            {
                "id": movie.id,
                "title": movie.title,
                "poster_url": movie.poster_url,
                "resolution": resolution,
                "file_size_gb": round(movie.file_size / (1024**3), 2) if movie.file_size else None,
                "has_file": movie.has_file,
                "downgrade_excluded": movie.id in settings.downgrade_excluded_movie_ids,
                "job": {
                    "kind": job.kind,
                    "status": job.status,
                    "attempt_count": job.attempt_count,
                    "updated_at": job.updated_at,
                }
                if job
                else None,
            }
        )
    return JSONResponse(results)


@app.get("/api/sessions")
async def api_sessions(_: str = Depends(require_login)) -> JSONResponse:
    sessions = await plex.list_sessions()
    return JSONResponse(
        [
            {
                "session_key": s.session_key,
                "title": s.title,
                "media_type": s.media_type,
                "username": s.username,
                "resolution": s.resolution,
                "watched_fraction": round(s.watched_fraction, 3),
            }
            for s in sessions
        ]
    )


@app.post("/api/debug/run-downgrade-scan")
async def api_run_downgrade_scan(ignore_cooldown: bool = False, _: str = Depends(require_login)) -> JSONResponse:
    """Manually trigger one downgrade scan pass right now, for testing —
    the real schedule only fires once a day at DOWNGRADE_SCAN_HOUR_UTC.

    ignore_cooldown=true bypasses the per-movie recheck window for a genuine
    full sweep, e.g. ?ignore_cooldown=true.
    """
    asyncio.create_task(workflows.run_downgrade_scan_now(ignore_cooldown=ignore_cooldown))
    return JSONResponse({"ok": True, "note": "scan started in background, watch the Log tab"})


@app.post("/api/debug/stop-downgrade-scan")
async def api_stop_downgrade_scan(_: str = Depends(require_login)) -> JSONResponse:
    ok = workflows.request_stop_downgrade_scan()
    return JSONResponse({"ok": ok, "note": "stopping after the current movie" if ok else "no scan is running"})


@app.post("/api/debug/resume-downgrade-scan")
async def api_resume_downgrade_scan(_: str = Depends(require_login)) -> JSONResponse:
    if not workflows.can_resume_downgrade_scan():
        raise HTTPException(status_code=400, detail="No stopped scan to resume")
    asyncio.create_task(workflows.resume_downgrade_scan())
    return JSONResponse({"ok": True, "note": "resuming in background"})


@app.post("/api/debug/force-downgrade/{movie_id}")
async def api_force_downgrade_movie(movie_id: int, _: str = Depends(require_login)) -> JSONResponse:
    """Test one specific movie right now instead of waiting for it to come up
    in a full library scan.
    """
    asyncio.create_task(workflows.force_downgrade_movie(movie_id))
    return JSONResponse({"ok": True, "note": "evaluating now, watch the Log/History tabs"})


@app.post("/api/movies/downgrade-exclusion")
async def api_set_downgrade_exclusion(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Mark one or more movies as excluded (or not) from the downgrade workflow.
    Accepts {"movie_ids": [1,2,3], "excluded": true} for both single and bulk use.
    """
    movie_ids = body.get("movie_ids")
    excluded = body.get("excluded")
    if not isinstance(movie_ids, list) or not all(isinstance(m, int) for m in movie_ids) or not isinstance(excluded, bool):
        raise HTTPException(status_code=400, detail="Expected {movie_ids: [int, ...], excluded: bool}")
    result = None
    for movie_id in movie_ids:
        result = settings_store.set_downgrade_excluded(movie_id, excluded)
    return JSONResponse({"downgrade_excluded_movie_ids": result if result is not None else settings.downgrade_excluded_movie_ids})


@app.post("/api/movies/{movie_id}/force-upgrade")
async def api_force_upgrade_movie(movie_id: int, _: str = Depends(require_login)) -> JSONResponse:
    """Trigger the upgrade workflow for a movie that isn't currently playing (e.g. testing)."""
    result = await workflows.force_upgrade_movie(movie_id)
    return JSONResponse({"result": result})


@app.post("/api/movies/{movie_id}/pause-upgrade")
async def api_pause_upgrade_movie(movie_id: int, body: dict, _: str = Depends(require_login)) -> JSONResponse:
    """Temporarily prevents this movie from claiming an upgrade job, so a
    shared viewing (e.g. Servarr's Movie Night) isn't interrupted by a
    surprise mid-movie 4K swap. Body: {"minutes": 1-1440}, default 240.
    Requires the same admin login as every other API route.
    """
    minutes = body.get("minutes", 240)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:
        raise HTTPException(status_code=400, detail="minutes must be an integer between 1 and 1440")
    workflows.pause_upgrade_for(movie_id, minutes)
    await log(f"upgrade: paused for movie {movie_id} for {minutes}m (movie night)")
    return JSONResponse({"ok": True, "paused_minutes": minutes})


@app.post("/api/sessions/{session_key}/trigger-upgrade")
async def api_trigger_upgrade(session_key: str, _: str = Depends(require_login)) -> JSONResponse:
    sessions = await plex.list_sessions()
    session = next((s for s in sessions if s.session_key == session_key), None)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found (has it ended?)")
    result = await workflows.try_process_session_for_upgrade(session)
    return JSONResponse({"result": result})


@app.get("/api/jobs")
async def api_jobs(_: str = Depends(require_login)) -> JSONResponse:
    queue = await radarr.get_queue()
    queue_by_movie: dict[int, dict] = {}
    for record in queue:
        movie_id = record.get("movieId")
        if movie_id is not None:
            queue_by_movie[movie_id] = record

    results = []
    for key, job in store.active_jobs.items():
        entry = {"movie_id": key, **{f: getattr(job, f) for f in job.__dataclass_fields__}}
        record = queue_by_movie.get(job.movie_id)
        if record:
            size = record.get("size", 0) or 0
            sizeleft = record.get("sizeleft", 0) or 0
            entry["download"] = {
                "release_title": record.get("title"),
                "size_gb": round(size / (1024**3), 2) if size else None,
                "percent_complete": round((1 - sizeleft / size) * 100, 1) if size else None,
                "timeleft": record.get("timeleft"),
                "download_client": record.get("downloadClient"),
                "tracked_status": record.get("trackedDownloadStatus"),
            }
        else:
            entry["download"] = None
        results.append(entry)
    return JSONResponse(results)


@app.post("/api/jobs/{movie_id}/force-check")
async def api_force_check(movie_id: int, _: str = Depends(require_login)) -> JSONResponse:
    ok = workflows.request_force_check(movie_id)
    if not ok:
        raise HTTPException(status_code=404, detail="No in-progress upgrade wait for this movie right now")
    return JSONResponse({"ok": True})


@app.post("/api/jobs/{movie_id}/cancel")
async def api_cancel_job(movie_id: int, _: str = Depends(require_login)) -> JSONResponse:
    """Clear a stuck job's tracking without touching any files — for cases
    like a job left endlessly searching because Radarr re-adopted a renamed
    original as its own tracked file, confusing the workflow's expectations.
    """
    job = await store.get(movie_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No active job for this movie")
    await store.release(movie_id)
    await log(f"jobs: manually cancelled stuck {job.kind} job for movie {movie_id} by user request (file left untouched)")
    return JSONResponse({"ok": True})


@app.get("/api/kept-files")
async def api_kept_files(_: str = Depends(require_login)) -> JSONResponse:
    files = preserve.list_kept_files()
    for f in files:
        info = store.preserved_info(f["path"])
        f["kind"] = info.get("kind") if info else None
        preserved_at = store.preserved_since(f["path"])
        if preserved_at is None:
            # No recorded rename time (e.g. predates this tracking, or the
            # record was lost) — filesystem timestamps aren't trustworthy
            # here (see preserve.py), so age is simply unknown rather than
            # guessed.
            f["age_days"] = None
            f["auto_delete_in_days"] = None
            continue
        age_days = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(preserved_at)
        ).total_seconds() / 86400
        f["age_days"] = round(age_days, 1)
        f["auto_delete_in_days"] = round(max(settings.keep_original_days - age_days, 0), 1)
    return JSONResponse(files)


@app.post("/api/kept-files/delete")
async def api_delete_kept_file(body: dict, _: str = Depends(require_login)) -> JSONResponse:
    path = body.get("path", "")
    try:
        deleted = preserve.delete_kept_file(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")
    await log(f"kept-files: manually deleted '{path}' by user request")
    return JSONResponse({"ok": True})


@app.get("/api/connections")
async def api_get_connections(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(connections_store.current_display())


@app.post("/api/connections")
async def api_save_connections(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in connections_store.ALL_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown connection field(s): {unknown}")
    result = connections_store.save_overrides(update)
    await log("connections: settings updated by user")
    return JSONResponse(result)


@app.get("/api/status")
async def api_status(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(
        {
            "dry_run": settings.dry_run,
            "never_terminate_session": settings.never_terminate_session,
            "upgrade_workflow_enabled": settings.enable_upgrade_workflow,
            "downgrade_workflow_enabled": settings.enable_downgrade_workflow,
            "plex_allowed_usernames": settings.plex_allowed_usernames or None,
        }
    )


@app.get("/api/settings")
async def get_settings(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(settings_store.current_editable())


@app.get("/api/plex-users")
async def api_plex_users(_: str = Depends(require_login)) -> JSONResponse:
    """Everyone who could show up in the upgrade allow-list picker.

    Plex's own account list is the primary source — it's always available
    and doesn't depend on Tautulli having already logged a play from that
    person. Tautulli's list (if configured) is merged in on top since it can
    know about profiles Plex's local /accounts doesn't surface.
    """
    plex_known = await plex.list_known_users()
    tautulli_known = await tautulli.list_known_users()
    known = set(plex_known) | set(tautulli_known) | set(settings.plex_allowed_usernames)
    return JSONResponse(
        {
            "known_users": sorted(known),
            "allowed_usernames": settings.plex_allowed_usernames,
        }
    )


@app.post("/api/plex-users")
async def api_set_plex_users(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    usernames = update.get("allowed_usernames")
    if not isinstance(usernames, list) or not all(isinstance(u, str) for u in usernames):
        raise HTTPException(status_code=400, detail="allowed_usernames must be a list of strings")
    return JSONResponse({"allowed_usernames": settings_store.save_allowed_usernames(usernames)})


@app.post("/api/settings")
async def update_settings(update: dict, _: str = Depends(require_login)) -> JSONResponse:
    unknown = [k for k in update if k not in settings_store.EDITABLE_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown/non-editable setting(s): {unknown}")
    try:
        update = settings_store.validate(update)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(settings_store.save_overrides(update))


@app.get("/api/scan-status")
async def api_scan_status(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse({**workflows.scan_status, "can_resume": workflows.can_resume_downgrade_scan()})


@app.get("/api/history")
async def api_history(_: str = Depends(require_login)) -> JSONResponse:
    return JSONResponse(store.history)


@app.get("/api/log")
async def api_log(lines: int = 200, _: str = Depends(require_login)) -> JSONResponse:
    if not os.path.exists(settings.log_file):
        return JSONResponse([])
    with open(settings.log_file, "r", encoding="utf-8") as f:
        all_lines = f.readlines()
    return JSONResponse([line.rstrip("\n") for line in all_lines[-lines:]])
