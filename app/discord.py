from typing import Optional

import httpx

from .config import settings
from .jobs import store
from .logger import log

GOLD = 0xD4AF37
RED = 0xE05555
TEAL = 0x4FB6A8
ORANGE = 0xE0A555
PURPLE = 0x9B7ED9

AUTHOR = {"name": "Reclaimarr", "icon_url": "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f3ac.png"}


def _stats_footer() -> dict:
    s = store.stats
    return {
        "text": (
            f"{int(s['upgrades_done'])} upgraded • {int(s['downgrades_done'])} downgraded • "
            f"{s['gb_saved_total']:.0f} GB saved all-time"
        )
    }


async def _send(
    title: str,
    description: str,
    color: int,
    poster_url: Optional[str] = None,
    fields: Optional[list[dict]] = None,
    large_image: bool = True,
) -> None:
    if not settings.discord_webhook_url:
        return

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "author": AUTHOR,
        "footer": _stats_footer(),
    }
    if fields:
        embed["fields"] = fields
    if poster_url:
        # Large image for the four "something happened to a movie" headline
        # events (upgrade/downgrade outcomes) so they're eye-catching in the
        # channel; small thumbnail for the quieter operational notices
        # (dedupe/stall/integrity) so they don't visually dominate a channel
        # that's mostly seeing the headline events.
        embed["image" if large_image else "thumbnail"] = {"url": poster_url}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Display name is configurable (DISCORD_USERNAME) so several
            # tools sharing one channel can all post under a single identity
            # if you prefer; the embed's author line still says Reclaimarr.
            resp = await client.post(settings.discord_webhook_url, json={"username": settings.discord_username, "embeds": [embed]})
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a failed notification should never break a workflow
        await log(f"discord: failed to send notification: {exc}")


async def upgrade_ready(movie_title: str, poster_url: Optional[str], pun: Optional[str] = None, size_gb: Optional[float] = None) -> None:
    headline = pun or f"**{movie_title}** is now available in 4K."
    fields = [{"name": "Size", "value": f"{size_gb:.1f} GB", "inline": True}] if size_gb else None
    await _send(
        "\U0001f3ac 4K Upgrade Ready",
        f"{headline}\nRestart playback to watch in 4K.",
        GOLD,
        poster_url,
        fields=fields,
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
        "\U0001f4e6 Space Reclaimed",
        f"**{movie_title}** replaced with a smaller encode.",
        GOLD,
        poster_url,
        fields=[
            {"name": "Before", "value": f"{old_gb:.1f} GB", "inline": True},
            {"name": "After", "value": f"{new_gb:.1f} GB", "inline": True},
            {"name": "Saved", "value": f"{old_gb - new_gb:.1f} GB", "inline": True},
        ],
    )


async def downgrade_failed(movie_title: str, reason: str, poster_url: Optional[str]) -> None:
    await _send(
        "⚠️ Downgrade Failed",
        f"**{movie_title}** — {reason}.",
        RED,
        poster_url,
    )


async def duplicate_removed(movie_title: str, kept_title: str, removed_count: int, poster_url: Optional[str]) -> None:
    await _send(
        "\U0001f9f9 Duplicate Grab Cleaned Up",
        f"**{movie_title}** had {removed_count + 1} simultaneous downloads — kept the best one, removed the rest.",
        TEAL,
        poster_url,
        fields=[{"name": "Kept", "value": kept_title[:1024], "inline": False}],
        large_image=False,
    )


async def download_stalled_retry(movie_title: str, workflow: str, poster_url: Optional[str]) -> None:
    await _send(
        "\U0001f422 Download Too Slow — Retrying",
        f"**{movie_title}**'s {workflow} download stalled below the speed floor — abandoned it and grabbed a different release.",
        ORANGE,
        poster_url,
        large_image=False,
    )


async def integrity_fixed(movie_title: str, reason: str, poster_url: Optional[str]) -> None:
    await _send(
        "\U0001f527 Library Integrity Fix",
        f"**{movie_title}** — {reason}. Grabbed a small replacement.",
        TEAL,
        poster_url,
        large_image=False,
    )


async def integrity_fixed_batch(fixes: list[tuple[str, str]]) -> None:
    """A whole sweep can flag several movies at once — one message listing
    all of them instead of one post per movie, which used to bury whatever
    live board was sitting above it in the channel.
    """
    lines = [f"**{title}** — {reason}" for title, reason in fixes[:15]]
    if len(fixes) > 15:
        lines.append(f"…and {len(fixes) - 15} more")
    await _send(
        f"\U0001f527 Library Integrity Fix — {len(fixes)} movie(s)",
        "\n".join(lines) + "\n\nGrabbed small replacements for each.",
        TEAL,
    )


async def integrity_check_suspicious(count: int) -> None:
    """A handful of genuinely broken files is normal. Dozens at once
    (e.g. a whole library right after a host reboot) means the
    library mount almost certainly isn't up yet, not mass data loss —
    surfaced here instead of silently grabbing replacements for all of them.
    """
    await _send(
        "⚠️ Integrity Check Skipped This Pass",
        (
            f"Found **{count}** movies with no file on disk in one sweep — way more than normal. "
            "This almost always means the library mount wasn't fully up yet (e.g. right after a reboot), "
            "not that this many files actually vanished. Didn't grab any replacements this pass — "
            "will check again next cycle once things have settled."
        ),
        ORANGE,
    )


async def daily_digest(stats: dict) -> None:
    lines = []
    if stats["upgrades_done"]:
        lines.append(f"\U0001f3ac {stats['upgrades_done']} movie(s) upgraded to 4K")
    if stats["downgrades_done"]:
        lines.append(f"\U0001f4e6 {stats['downgrades_done']} movie(s) reclaimed — **{stats['gb_saved']:.1f} GB** saved")
    if stats["duplicates_removed"]:
        lines.append(f"\U0001f9f9 {stats['duplicates_removed']} duplicate grab(s) cleaned up")
    if stats["stalls_recovered"]:
        lines.append(f"\U0001f422 {stats['stalls_recovered']} stalled download(s) recovered")
    if stats["integrity_fixes"]:
        lines.append(f"\U0001f527 {stats['integrity_fixes']} library integrity fix(es)")
    if stats["failed"]:
        lines.append(f"⚠️ {stats['failed']} job(s) failed — check the History tab")

    description = "\n".join(lines) if lines else "Quiet day — nothing to report."
    await _send("\U0001f4ca Reclaimarr Daily Digest", description, PURPLE)


async def startup_online() -> None:
    await _send(
        "✅ Reclaimarr Online",
        "Watching Plex sessions and the library. Ready to go.",
        GOLD,
    )
