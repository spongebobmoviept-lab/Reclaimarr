import os
from typing import Optional

from .config import settings
from .logger import log

KEEP_SUFFIX = "-reclaimarr-kept"

# Only paths under one of the configured media roots (MEDIA_ROOTS, see
# config.py) are ever renamed, moved, or deleted, regardless of what any
# single job's data claims a path is. Anything else mounted into the
# container (or not mounted at all) is left completely alone. Read live from
# settings on every call so tests and future live-reload can change it.


def allowed_media_roots() -> tuple[str, ...]:
    return tuple(settings.media_roots)


# Preserved originals get MOVED here, out of Radarr's scanned library
# entirely — not just renamed in place within the movie's own folder.
# In practice, renaming alone isn't reliable
# protection: Radarr has its own independent background process that can
# rediscover a renamed file sitting in a movie's folder, re-adopt it as the
# tracked movieFile, and delete it during a later import — regardless of
# anything Reclaimarr itself does or doesn't trigger. A file Radarr's
# library scan never even looks at can't be rediscovered this way.
#
# One vault PER media root rather than a single shared one: two roots can
# be genuinely different filesystems inside the container (e.g. a local
# disk and a network share), and the preserve step below is an os.rename(),
# which cannot cross a filesystem boundary (OSError: Invalid cross-device
# link). Each vault lives under the same root as the files it protects so
# the rename always stays on one device.
VAULT_DIR_NAME = "reclaimarr-vault"


def vault_roots() -> tuple[str, ...]:
    return tuple(os.path.join(root, VAULT_DIR_NAME) for root in allowed_media_roots())


def _is_within_root(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    return real == real_root or real.startswith(real_root + os.sep)


def _matching_root(path: str, roots: tuple) -> Optional[str]:
    """The most specific of `roots` that contains `path`, or None if none
    does (longest match wins, so nested roots resolve to the inner one)."""
    matches = [root for root in roots if _is_within_root(path, root)]
    if not matches:
        return None
    return max(matches, key=len)


def _is_within_allowed_root(path: str) -> bool:
    return _matching_root(path, allowed_media_roots()) is not None


def _is_within_vault(path: str) -> bool:
    return _matching_root(path, vault_roots()) is not None


def _vault_root_for(source_path: str) -> Optional[str]:
    """The vault that lives on the SAME filesystem as source_path, so the
    eventual os.rename() into it can never cross a device boundary.
    """
    media_root = _matching_root(source_path, allowed_media_roots())
    if media_root is None:
        return None
    return os.path.join(media_root, VAULT_DIR_NAME)


def _vault_path(source_path: str) -> str:
    """Same movie-folder name and filename, just rooted under that source's
    own vault (see _vault_root_for) instead of sitting inside the movie's
    own (Radarr-scanned) folder.
    """
    vault_root = _vault_root_for(source_path)
    if vault_root is None:
        # Callers (keep_in_place) are expected to have already checked
        # _is_within_allowed_root before ever computing a vault path — this
        # would be a programming error, not a runtime condition to handle
        # quietly.
        raise ValueError(
            f"'{source_path}' is not under any allowed media root "
            f"({', '.join(allowed_media_roots())}) — cannot compute a vault path for it"
        )
    movie_folder = os.path.basename(os.path.dirname(source_path))
    base, ext = os.path.splitext(os.path.basename(source_path))
    vault_dir = os.path.join(vault_root, movie_folder)
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
        await log(f"keep-in-place: REFUSING — '{source_path}' is outside all allowed media roots ({', '.join(allowed_media_roots())}), not touching it")
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
    for vault_root in vault_roots():
        if not os.path.isdir(vault_root):
            continue
        for dirpath, _dirnames, filenames in os.walk(vault_root):
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
        raise ValueError(f"Refusing to delete a path outside the vault ({', '.join(vault_roots())})")
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
        await log(f"remove-no-gain-file: REFUSING — '{path}' is outside all allowed media roots ({', '.join(allowed_media_roots())})")
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
        await log(f"revert-no-gain: REFUSING — '{kept_path}' isn't in the vault ({', '.join(vault_roots())})")
        return False
    if not _is_within_allowed_root(original_path):
        await log(f"revert-no-gain: REFUSING — '{original_path}' is outside all allowed media roots ({', '.join(allowed_media_roots())})")
        return False
    if no_gain_path and not _is_within_allowed_root(no_gain_path):
        await log(f"revert-no-gain: REFUSING — '{no_gain_path}' is outside all allowed media roots ({', '.join(allowed_media_roots())})")
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
