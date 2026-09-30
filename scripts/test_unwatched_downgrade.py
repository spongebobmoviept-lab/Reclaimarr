#!/usr/bin/env python3
"""Find the N biggest movies with no Tautulli watch history and force-test
the downgrade workflow on each, via Reclaimarr's own API.

This triggers REAL downgrade jobs (searches + grabs) on your library — use
it deliberately, ideally with DRY_RUN=true on the Reclaimarr side first.

Standard library only. Configure with environment variables, then run:

    RADARR_URL=http://radarr:7878 RADARR_API_KEY=... \
    TAUTULLI_URL=http://tautulli:8181 TAUTULLI_API_KEY=... \
    RECLAIMARR_URL=http://localhost:8585 RECLAIMARR_USER=admin RECLAIMARR_PASSWORD=... \
    python scripts/test_unwatched_downgrade.py [count]

Safety notes:
  - Only ever calls Reclaimarr's own /api/debug/force-downgrade endpoint —
    it still enforces every real safety rule (never touches a currently
    playing movie, preserves the original file, concurrent-download cap,
    stall timeout). This script just picks *which* movies to test.
  - Staggers requests 2s apart so it doesn't fire N indexer searches at once.
"""

import base64
import json
import os
import sys
import time
import urllib.request
from urllib.parse import urlencode


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"Missing required environment variable {name}")
    return value.rstrip("/") if name.endswith("_URL") else value


def get_json(url: str, headers: dict | None = None) -> dict | list:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def post(url: str, user: str, password: str) -> None:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, method="POST", data=b"", headers={"Authorization": f"Basic {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    radarr_url, radarr_key = _env("RADARR_URL"), _env("RADARR_API_KEY")
    tautulli_url, tautulli_key = _env("TAUTULLI_URL"), _env("TAUTULLI_API_KEY")
    reclaimarr_url = _env("RECLAIMARR_URL", "http://localhost:8585")
    user, password = _env("RECLAIMARR_USER", "admin"), _env("RECLAIMARR_PASSWORD")
    threshold_gb = float(os.environ.get("SIZE_THRESHOLD_GB", "15"))

    movies = get_json(f"{radarr_url}/api/v3/movie", headers={"X-Api-Key": radarr_key})
    history = get_json(
        f"{tautulli_url}/api/v2?"
        + urlencode({"apikey": tautulli_key, "cmd": "get_history", "media_type": "movie", "length": 500})
    )
    watched_titles = {r["title"] for r in history["response"]["data"]["data"]}

    threshold_bytes = threshold_gb * 1024**3
    candidates = []
    for m in movies:
        if not m.get("hasFile"):
            continue
        size = (m.get("movieFile") or {}).get("size", 0)
        if size > threshold_bytes and m["title"] not in watched_titles:
            candidates.append((size, m["title"], m["id"]))
    candidates.sort(reverse=True)
    candidates = candidates[:count]

    print(f"Testing {len(candidates)} unwatched movies (biggest first):")
    for size, title, movie_id in candidates:
        print(f"  {size / 1024**3:.1f} GB — {title} (id {movie_id})")
        post(f"{reclaimarr_url}/api/debug/force-downgrade/{movie_id}", user, password)
        time.sleep(2)  # don't fire every indexer search in the same instant

    print("\nDone. Check Reclaimarr's History and Active Jobs tabs for results.")


if __name__ == "__main__":
    main()
