#!/usr/bin/env python3
"""
Extract photos/videos/GIFs that *you* sent or uploaded from a Facebook data
dump, restore creation-time EXIF metadata, dedupe (perceptual hash for
images, sha256 for videos/gifs), and lay them out under photos/YYYY/MM/.

Sources scanned (only entries where sender_name == --user, or all entries
for things you uploaded yourself like albums/posts/uncategorized):

  messages/inbox/<thread>/message_*.json
  messages/message_requests/**/message_*.json
  messages/filtered_threads/**/message_*.json
  messages/archived_threads/**/message_*.json
  messages/e2ee_cutover/**/message_*.json
  groups/your_group_messages/*.json
  posts/your_uncategorized_photos.json
  posts/your_posts__check_ins__photos_and_videos_*.json
  posts/album/*.json
  posts/your_videos.json
  posts/birthday_media.json
  activity_you're_tagged_in/*.json  (URL-only; no local files)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
import imagehash


PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".gifv"}
ALL_EXT = PHOTO_EXT | GIF_EXT | VIDEO_EXT


@dataclass
class MediaRef:
    source: Path
    timestamp: int
    kind: str       # "photo" | "gif" | "video"
    origin: str     # short tag describing where it came from

    def ext(self) -> str:
        return self.source.suffix.lower()


def classify(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in GIF_EXT:
        return "gif"
    if ext in VIDEO_EXT:
        return "video"
    return None


def normalize_timestamp(ts) -> int:
    """Facebook uses both seconds and milliseconds; coerce to seconds."""
    s = str(int(ts))
    return int(s[:10]) if len(s) > 10 else int(s)


def iter_media_from_obj(obj: dict, fallback_ts: int | None) -> list[tuple[str, int]]:
    """Pull (uri, timestamp) pairs out of a media-shaped dict."""
    out = []
    uri = obj.get("uri")
    if not uri or not isinstance(uri, str):
        return out
    if uri.startswith("http://") or uri.startswith("https://"):
        return out
    ts = obj.get("creation_timestamp")
    if ts is None or ts == 0:
        ts = fallback_ts
    if ts is None:
        return out
    out.append((uri, normalize_timestamp(ts)))
    return out


def collect_from_message_thread(json_path: Path, user_name: str) -> list[tuple[str, int, str]]:
    """Return list of (uri, ts, origin) for media YOU sent in this thread."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! skip {json_path}: {e}", file=sys.stderr)
        return []

    msgs = data.get("messages") or []
    results = []
    origin = json_path.parent.name
    for m in msgs:
        if m.get("sender_name") != user_name:
            continue
        ts_ms = m.get("timestamp_ms")
        fallback = normalize_timestamp(ts_ms) if ts_ms else None
        for key in ("photos", "videos", "gifs"):
            for item in m.get(key) or []:
                if not isinstance(item, dict):
                    continue
                for uri, ts in iter_media_from_obj(item, fallback):
                    results.append((uri, ts, f"msg:{origin}"))
    return results


def collect_from_posts_uncategorized(json_path: Path) -> list[tuple[str, int, str]]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for item in data.get("other_photos_v2") or []:
        out.extend((u, t, "uncategorized") for u, t in iter_media_from_obj(item, None))
    return out


def collect_from_posts_timeline(json_path: Path) -> list[tuple[str, int, str]]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    if not isinstance(data, list):
        return out
    for post in data:
        post_ts = post.get("timestamp")
        fallback = normalize_timestamp(post_ts) if post_ts else None
        for att in post.get("attachments") or []:
            for entry in att.get("data") or []:
                media = entry.get("media")
                if isinstance(media, dict):
                    out.extend(
                        (u, t, "post") for u, t in iter_media_from_obj(media, fallback)
                    )
    return out


def collect_from_album(json_path: Path) -> list[tuple[str, int, str]]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    name = (data.get("name") or json_path.stem).strip() or "album"
    tag = f"album:{name}"
    for item in data.get("photos") or []:
        out.extend((u, t, tag) for u, t in iter_media_from_obj(item, None))
    cover = data.get("cover_photo")
    if isinstance(cover, dict):
        out.extend((u, t, tag) for u, t in iter_media_from_obj(cover, None))
    return out


