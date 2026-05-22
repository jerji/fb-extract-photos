"""Destination-folder helpers and the run manifest.

The manifest (``photos/_manifest.csv``) is the source of truth for
"what has already been processed". On resume, any ``dedupe_key`` in
the manifest is skipped so the run is incremental and crash-safe.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .types import MediaRef


def safe_dest(out_root: Path, ref: MediaRef) -> Path:
    """Pick a unique destination path under ``out_root/YYYY/MM/``.

    The folder is created if missing. If the chosen filename already
    exists (rare — only happens when two distinct dedup keys happen to
    share a basename), we suffix ``_1``, ``_2``, … until we find a
    free slot.

    .. note::
       This function is **not** safe to call from multiple threads
       concurrently against the same output root — two threads could
       both observe ``.exists() == False`` and pick the same path.
       The caller wraps it in a lock and immediately ``.touch()``s
       the result to reserve the slot.
    """
    dt = datetime.fromtimestamp(ref.timestamp)
    folder = out_root / f"{dt.year:04d}" / f"{dt.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)

    candidate = folder / ref.source.name
    n = 1
    while candidate.exists():
        candidate = folder / f"{ref.source.stem}_{n}{ref.source.suffix}"
        n += 1
    return candidate


def load_manifest_keys(manifest_path: Path) -> set[str]:
    """Return the set of dedupe keys already recorded in the manifest.

    Returns an empty set if the file is absent or unreadable; the
    caller treats that as "no resume state, process everything".
    """
    if not manifest_path.exists():
        return set()
    done: set[str] = set()
    try:
        with open(manifest_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = row.get("dedupe_key")
                if key:
                    done.add(key)
    except OSError:
        pass
    return done
