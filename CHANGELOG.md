# Changelog

## 1.1.0 — 2026-09-30

### Added
- **Several media roots.** `MEDIA_ROOTS` (comma-separated, default `/media2`) replaces the hard-coded root. Each root gets its own `reclaimarr-vault`, so preserving a file never crosses filesystems. Nested roots resolve to the most specific one.
- **Optional qBittorrent boost** (`QBIT_URL`, `QBIT_USERNAME`, `QBIT_PASSWORD`): a 4K upgrade's torrent is force-started and top-prioritised as soon as its hash is known, and un-forced when the job ends. Optional `QBIT_ACTIVE_HASHES_FILE` lists those hashes for other queue tools (off by default).
- **Daily Discord digest** at `DIGEST_HOUR_UTC`, plus new embeds: all-time stats footer, duplicate grab cleaned up, stalled download retried, library integrity fixes (batched per sweep), integrity check skipped, and an optional startup notice (`DISCORD_STARTUP_NOTICE`). The webhook name is configurable (`DISCORD_USERNAME`).
- `POST /api/movies/{id}/pause-upgrade` (admin login required) to hold off upgrades during a shared viewing, e.g. Servarr's Movie Night.
- `UPGRADE_NO_RELEASE_COOLDOWN_HOURS` (default 12): a much longer retry cooldown when no 4K release exists at all.
- Library integrity check: startup delay (`INTEGRITY_STARTUP_DELAY_SECONDS`), a 30-second re-check before treating a file as missing, and a sanity bail-out (`INTEGRITY_SANITY_LIMIT`) when too many files look missing at once.
- Unit tests (`tests/`) and a helper script (`scripts/test_unwatched_downgrade.py`).

### Changed
- Plex session terminate is a single attempt, and a failure no longer aborts the rest of the finish-upgrade flow.
- The `.env` template is now `.env.example` and `.env` is optional (and git-ignored).
- Docker base image pinned to `python:3.12-slim-bookworm`; `/health` reports the version.
- Prebuilt multi-arch images (amd64 + arm64) at `ghcr.io/spongebobmoviept-lab/reclaimarr` (`1.1.0`, `1.1`, `latest`). `docker-compose.yml` uses the image by default; `.env` is optional.

## 1.0.0

Initial release.
