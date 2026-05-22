"""Perceptual-hash + sha256 deduplication with a persistent cache.

Images are dedup'd by ``imagehash.phash`` so that re-encodes of the same
photo (which Facebook does aggressively — different upload paths often
produce slightly different bytes for the same image) collapse together.
Videos and GIFs fall back to sha256 of the file content.

The cache (``output/.hash_cache.json``) maps a source path to
``{mtime, size, key, phash_size}``; an entry is honoured only when all
three identity fields still match. This makes re-running across
incremental dump expansions essentially free for unchanged files.
"""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError

from .types import MediaRef


# Cached entry shape: kept as a plain dict so it serialises to JSON
# without ceremony. See `hash_refs` for the invariant.
_CacheEntry = dict[str, object]


def _hash_one(args: tuple[str, str, int]) -> tuple[str, str]:
    """Compute a dedup key for a single file.

    Defined at module scope (not as a closure) so
    :class:`ProcessPoolExecutor` can pickle it. The args tuple is
    flattened for the same reason — picking a dataclass across the
    process boundary is more work than packing three primitives.

    Returns ``(source_path_str, key)`` where ``key`` is prefixed with
    ``"phash:"`` or ``"sha256:"`` so the algorithm is obvious in logs.
    """
    source, kind, phash_size = args
    path = Path(source)
    if kind == "photo":
        try:
            with Image.open(path) as im:
                return source, "phash:" + str(
                    imagehash.phash(im.convert("RGB"), hash_size=phash_size)
                )
        except (
            UnidentifiedImageError,
            OSError,
            Image.DecompressionBombError,
            ValueError,
        ):
            # Fall through to sha256 — happens for truncated downloads
            # or formats Pillow doesn't recognise. Better to have a
            # less-clever dedup than to fail the whole run.
            pass
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return source, "sha256:" + h.hexdigest()


def _load_cache(cache_path: Path) -> dict[str, _CacheEntry]:
    """Read the JSON cache or return an empty dict if absent/corrupt."""
    if not cache_path.exists():
        return {}
    try:
        loaded = json.loads(cache_path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def hash_refs(
    refs: list[MediaRef],
    phash_size: int,
    workers: int,
    cache_path: Path,
) -> dict[str, str]:
    """Compute a dedup key per ref, using the on-disk cache when valid.

    Parameters
    ----------
    refs:
        Output of :func:`scanners.gather_all`.
    phash_size:
        Passed through to ``imagehash.phash``. Larger = stricter dedupe
        (fewer false collapses, more bytes per hash). The cache stores
        this value alongside each entry and re-hashes if it changes.
    workers:
        Number of subprocesses for the hashing work pool.
    cache_path:
        Where to read/write the JSON cache. The directory must exist.

    Returns
    -------
    dict mapping ``str(MediaRef.source)`` → dedup key. Files that
    couldn't be stat'd are silently skipped (and won't appear in the
    return value, so :func:`dedupe` will drop them).
    """
    cache = _load_cache(cache_path)

    source_to_key: dict[str, str] = {}
    todo: list[tuple[str, str, int]] = []

    # First pass: separate cache hits from work-to-do.
    for r in refs:
        src = str(r.source)
        try:
            st = r.source.stat()
        except OSError:
            continue
        entry = cache.get(src)
        if (
            entry
            and entry.get("mtime") == st.st_mtime
            and entry.get("size") == st.st_size
            and entry.get("phash_size", phash_size) == phash_size
        ):
            source_to_key[src] = str(entry["key"])
        else:
            todo.append((src, r.kind, phash_size))

    if cache:
        print(
            f"  cache: {len(source_to_key)}/{len(refs)} hits, "
            f"{len(todo)} to hash"
        )

    if not todo:
        return source_to_key

    # Pick a chunksize that keeps workers busy without thrashing the
    # pickling queue. 8 chunks per worker is a fine middle ground.
    chunksize = max(1, len(todo) // (workers * 8) or 1)

    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for src, key in ex.map(_hash_one, todo, chunksize=chunksize):
            source_to_key[src] = key
            try:
                st = Path(src).stat()
                cache[src] = {
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                    "key": key,
                    "phash_size": phash_size,
                }
            except OSError:
                # File vanished between hashing and stat — skip cache
                # update; the result is still usable for this run.
                pass
            done += 1
            if done % 500 == 0:
                print(f"  hashed {done}/{len(todo)}")

    try:
        cache_path.write_text(json.dumps(cache))
    except OSError as e:
        print(f"  ! could not save hash cache: {e}", file=sys.stderr)

    return source_to_key


def dedupe(
    refs: list[MediaRef],
    source_to_key: dict[str, str],
) -> dict[str, MediaRef]:
    """Group refs by their dedup key, keeping the earliest-timestamped
    ref in each group.

    Refs without a key (because hashing failed) are dropped — they'd
    have no way to participate in resume tracking anyway.
    """
    by_key: dict[str, MediaRef] = {}
    for r in refs:
        key = source_to_key.get(str(r.source))
        if key is None:
            continue
        existing = by_key.get(key)
        if existing is None or r.timestamp < existing.timestamp:
            by_key[key] = r
    return by_key
