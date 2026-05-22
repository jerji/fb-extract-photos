"""Walk a Facebook dump and emit :class:`MediaRef` records.

The dump's directory layout is documented in the project README. Each
``collect_from_*`` function knows the shape of one specific JSON file
(message thread, album, posts timeline, …) and returns ``RawMedia``
tuples. :func:`gather_all` orchestrates the walk and resolves URIs to
real on-disk files.

Only one filter is enforced: for *message* JSON we require
``sender_name == user_name``. For things that are inherently yours
(albums, posts, uncategorized photos, your_videos) we accept everything.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from .types import (
    ExifTags,
    MediaRef,
    RawMedia,
    Timestamp,
    classify,
    normalize_timestamp,
)


# ---- media_metadata.exif_data parsing ---------------------------------------


def extract_metadata(obj: dict) -> tuple[ExifTags, Timestamp | None]:
    """Pull EXIF tags and a more-accurate ``taken_timestamp`` from a
    Facebook media object's ``media_metadata.exif_data`` block.

    Most messenger photos have nothing here; posts and uploaded photos
    sometimes carry GPS, camera info, and ``upload_ip``. We translate
    Facebook's field names into the exiftool tag names so the result
    can be passed straight to :func:`exif.build_exif_args`.

    Returns
    -------
    (tags, taken_timestamp):
        ``tags`` is empty if Facebook recorded nothing useful.
        ``taken_timestamp`` is None unless ``taken_timestamp`` was
        present and non-zero — when set, callers should prefer it over
        ``creation_timestamp`` (the latter is the *upload* time).
    """
    md = obj.get("media_metadata") or {}
    pm = md.get("photo_metadata") or md.get("video_metadata") or {}
    tags: ExifTags = {}
    taken: Timestamp | None = None

    for entry in pm.get("exif_data") or []:
        if not isinstance(entry, dict):
            continue

        tt = entry.get("taken_timestamp")
        if tt:
            taken = normalize_timestamp(tt)

        # GPS — filter (0, 0) which is "null island" and almost always
        # means the device dropped a placeholder rather than a real fix.
        lat = entry.get("latitude")
        lon = entry.get("longitude")
        if (
            isinstance(lat, (int, float))
            and isinstance(lon, (int, float))
            and not (lat == 0 and lon == 0)
        ):
            tags["GPSLatitude"] = abs(lat)
            tags["GPSLatitudeRef"] = "N" if lat >= 0 else "S"
            tags["GPSLongitude"] = abs(lon)
            tags["GPSLongitudeRef"] = "E" if lon >= 0 else "W"

        # Direct one-to-one Facebook → exiftool tag mappings.
        for src_key, dst_tag in (
            ("camera_make", "Make"),
            ("camera_model", "Model"),
            ("iso", "ISO"),
            ("focal_length", "FocalLength"),
            ("f_stop", "FNumber"),
            ("exposure_time", "ExposureTime"),
            ("orientation", "Orientation"),
        ):
            value = entry.get(src_key)
            if value not in (None, ""):
                tags[dst_tag] = value

        # upload_ip is interesting historical breadcrumb; stash in
        # UserComment so Photos apps surface it (but only the first one
        # if multiple entries have IPs).
        ip = entry.get("upload_ip")
        if ip and "UserComment" not in tags:
            tags["UserComment"] = f"Facebook upload IP: {ip}"

    return tags, taken


def iter_media_from_obj(
    obj: dict, fallback_ts: Timestamp | None
) -> list[tuple[str, Timestamp, ExifTags]]:
    """Extract ``(uri, timestamp, extra_exif)`` from one Facebook media
    object.

    ``fallback_ts`` is used when the object has no ``creation_timestamp``
    of its own — for messages this is the enclosing ``timestamp_ms``;
    for posts the enclosing ``timestamp``.

    Returns at most one triple. We return ``list`` (not a single value)
    so callers can ``.extend()`` cleanly when iterating arrays.
    """
    uri = obj.get("uri")
    if not isinstance(uri, str) or uri.startswith(("http://", "https://")):
        return []
    extra, taken = extract_metadata(obj)
    # Prefer the actual capture time over the upload time.
    ts = taken or obj.get("creation_timestamp") or fallback_ts
    if not ts:
        return []
    return [(uri, normalize_timestamp(ts), extra)]


# ---- Per-JSON-shape collectors ----------------------------------------------
#
# Each of these knows the structure of one specific export file type.
# They are intentionally small and unforgiving — any unexpected shape
# silently yields nothing rather than raising. This is appropriate
# because Facebook's schema drifts version to version and we'd rather
# skip a file than abort the whole run.


def _safe_load(json_path: Path) -> object | None:
    """Read a JSON file or return None (and log) on failure."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! skip {json_path}: {e}", file=sys.stderr)
        return None


