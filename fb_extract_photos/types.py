"""Shared dataclasses, type aliases, and file-extension constants.

This module is dependency-free so every other module can import from it
without creating cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NamedTuple, TypeAlias


# ---- Type aliases -----------------------------------------------------------

#: Unix epoch *seconds*. Facebook also emits milliseconds in some fields;
#: those are normalised on read by :func:`normalize_timestamp`.
Timestamp: TypeAlias = int

#: An exiftool tag name → value mapping. Values are stringified at write
#: time, so any type that has a sensible ``str()`` is fine.
ExifTags: TypeAlias = dict[str, object]

#: One of the three kinds of media we copy.
MediaKind: TypeAlias = Literal["photo", "gif", "video"]


# ---- File-extension classification ------------------------------------------

PHOTO_EXT: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
)
GIF_EXT: Final[frozenset[str]] = frozenset({".gif"})
VIDEO_EXT: Final[frozenset[str]] = frozenset(
    {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".gifv"}
)
ALL_EXT: Final[frozenset[str]] = PHOTO_EXT | GIF_EXT | VIDEO_EXT


# ---- Output-folder side files -----------------------------------------------

MANIFEST_NAME: Final[str] = "_manifest.csv"
CACHE_NAME: Final[str] = ".hash_cache.json"
MANIFEST_HEADERS: Final[list[str]] = [
    "dedupe_key", "timestamp", "kind", "origin", "source", "dest",
]


# ---- Records passed between stages ------------------------------------------


class RawMedia(NamedTuple):
    """One media entry as we read it from a JSON manifest.

    Source-path resolution and on-disk classification happen later (in
    :func:`fb_extract_photos.scanners.gather_all`), so this record only
    carries what we can know from the JSON alone.
    """

    uri: str
    timestamp: Timestamp
    origin: str
    extra_exif: ExifTags


@dataclass
class MediaRef:
    """A media file resolved to a real on-disk path, ready to be copied.

    Several JSON entries can resolve to the same file (e.g. an album cover
    that also appears in the album body). :func:`scanners.gather_all`
    collapses those down by source path, keeping the earliest timestamp
    and merging any extra exif tags.

    Attributes
    ----------
    source:
        Absolute path to the file on disk.
    timestamp:
        Capture/upload time in unix seconds — embedded in EXIF and used
        to bucket the file into ``YYYY/MM/``.
    kind:
        ``"photo"`` / ``"gif"`` / ``"video"``. Drives which EXIF tags
        we write (videos get QuickTime atoms in addition).
    origin:
        Short audit tag such as ``"msg:<thread>"``, ``"album:<name>"``,
        ``"post"``. Surfaced in the manifest so you can trace any file
        back to where Facebook stored it.
    extra_exif:
        Tags pulled from ``media_metadata.exif_data`` (GPS, camera_make,
        ISO, upload_ip → UserComment, …). Empty for most message photos.
    """

    source: Path
    timestamp: Timestamp
    kind: MediaKind
    origin: str
    extra_exif: ExifTags = field(default_factory=dict)


# ---- Small pure helpers (kept here so types/utilities live together) -------


def classify(path: Path) -> MediaKind | None:
    """Map a path's extension to a :data:`MediaKind`, or ``None`` if the
    extension isn't one we handle."""
    ext = path.suffix.lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in GIF_EXT:
        return "gif"
    if ext in VIDEO_EXT:
        return "video"
    return None


def normalize_timestamp(ts: int | float | str) -> Timestamp:
    """Coerce a Facebook timestamp to unix seconds.

    Facebook mixes units: ``creation_timestamp`` and ``taken_timestamp``
    are in seconds; ``timestamp_ms`` is in milliseconds. We disambiguate
    by digit count — anything longer than 10 digits is treated as ms and
    truncated. (Ten digits in seconds covers everything up to year 2286.)
    """
    s = str(int(ts))
    return int(s[:10]) if len(s) > 10 else int(s)
