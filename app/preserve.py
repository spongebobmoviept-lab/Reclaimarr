import os
from typing import Optional

from .logger import log

KEEP_SUFFIX = "-reclaimarr-kept"

# Movies live on the ten_tb share (/media2). four_tb (/media1) is TV shows —
# not something Reclaimarr manages yet — and sits on a pool with disks that
# already have real SMART errors, so it must never be renamed, deleted, or
# even listed, regardless of what any single job's data claims a path is.
# Update this (and the docker-compose mount) if/when TV show support is added.
ALLOWED_MEDIA_ROOT = "/media2"

# Preserved originals get MOVED here, out of Radarr's scanned library
# entirely — not just renamed in place within the movie's own folder.
# Confirmed live (Elvis, Rain Man) that renaming alone isn't reliable
# protection: Radarr has its own independent background process that can
# rediscover a renamed file sitting in a movie's folder, re-adopt it as the
# tracked movieFile, and delete it during a later import — regardless of
# anything Reclaimarr itself does or doesn't trigger. A file Radarr's
# library scan never even looks at can't be rediscovered this way.
VAULT_ROOT = os.path.join(ALLOWED_MEDIA_ROOT, "reclaimarr-vault")


def _is_within_root(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real == real_root or real.startswith(real_root + os.sep)


def _is_within_allowed_root(path: str) -> bool:
    return _is_within_root(path, ALLOWED_MEDIA_ROOT)


def _is_within_vault(path: str) -> bool:
    return _is_within_root(path, VAULT_ROOT)


def _vault_path(source_path: str) -> str:
    """Same movie-folder name and filename, just rooted under VAULT_ROOT
    instead of sitting inside the movie's own (Radarr-scanned) folder.
    """
    movie_folder = os.path.basename(os.path.dirname(source_path))
    base, ext = os.path.splitext(os.path.basename(source_path))
    vault_dir = os.path.join(VAULT_ROOT, movie_folder)
    candidate = os.path.join(vault_dir, f"{base}{KEEP_SUFFIX}{ext}")
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(vault_dir, f"{base}{KEEP_SUFFIX}-{counter}{ext}")
        counter += 1
    return candidate


async def keep_in_place(source_path: str) -> Optional[str]:
    """Move a movie's current file out of Radarr's scanned library into the
    vault, so it survives both Radarr's own upgrade-import cleanup AND its
    independent background rediscovery of renamed-but-still-present files.
    After this, Radarr sees the file as genuinely missing and will grab+
    import fresh.

    This is a move (rename across directories on the same filesystem),
    instant regardless of file size — never doubles disk usage. Returns the
    new path, or None if the source doesn't exist (nothing to preserve).
    """
    if _is_within_vault(source_path):
        await log(f"keep-in-place: '{source_path}' is already in the vault, not moving again")
        return source_path

    if not source_path or not _is_within_allowed_root(source_path):
        await log(f"keep-in-place: REFUSING — '{source_path}' is outside {ALLOWED_MEDIA_ROOT}, not touching it")
        return None

    if not os.path.exists(source_path):
        await log(f"keep-in-place: source path missing or inaccessible, skipping: {source_path}")
        return None

    dest_path = _vault_path(source_path)
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        os.rename(source_path, dest_path)
    except OSError as exc:
        await log(f"keep-in-place: FAILED moving {source_path}: {exc}")
        return None

    await log(f"keep-in-place: '{source_path}' -> '{dest_path}' — original moved out of Radarr's library entirely")
    return dest_path


def list_kept_files() -> list[dict]:
    """Lists preserved originals from the vault — deliberately does NOT
    report a filesystem timestamp for age. Confirmed live: both mtime and
    ctime reflect the ORIGINAL file's age (months old), not when it was
    preserved, especially over NFS — trusting either caused real files to be
    auto-deleted within minutes instead of after 7 days. Callers must use
    JobStore.preserved_since(path) for the real preservation timestamp.
    """
    results = []
    if os.path.isdir(VAULT_ROOT):
        for dirpath, _dirnames, filenames in os.walk(VAULT_ROOT):
            for name in filenames:
                path = os.path.join(dirpath, name)
                results.append(
                    {
                        "path": path,
                        "name": name,
                        "size_gb": round(os.path.getsize(path) / (1024**3), 2),
                    }
                )
    return results


def delete_kept_file(path: str) -> bool:
    """Delete a preserved original — only ever a file actually sitting in
    the vault (identified by location, not just a filename pattern
    somewhere under the whole media root), since this is exposed via the API.
    """
    real_path = os.path.realpath(path)
    if not _is_within_vault(real_path):
        raise ValueError(f"Refusing to delete a path outside the vault ({VAULT_ROOT})")
    if KEEP_SUFFIX not in os.path.basename(real_path):
        raise ValueError("Refusing to delete a file not marked as a Reclaimarr-kept original")
    if not os.path.isfile(real_path):
        return False
    os.remove(real_path)
    return True


async def remove_no_gain_file(path: str, protected_path: str) -> bool:
    """Delete a just-imported file that turned out not to save any space —
    guarded the same way as everything else here (must be within the
    allowed media root), plus a check against the path that must never be
    touched (the original this download was meant to replace).
    """
    if not path or not os.path.exists(path):
        return False
    if not _is_within_allowed_root(path):
        await log(f"remove-no-gain-file: REFUSING — '{path}' is outside {ALLOWED_MEDIA_ROOT}")
        return False
    if path == protected_path:
        await log(f"remove-no-gain-file: REFUSING — '{path}' is the protected original, not a no-gain download")
        return False
    try:
        os.remove(path)
        return True
    except OSError as exc:
        await log(f"remove-no-gain-file: failed to remove '{path}': {exc}")
        return False


async def revert_no_gain(kept_path: str, no_gain_path: str, original_path: str) -> bool:
    """Undo a downgrade that didn't actually save any space: delete the
    file that was just grabbed and imported, then rename the preserved
    original back to its normal name so exactly one file remains — the
    whole point of downgrading is to save space, so ending up with two
    same-size copies sitting on disk defeats it.
    """
    if not _is_within_vault(kept_path):
        await log(f"revert-no-gain: REFUSING — '{kept_path}' isn't in the vault ({VAULT_ROOT})")
        return False
    if not _is_within_allowed_root(original_path):
        await log(f"revert-no-gain: REFUSING — '{original_path}' is outside {ALLOWED_MEDIA_ROOT}")
        return False
    if no_gain_path and not _is_within_allowed_root(no_gain_path):
        await log(f"revert-no-gain: REFUSING — '{no_gain_path}' is outside {ALLOWED_MEDIA_ROOT}")
        return False
    if KEEP_SUFFIX not in os.path.basename(kept_path):
        await log(f"revert-no-gain: REFUSING — '{kept_path}' isn't a Reclaimarr-kept original")
        return False

    # no_gain_path can legitimately be empty — e.g. reverting a stalled job
    # where nothing ever finished importing, so there's nothing to remove.
    if no_gain_path and os.path.exists(no_gain_path):
        try:
            os.remove(no_gain_path)
        except OSError as exc:
            await log(f"revert-no-gain: failed to remove the no-gain file '{no_gain_path}': {exc}")
            return False

    if os.path.exists(original_path):
        await log(f"revert-no-gain: '{original_path}' unexpectedly already exists, not overwriting — leaving the kept original as-is")
        return False

    try:
        os.rename(kept_path, original_path)
    except OSError as exc:
        await log(f"revert-no-gain: failed to restore '{kept_path}' -> '{original_path}': {exc}")
        return False

    await log(f"revert-no-gain: restored '{original_path}' — no-gain download removed, original back in place")
    return True