def collect_from_message_thread(
    json_path: Path, user_name: str
) -> list[RawMedia]:
    """Collect media YOU sent in one ``message_*.json`` thread file."""
    data = _safe_load(json_path)
    if not isinstance(data, dict):
        return []

    results: list[RawMedia] = []
    origin = json_path.parent.name
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("sender_name") != user_name:
            continue
        ts_ms = msg.get("timestamp_ms")
        fallback = normalize_timestamp(ts_ms) if ts_ms else None
        # `photos`, `videos`, `gifs` all share the same item shape.
        for key in ("photos", "videos", "gifs"):
            for item in msg.get(key) or []:
                if not isinstance(item, dict):
                    continue
                for uri, ts, extra in iter_media_from_obj(item, fallback):
                    results.append(RawMedia(uri, ts, f"msg:{origin}", extra))
    return results


def collect_from_posts_uncategorized(json_path: Path) -> list[RawMedia]:
    """Collect from ``posts/your_uncategorized_photos.json``."""
    data = _safe_load(json_path)
    if not isinstance(data, dict):
        return []
    out: list[RawMedia] = []
    for item in data.get("other_photos_v2") or []:
        if isinstance(item, dict):
            for uri, ts, extra in iter_media_from_obj(item, None):
                out.append(RawMedia(uri, ts, "uncategorized", extra))
    return out


def collect_from_posts_timeline(json_path: Path) -> list[RawMedia]:
    """Collect from ``posts/your_posts__check_ins__photos_and_videos_*.json``
    (also the same shape as ``birthday_media.json``)."""
    data = _safe_load(json_path)
    if not isinstance(data, list):
        return []
    out: list[RawMedia] = []
    for post in data:
        if not isinstance(post, dict):
            continue
        post_ts = post.get("timestamp")
        fallback = normalize_timestamp(post_ts) if post_ts else None
        for att in post.get("attachments") or []:
            for entry in att.get("data") or []:
                media = entry.get("media") if isinstance(entry, dict) else None
                if isinstance(media, dict):
                    for uri, ts, extra in iter_media_from_obj(media, fallback):
                        out.append(RawMedia(uri, ts, "post", extra))
    return out


def collect_from_album(json_path: Path) -> list[RawMedia]:
    """Collect from one ``posts/album/<n>.json`` file (plus its
    ``cover_photo`` field)."""
    data = _safe_load(json_path)
    if not isinstance(data, dict):
        return []
    name = (data.get("name") or json_path.stem).strip() or "album"
    tag = f"album:{name}"
    out: list[RawMedia] = []
    for item in data.get("photos") or []:
        if isinstance(item, dict):
            for uri, ts, extra in iter_media_from_obj(item, None):
                out.append(RawMedia(uri, ts, tag, extra))
    cover = data.get("cover_photo")
    if isinstance(cover, dict):
        for uri, ts, extra in iter_media_from_obj(cover, None):
            out.append(RawMedia(uri, ts, tag, extra))
    return out


