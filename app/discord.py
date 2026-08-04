from typing import Optional

import httpx

from .config import settings
from .logger import log

GOLD = 0xD4AF37
RED = 0xE05555

REPO_FOOTER = {"text": "Reclaimarr"}


async def _send(title: str, description: str, color: int, poster_url: Optional[str] = None) -> None:
    if not settings.discord_webhook_url:
        return

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": REPO_FOOTER,
    }
    if poster_url:
        embed["thumbnail"] = {"url": poster_url}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.discord_webhook_url, json={"embeds": [embed]})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a failed notification should never break a workflow
        await log(f"discord: failed to send notification: {exc}")


async def upgrade_ready(movie_title: str, poster_url: Optional[str], pun: Optional[str] = None) -> None:
    headline = pun or f"**{movie_title}** is now available in 4K."
    await _send(
        "🎬 4K Upgrade Ready",
        f"{headline}\nRestart playback to watch in 4K.",
        GOLD,
        poster_url,
    )


async def upgrade_unavailable(movie_title: str, reason: str, poster_url: Optional[str]) -> None:
    await _send(
        "⚠️ No 4K Available",
        f"**{movie_title}** — {reason}.",
        RED,
        poster_url,
    )


async def downgrade_done(movie_title: str, old_gb: float, new_gb: float, poster_url: Optional[str]) -> None:
    await _send(
        "📦 Space Reclaimed",
        f"**{movie_title}** replaced with a smaller encode.\n**{old_gb:.1f} GB → {new_gb:.1f} GB** "
        f"(saved {old_gb - new_gb:.1f} GB)",
        GOLD,
        poster_url,
    )


async def downgrade_failed(movie_title: str, reason: str, poster_url: Optional[str]) -> None:
    await _send(
        "⚠️ Downgrade Failed",
        f"**{movie_title}** — {reason}.",
        RED,
        poster_url,
    )