def collect_from_your_videos(json_path: Path) -> list[tuple[str, int, str]]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    # Structure: {"videos_v2": [ {uri, creation_timestamp, ...}, ... ]}
    for key in ("videos_v2", "videos"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                out.extend((u, t, "your_videos") for u, t in iter_media_from_obj(item, None))
    return out


def collect_from_tagged_in(json_path: Path) -> list[tuple[str, int, str]]:
    """The tagged-in JSON usually contains only facebook.com URLs, but try."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    if not isinstance(data, list):
        return out
    for entry in data:
        ts = entry.get("timestamp")
        fallback = normalize_timestamp(ts) if ts else None
        for m in entry.get("media") or []:
            if isinstance(m, dict):
                out.extend(
                    (u, t, "tagged_in") for u, t in iter_media_from_obj(m, fallback)
                )
    return out


def gather_all(source_root: Path, user_name: str) -> list[MediaRef]:
    activity = source_root / "your_facebook_activity"
    refs: list[MediaRef] = []
    raw: list[tuple[str, int, str]] = []

    # --- Messages (sender-filtered) ---
    msg_root = activity / "messages"
    for sub in ("inbox", "message_requests", "filtered_threads",
                "archived_threads", "e2ee_cutover"):
        base = msg_root / sub
        if not base.exists():
            continue
        for json_path in base.rglob("message_*.json"):
            raw.extend(collect_from_message_thread(json_path, user_name))

    # --- Groups (sender-filtered) ---
    group_msgs = activity / "groups" / "your_group_messages"
    if group_msgs.exists():
        for json_path in group_msgs.glob("*.json"):
            raw.extend(collect_from_message_thread(json_path, user_name))

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
            raw.extend(collect_from_posts_timeline(p))  # same shape

    # --- Tagged in ---
    tagged_dir = activity / "activity_you're_tagged_in"
    if tagged_dir.exists():
        for p in tagged_dir.glob("*.json"):
            raw.extend(collect_from_tagged_in(p))

    # --- Resolve to absolute paths, classify, build MediaRefs ---
    seen_paths: dict[Path, MediaRef] = {}
    missing = 0
    for uri, ts, origin in raw:
        # uri is relative to source_root (e.g. "your_facebook_activity/...")
        candidate = (source_root / uri).resolve()
        if not candidate.exists():
            # try alternate extensions (the messenger script handles this case)
            for alt in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4"):
                if candidate.with_suffix(alt).exists():
                    candidate = candidate.with_suffix(alt)
                    break
            else:
                missing += 1
                continue
        kind = classify(candidate)
        if kind is None:
            continue
        existing = seen_paths.get(candidate)
        if existing is None or ts < existing.timestamp:
            seen_paths[candidate] = MediaRef(
                source=candidate, timestamp=ts, kind=kind, origin=origin
            )
    if missing:
        print(f"  note: {missing} referenced media files not present locally "
              f"(expected for a partial dump)", file=sys.stderr)
    return list(seen_paths.values())


def compute_dedup_key(ref: MediaRef, phash_size: int) -> str:
    """Perceptual hash for images, sha256 for everything else."""
    if ref.kind == "photo":
        try:
            with Image.open(ref.source) as im:
                im = im.convert("RGB")
                return "phash:" + str(imagehash.phash(im, hash_size=phash_size))
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            print(f"  ! cannot hash {ref.source.name}: {e}; falling back to sha256",
                  file=sys.stderr)
    # videos, gifs, or unreadable images
    h = hashlib.sha256()
    with open(ref.source, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def dedupe(refs: list[MediaRef], phash_size: int) -> list[MediaRef]:
    by_key: dict[str, MediaRef] = {}
    for i, r in enumerate(refs, 1):
        if i % 200 == 0:
            print(f"  hashing {i}/{len(refs)}...", file=sys.stderr)
        key = compute_dedup_key(r, phash_size)
        cur = by_key.get(key)
        if cur is None or r.timestamp < cur.timestamp:
            by_key[key] = r
    return list(by_key.values())


def fmt_exif(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y:%m:%d %H:%M:%S")


def write_exif(exiftool: str, dest: Path, ts: int, kind: str) -> bool:
    stamp = fmt_exif(ts)
    args = [exiftool, "-q", "-overwrite_original", "-P"]
    if kind == "video":
        # QuickTime-style atoms
        args += ["-api", "quicktime=1"]
        for tag in ("CreationDate", "CreateDate", "ModifyDate",
                    "TrackCreateDate", "TrackModifyDate",
                    "MediaCreateDate", "MediaModifyDate"):
            args.append(f"-{tag}={stamp}")
    args += [f"-DateTimeOriginal={stamp}",
             f"-CreateDate={stamp}",
             f"-ModifyDate={stamp}",
             f"-FileModifyDate={stamp}",
             f"-FileCreateDate={stamp}"]
    args.append(str(dest))
    try:
        subprocess.run(args, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode("utf-8", "replace").strip().splitlines()
        # Some formats (.gif, oddball webp) reject certain tags; that's fine
        # so long as some tags landed. Silence the common ones.
        ignorable = ("Not a valid", "minor", "Warning:")
        if msg and not any(any(s in line for s in ignorable) for line in msg):
            print(f"  ! exiftool: {dest.name}: {msg[0]}", file=sys.stderr)
        return False


def safe_dest(out_root: Path, ref: MediaRef) -> Path:
    dt = datetime.fromtimestamp(ref.timestamp)
    folder = out_root / f"{dt.year:04d}" / f"{dt.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    base = ref.source.name
    candidate = folder / base
    n = 1
    while candidate.exists():
        candidate = folder / f"{ref.source.stem}_{n}{ref.source.suffix}"
        n += 1
    return candidate


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=here / "output",
                    help="Facebook dump root (default: ./output)")
    ap.add_argument("--output", type=Path, default=here / "photos",
                    help="Destination photo folder (default: ./photos)")
    ap.add_argument("--user", default="Angel Ouellet",
                    help='Your sender_name as it appears in messages JSON '
                         '(default: "Angel Ouellet")')
    ap.add_argument("--exiftool", default="exiftool",
                    help="Path to exiftool binary (default: PATH lookup)")
    ap.add_argument("--phash-size", type=int, default=8,
                    help="Perceptual hash size; larger = stricter (default: 8)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="Copy every referenced file, no dedupe")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't copy or modify anything; just print what would happen")
    args = ap.parse_args()

    source = args.source.resolve()
    if not (source / "your_facebook_activity").exists():
        print(f"error: {source}/your_facebook_activity not found", file=sys.stderr)
        return 1
    if shutil.which(args.exiftool) is None and not Path(args.exiftool).exists():
        print(f"error: exiftool not found at '{args.exiftool}'", file=sys.stderr)
        return 1

    print(f"Source:   {source}")
    print(f"Output:   {args.output.resolve()}")
    print(f"User:     {args.user}")
    print(f"Dry-run:  {args.dry_run}")
    print()

    print("Step 1/3: scanning JSON manifests...")
    refs = gather_all(source, args.user)
    by_origin: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    for r in refs:
        # collapse "msg:<thread>" into just "msg"
        tag = r.origin.split(":", 1)[0]
        by_origin[tag] += 1
        by_kind[r.kind] += 1
    print(f"  found {len(refs)} unique source files")
    for k, v in sorted(by_origin.items()):
        print(f"    {k:>14}: {v}")
    for k, v in sorted(by_kind.items()):
        print(f"    {k:>14}: {v}")

    if not refs:
        print("Nothing to do.")
        return 0

    if args.no_dedupe:
        unique = refs
        print("\nStep 2/3: dedupe skipped (--no-dedupe).")
    else:
        print("\nStep 2/3: deduplicating (phash for images, sha256 otherwise)...")
        unique = dedupe(refs, args.phash_size)
        print(f"  {len(refs)} -> {len(unique)} after dedupe "
              f"({len(refs) - len(unique)} duplicates collapsed)")

    print(f"\nStep 3/3: copying to {args.output}/YYYY/MM/ and setting EXIF...")
    if args.dry_run:
        for r in unique[:10]:
            dt = datetime.fromtimestamp(r.timestamp)
            print(f"  [dry] {r.source.name:>40} -> "
                  f"{dt:%Y}/{dt:%m}/  ({r.kind}, {r.origin})")
        if len(unique) > 10:
            print(f"  [dry] ... and {len(unique) - 10} more")
        return 0

    args.output.mkdir(parents=True, exist_ok=True)
    copied = 0
    exif_ok = 0
    for i, r in enumerate(unique, 1):
        if i % 100 == 0:
            print(f"  copy {i}/{len(unique)}...")
        dest = safe_dest(args.output, r)
        try:
            shutil.copy2(r.source, dest)
        except OSError as e:
            print(f"  ! copy failed for {r.source}: {e}", file=sys.stderr)
            continue
        copied += 1
        if write_exif(args.exiftool, dest, r.timestamp, r.kind):
            exif_ok += 1
        # set filesystem mtime/atime regardless
        try:
            os.utime(dest, (r.timestamp, r.timestamp))
        except OSError:
            pass

    print()
    print(f"Done. Copied {copied} file(s); EXIF written on {exif_ok}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