def collect_from_your_videos(json_path: Path) -> list[RawMedia]:
    """Collect from ``posts/your_videos.json``.

    The list lives under either ``videos_v2`` (newer dumps) or
    ``videos`` (older). We try both.
    """
    data = _safe_load(json_path)
    if not isinstance(data, dict):
        return []
    out: list[RawMedia] = []
    for key in ("videos_v2", "videos"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                for uri, ts, extra in iter_media_from_obj(item, None):
                    out.append(RawMedia(uri, ts, "your_videos", extra))
    return out


def collect_from_tagged_in(json_path: Path) -> list[RawMedia]:
    """Collect from ``activity_you're_tagged_in/*.json``.

    In practice these files reference facebook.com URLs only, so
    nothing local gets found — but we scan anyway in case a future
    export shape includes local files.
    """
    data = _safe_load(json_path)
    if not isinstance(data, list):
        return []
    out: list[RawMedia] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("timestamp")
        fallback = normalize_timestamp(ts) if ts else None
        for m in entry.get("media") or []:
            if isinstance(m, dict):
                for uri, t, extra in iter_media_from_obj(m, fallback):
                    out.append(RawMedia(uri, t, "tagged_in", extra))
    return out


# ---- User-name auto-detection ----------------------------------------------


# Common locations of the profile JSON across Facebook export versions.
_PROFILE_PATHS: tuple[tuple[str, ...], ...] = (
    ("personal_information", "profile_information", "profile_information.json"),
    ("profile_information", "profile_information.json"),
)


def _profile_name(source_root: Path) -> str | None:
    """Look for a profile_information.json in the dump and pull the user's
    full name out of it. Returns ``None`` if neither file is present or
    if the expected fields aren't there.

    Schema seen in full dumps::

        {"profile_v2": {"name": {"full_name": "...", ...}, ...}}
    """
    for parts in _PROFILE_PATHS:
        p = source_root.joinpath(*parts)
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        name = (
            ((data.get("profile_v2") or {}).get("name") or {}).get("full_name")
            or (data.get("profile") or {}).get("name", {}).get("full_name")
        )
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _most_frequent_sender(source_root: Path) -> str | None:
    """Walk every message JSON and return the most prolific ``sender_name``.

    In your own export YOU are typically the most-sending participant
    by a wide margin (typically 2–5×), because you appear in every
    thread while each other participant only appears in their own.

    The intersection-of-participants approach is less reliable: a few
    threads always lack the user in their participants list (group
    chats where participant info was truncated, system messages, etc.),
    causing the intersection to collapse to empty.
    """
    activity = source_root / "your_facebook_activity"
    msg_root = activity / "messages"
    counts: Counter[str] = Counter()

    for sub in (
        "inbox", "message_requests", "filtered_threads",
        "archived_threads", "e2ee_cutover",
    ):
        base = msg_root / sub
        if not base.exists():
            continue
        for jp in base.rglob("message_*.json"):
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for msg in data.get("messages") or []:
                if isinstance(msg, dict):
                    sn = msg.get("sender_name")
                    if sn:
                        counts[sn] += 1

    # Also count group messages.
    gm = activity / "groups" / "your_group_messages"
    if gm.exists():
        for jp in gm.glob("*.json"):
            try:
                data = json.loads(jp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for msg in data.get("messages") or []:
                if isinstance(msg, dict):
                    sn = msg.get("sender_name")
                    if sn:
                        counts[sn] += 1

    if not counts:
        return None
    name, _ = counts.most_common(1)[0]
    return name


def detect_user_name(source_root: Path) -> str | None:
    """Best-effort: figure out the export owner's name from the dump.

    Tries two strategies in order:

    1. Read ``personal_information/profile_information/profile_information.json``
       (only present in full dumps, not the ``your_facebook_activity``
       subset). This is the canonical name Facebook had for you.
    2. Pick the most-frequent ``sender_name`` across every message and
       group-message JSON. In practice this is correct: the export
       owner sends 2–5× more total messages than any single contact.

    Returns ``None`` only when both strategies come up empty (e.g. a
    dump with no messages and no profile info).
    """
    return _profile_name(source_root) or _most_frequent_sender(source_root)


# ---- Resolution / top-level walk -------------------------------------------


# Extensions we try if the URI in JSON points to a missing file. Facebook
# occasionally rewrites a saved .jpg to .png (or vice versa) without
# updating the manifest, so we probe a small set before giving up.
_ALT_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4")


def _resolve_candidate(source_root: Path, uri: str) -> Path | None:
    """Resolve a JSON URI to an existing on-disk path, probing common
    alternate extensions if the literal path is missing."""
    candidate = (source_root / uri).resolve()
    if candidate.exists():
        return candidate
    for alt in _ALT_EXTS:
        c = candidate.with_suffix(alt)
        if c.exists():
            return c
    return None


def gather_all(source_root: Path, user_name: str) -> list[MediaRef]:
    """Walk every relevant JSON file in the dump and return the
    distinct-by-source-path media list.

    Two phases:

    1. Run every ``collect_from_*`` against its corresponding JSON files.
       The output is a flat list of :class:`RawMedia` tuples that may
       contain duplicates (same URI referenced from multiple places) and
       unresolvable URIs (the dump may be partial).
    2. Resolve each URI to a real file, classify by extension, and
       de-duplicate by source path — keeping the earliest timestamp
       and merging any extra exif tags (without clobbering existing
       ones, so the first metadata wins).
    """
    activity = source_root / "your_facebook_activity"
    raw: list[RawMedia] = []

    # --- Messages (sender-filtered) ---
    msg_root = activity / "messages"
    for sub in (
        "inbox", "message_requests", "filtered_threads",
        "archived_threads", "e2ee_cutover",
    ):
        base = msg_root / sub
        if base.exists():
            for jp in base.rglob("message_*.json"):
                raw.extend(collect_from_message_thread(jp, user_name))

    # --- Groups (sender-filtered, same shape as message threads) ---
    group_msgs = activity / "groups" / "your_group_messages"
    if group_msgs.exists():
        for jp in group_msgs.glob("*.json"):
            raw.extend(collect_from_message_thread(jp, user_name))

    # --- Posts you uploaded ---
    posts = activity / "posts"
    if posts.exists():
        p = posts / "your_uncategorized_photos.json"
        if p.exists():
            raw.extend(collect_from_posts_uncategorized(p))

        for p in posts.glob("your_posts__check_ins__photos_and_videos_*.json"):
            raw.extend(collect_from_posts_timeline(p))

        album_dir = posts / "album"
        if album_dir.exists():
            for p in album_dir.glob("*.json"):
                raw.extend(collect_from_album(p))

        p = posts / "your_videos.json"
        if p.exists():
            raw.extend(collect_from_your_videos(p))

        p = posts / "birthday_media.json"
        if p.exists():
            raw.extend(collect_from_posts_timeline(p))

    # --- Tagged-in (URL-only in practice) ---
    tagged_dir = activity / "activity_you're_tagged_in"
    if tagged_dir.exists():
        for p in tagged_dir.glob("*.json"):
            raw.extend(collect_from_tagged_in(p))

    # --- Resolve + classify + dedupe-by-path ---
    seen: dict[Path, MediaRef] = {}
    missing = 0
    for r in raw:
        candidate = _resolve_candidate(source_root, r.uri)
        if candidate is None:
            missing += 1
            continue
        kind = classify(candidate)
        if kind is None:
            continue
        existing = seen.get(candidate)
        if existing is None:
            seen[candidate] = MediaRef(
                candidate, r.timestamp, kind, r.origin, dict(r.extra_exif)
            )
        else:
            # Keep the earliest timestamp + origin; merge new tags
            # without overwriting tags we've already collected.
            if r.timestamp < existing.timestamp:
                existing.timestamp = r.timestamp
                existing.origin = r.origin
            for k, v in r.extra_exif.items():
                existing.extra_exif.setdefault(k, v)

    if missing:
        print(
            f"  note: {missing} referenced media files not present "
            f"locally (expected for a partial dump)",
            file=sys.stderr,
        )
    return list(seen.values())
