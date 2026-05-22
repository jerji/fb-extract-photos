#!/usr/bin/env python3
"""
Extract photos/videos/GIFs that *you* sent or uploaded from a Facebook data
dump, restore creation-time EXIF metadata (plus GPS/camera tags when
Facebook recorded them), dedupe (perceptual hash for images, sha256 for
videos/gifs), and lay them out under photos/YYYY/MM/.

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

Hash cache (.hash_cache.json) and run manifest (_manifest.csv) are written
under --output so re-runs are incremental: already-processed files are
skipped, already-hashed files keep their cached key.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Queue

from PIL import Image, UnidentifiedImageError
import imagehash


PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
GIF_EXT = {".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".webm", ".gifv"}
ALL_EXT = PHOTO_EXT | GIF_EXT | VIDEO_EXT

MANIFEST_NAME = "_manifest.csv"
CACHE_NAME = ".hash_cache.json"
MANIFEST_HEADERS = [
    "dedupe_key", "timestamp", "kind", "origin", "source", "dest"
]


# ---------------------------------------------------------------- data shapes


@dataclass
class MediaRef:
    source: Path
    timestamp: int
    kind: str                # "photo" | "gif" | "video"
    origin: str              # short tag describing where it came from
    extra_exif: dict = field(default_factory=dict)


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
    s = str(int(ts))
    return int(s[:10]) if len(s) > 10 else int(s)


# ---------------------------------------------------------------- JSON scrape


def extract_metadata(obj: dict) -> tuple[dict, int | None]:
    """Pull GPS/camera tags and (more accurate) taken_timestamp from
    media_metadata.{photo,video}_metadata.exif_data, if present."""
    md = obj.get("media_metadata") or {}
    pm = md.get("photo_metadata") or md.get("video_metadata") or {}
    tags: dict[str, object] = {}
    taken: int | None = None
    for e in pm.get("exif_data") or []:
        if not isinstance(e, dict):
            continue
        tt = e.get("taken_timestamp")
        if tt:
            taken = normalize_timestamp(tt)
        lat = e.get("latitude")
        lon = e.get("longitude")
        if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and not (lat == 0 and lon == 0)):
            tags["GPSLatitude"] = abs(lat)
            tags["GPSLatitudeRef"] = "N" if lat >= 0 else "S"
            tags["GPSLongitude"] = abs(lon)
            tags["GPSLongitudeRef"] = "E" if lon >= 0 else "W"
        for src, dst in (
            ("camera_make", "Make"),
            ("camera_model", "Model"),
            ("iso", "ISO"),
            ("focal_length", "FocalLength"),
            ("f_stop", "FNumber"),
            ("exposure_time", "ExposureTime"),
            ("orientation", "Orientation"),
        ):
            v = e.get(src)
            if v not in (None, ""):
                tags[dst] = v
        ip = e.get("upload_ip")
        if ip and "UserComment" not in tags:
            tags["UserComment"] = f"Facebook upload IP: {ip}"
    return tags, taken


def iter_media_from_obj(obj: dict, fallback_ts: int | None) -> list[tuple[str, int, dict]]:
    """Pull (uri, timestamp, extra_exif) out of a media-shaped dict."""
    uri = obj.get("uri")
    if not isinstance(uri, str) or uri.startswith(("http://", "https://")):
        return []
    extra, taken = extract_metadata(obj)
    ts = taken or obj.get("creation_timestamp") or fallback_ts
    if not ts:
        return []
    return [(uri, normalize_timestamp(ts), extra)]


def collect_from_message_thread(json_path: Path, user_name: str) -> list[tuple[str, int, str, dict]]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! skip {json_path}: {e}", file=sys.stderr)
        return []

    results = []
    origin = json_path.parent.name
    for m in data.get("messages") or []:
        if m.get("sender_name") != user_name:
            continue
        ts_ms = m.get("timestamp_ms")
        fallback = normalize_timestamp(ts_ms) if ts_ms else None
        for key in ("photos", "videos", "gifs"):
            for item in m.get(key) or []:
                if not isinstance(item, dict):
                    continue
                for uri, ts, extra in iter_media_from_obj(item, fallback):
                    results.append((uri, ts, f"msg:{origin}", extra))
    return results


def collect_from_posts_uncategorized(json_path: Path) -> list[tuple[str, int, str, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for item in data.get("other_photos_v2") or []:
        out.extend(
            (u, t, "uncategorized", x)
            for u, t, x in iter_media_from_obj(item, None)
        )
    return out


def collect_from_posts_timeline(json_path: Path) -> list[tuple[str, int, str, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for post in data:
        post_ts = post.get("timestamp")
        fallback = normalize_timestamp(post_ts) if post_ts else None
        for att in post.get("attachments") or []:
            for entry in att.get("data") or []:
                media = entry.get("media")
                if isinstance(media, dict):
                    out.extend(
                        (u, t, "post", x)
                        for u, t, x in iter_media_from_obj(media, fallback)
                    )
    return out


def collect_from_album(json_path: Path) -> list[tuple[str, int, str, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    name = (data.get("name") or json_path.stem).strip() or "album"
    tag = f"album:{name}"
    out = []
    for item in data.get("photos") or []:
        out.extend((u, t, tag, x) for u, t, x in iter_media_from_obj(item, None))
    cover = data.get("cover_photo")
    if isinstance(cover, dict):
        out.extend((u, t, tag, x) for u, t, x in iter_media_from_obj(cover, None))
    return out


def collect_from_your_videos(json_path: Path) -> list[tuple[str, int, str, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for key in ("videos_v2", "videos"):
        for item in data.get(key) or []:
            if isinstance(item, dict):
                out.extend(
                    (u, t, "your_videos", x)
                    for u, t, x in iter_media_from_obj(item, None)
                )
    return out


def collect_from_tagged_in(json_path: Path) -> list[tuple[str, int, str, dict]]:
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        ts = entry.get("timestamp")
        fallback = normalize_timestamp(ts) if ts else None
        for m in entry.get("media") or []:
            if isinstance(m, dict):
                out.extend(
                    (u, t, "tagged_in", x)
                    for u, t, x in iter_media_from_obj(m, fallback)
                )
    return out


def gather_all(source_root: Path, user_name: str) -> list[MediaRef]:
    activity = source_root / "your_facebook_activity"
    raw: list[tuple[str, int, str, dict]] = []

    msg_root = activity / "messages"
    for sub in ("inbox", "message_requests", "filtered_threads",
                "archived_threads", "e2ee_cutover"):
        base = msg_root / sub
        if not base.exists():
            continue
        for json_path in base.rglob("message_*.json"):
            raw.extend(collect_from_message_thread(json_path, user_name))

    group_msgs = activity / "groups" / "your_group_messages"
    if group_msgs.exists():
        for json_path in group_msgs.glob("*.json"):
            raw.extend(collect_from_message_thread(json_path, user_name))

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

    tagged_dir = activity / "activity_you're_tagged_in"
    if tagged_dir.exists():
        for p in tagged_dir.glob("*.json"):
            raw.extend(collect_from_tagged_in(p))

    # Resolve URIs to absolute files, dedupe by path keeping earliest ts &
    # merging metadata.
    seen: dict[Path, MediaRef] = {}
    missing = 0
    for uri, ts, origin, extra in raw:
        candidate = (source_root / uri).resolve()
        if not candidate.exists():
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
        existing = seen.get(candidate)
        if existing is None:
            seen[candidate] = MediaRef(candidate, ts, kind, origin, dict(extra))
        else:
            if ts < existing.timestamp:
                existing.timestamp = ts
                existing.origin = origin
            # merge extra tags (don't clobber)
            for k, v in extra.items():
                existing.extra_exif.setdefault(k, v)
    if missing:
        print(f"  note: {missing} referenced media files not present locally "
              f"(expected for a partial dump)", file=sys.stderr)
    return list(seen.values())


# ---------------------------------------------------------------- hashing


def _hash_one(args: tuple[str, str, int]) -> tuple[str, str]:
    """Top-level so ProcessPoolExecutor can pickle it."""
    source, kind, phash_size = args
    path = Path(source)
    if kind == "photo":
        try:
            with Image.open(path) as im:
                return source, "phash:" + str(imagehash.phash(im.convert("RGB"),
                                                              hash_size=phash_size))
        except (UnidentifiedImageError, OSError,
                Image.DecompressionBombError, ValueError):
            pass
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return source, "sha256:" + h.hexdigest()


def hash_refs(refs: list[MediaRef], phash_size: int, workers: int,
              cache_path: Path) -> dict[str, str]:
    """Compute a dedup key for each ref's source path. Reads/writes a
    persistent cache so unchanged files are not re-hashed on next run."""
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            cache = {}

    source_to_key: dict[str, str] = {}
    todo: list[tuple[str, str, int]] = []
    for r in refs:
        src = str(r.source)
        try:
            st = r.source.stat()
        except OSError:
            continue
        entry = cache.get(src)
        if (entry
                and entry.get("mtime") == st.st_mtime
                and entry.get("size") == st.st_size
                and entry.get("phash_size", phash_size) == phash_size):
            source_to_key[src] = entry["key"]
        else:
            todo.append((src, r.kind, phash_size))

    if cache:
        print(f"  cache: {len(source_to_key)}/{len(refs)} hits, "
              f"{len(todo)} to hash")
    if todo:
        # chunksize keeps the worker queue full without overwhelming pickling
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
                    pass
                done += 1
                if done % 500 == 0:
                    print(f"  hashed {done}/{len(todo)}")
        try:
            cache_path.write_text(json.dumps(cache))
        except OSError as e:
            print(f"  ! could not save hash cache: {e}", file=sys.stderr)

    return source_to_key


def dedupe(refs: list[MediaRef],
           source_to_key: dict[str, str]) -> dict[str, MediaRef]:
    by_key: dict[str, MediaRef] = {}
    for r in refs:
        key = source_to_key.get(str(r.source))
        if key is None:
            continue
        cur = by_key.get(key)
        if cur is None or r.timestamp < cur.timestamp:
            by_key[key] = r
    return by_key


# ---------------------------------------------------------------- exiftool


class ExifToolDaemon:
    """Persistent `exiftool -stay_open` process; ~10x faster than per-file
    spawning for batch work.

    Notes:
      * `-q` is intentionally NOT passed: exiftool suppresses the `{ready}`
        sentinel when `-q` is set, deadlocking our reader. We just discard
        the harmless "N image files updated" chatter instead.
      * stderr is merged into stdout so warnings don't fill an unread pipe
        and block exiftool.
    """

    SENTINEL = "{ready}"

    def __init__(self, exe: str):
        self.proc = subprocess.Popen(
            [
                exe,
                "-stay_open", "True",
                "-@", "-",
                "-common_args",
                "-overwrite_original", "-P", "-m",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )

    def run(self, args: list[str]) -> str:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write("\n".join(args) + "\n-execute\n")
        self.proc.stdin.flush()
        out = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.strip() == self.SENTINEL:
                break
            out.append(line)
        return "".join(out)

    def close(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.write("-stay_open\nFalse\n")
                self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def build_exif_args(ref: MediaRef, dest: Path) -> list[str]:
    stamp = datetime.fromtimestamp(ref.timestamp).strftime("%Y:%m:%d %H:%M:%S")
    args: list[str] = []
    if ref.kind == "video":
        args += ["-api", "quicktime=1"]
        for tag in ("CreationDate", "CreateDate", "ModifyDate",
                    "TrackCreateDate", "TrackModifyDate",
                    "MediaCreateDate", "MediaModifyDate"):
            args.append(f"-{tag}={stamp}")
    args += [
        f"-DateTimeOriginal={stamp}",
        f"-CreateDate={stamp}",
        f"-ModifyDate={stamp}",
        f"-FileModifyDate={stamp}",
        f"-FileCreateDate={stamp}",
    ]
    for tag, val in ref.extra_exif.items():
        args.append(f"-{tag}={val}")
    args.append(str(dest))
    return args


# ---------------------------------------------------------------- output


def safe_dest(out_root: Path, ref: MediaRef) -> Path:
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
    if not manifest_path.exists():
        return set()
    done = set()
    try:
        with open(manifest_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = row.get("dedupe_key")
                if k:
                    done.add(k)
    except OSError:
        pass
    return done


# ---------------------------------------------------------------- main


def main() -> int:
    # Force line buffering on stdout so progress is visible when piped
    # through `tee`, `tail -f`, etc.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source", type=Path, default=here / "output",
                    help="Facebook dump root (default: ./output)")
    ap.add_argument("--output", type=Path, default=here / "photos",
                    help="Destination photo folder (default: ./photos)")
    ap.add_argument("--user", default="Angel Ouellet",
                    help='Your sender_name in messages JSON '
                         '(default: "Angel Ouellet")')
    ap.add_argument("--exiftool", default="exiftool",
                    help="Path to exiftool binary (default: PATH lookup)")
    ap.add_argument("--phash-size", type=int, default=8,
                    help="Perceptual hash size; larger = stricter (default: 8)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                    help="Parallel workers for hashing + exiftool "
                         f"(default: {os.cpu_count() or 4})")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="Copy every referenced file, no dedupe")
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore existing _manifest.csv; process everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't copy or modify anything; just print")
    args = ap.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    if not (source / "your_facebook_activity").exists():
        print(f"error: {source}/your_facebook_activity not found",
              file=sys.stderr)
        return 1
    if shutil.which(args.exiftool) is None and not Path(args.exiftool).exists():
        print(f"error: exiftool not found at '{args.exiftool}'",
              file=sys.stderr)
        return 1

    print(f"Source:   {source}")
    print(f"Output:   {output}")
    print(f"User:     {args.user}")
    print(f"Workers:  {args.workers}")
    print(f"Dry-run:  {args.dry_run}")
    print()

    print("Step 1/4: scanning JSON manifests...")
    refs = gather_all(source, args.user)
    by_origin: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    with_gps = 0
    for r in refs:
        by_origin[r.origin.split(":", 1)[0]] += 1
        by_kind[r.kind] += 1
        if "GPSLatitude" in r.extra_exif:
            with_gps += 1
    print(f"  found {len(refs)} unique source files ({with_gps} with GPS)")
    for k, v in sorted(by_origin.items()):
        print(f"    {k:>14}: {v}")
    for k, v in sorted(by_kind.items()):
        print(f"    {k:>14}: {v}")

    if not refs:
        print("Nothing to do.")
        return 0

    # Prep output paths needed for cache/manifest
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    cache_path = output / CACHE_NAME
    manifest_path = output / MANIFEST_NAME

    if args.no_dedupe:
        unique: list[tuple[str, MediaRef]] = [
            (f"path:{r.source}", r) for r in refs
        ]
        print("\nStep 2/4: dedupe skipped (--no-dedupe).")
        print("\nStep 3/4: -- skipped --")
    else:
        print(f"\nStep 2/4: hashing with {args.workers} workers "
              f"(phash for images, sha256 otherwise)...")
        source_to_key = hash_refs(refs, args.phash_size, args.workers,
                                  cache_path)
        print(f"\nStep 3/4: deduping...")
        by_key = dedupe(refs, source_to_key)
        print(f"  {len(refs)} -> {len(by_key)} after dedupe "
              f"({len(refs) - len(by_key)} duplicates collapsed)")
        unique = list(by_key.items())

    # Resume: skip already-processed dedupe keys
    if not args.no_resume:
        done = load_manifest_keys(manifest_path)
        if done:
            before = len(unique)
            unique = [(k, r) for k, r in unique if k not in done]
            print(f"  resume: {before - len(unique)} already in manifest, "
                  f"{len(unique)} new")

    if args.dry_run:
        print("\nStep 4/4: (dry-run) would copy:")
        for k, r in unique[:10]:
            dt = datetime.fromtimestamp(r.timestamp)
            gps = " +GPS" if "GPSLatitude" in r.extra_exif else ""
            print(f"  [dry] {r.source.name:>40} -> "
                  f"{dt:%Y}/{dt:%m}/  ({r.kind}, {r.origin}){gps}")
        if len(unique) > 10:
            print(f"  [dry] ... and {len(unique) - 10} more")
        return 0

    if not unique:
        print("\nNothing new to copy.")
        return 0

    print(f"\nStep 4/4: copying + writing EXIF "
          f"({args.workers} exiftool daemons)...")

    # Per-thread exiftool daemons via a queue
    daemon_pool: Queue[ExifToolDaemon] = Queue()
    for _ in range(args.workers):
        daemon_pool.put(ExifToolDaemon(args.exiftool))

    # Manifest: append mode; write header if new
    write_header = not manifest_path.exists()
    mf = open(manifest_path, "a", newline="", encoding="utf-8")
    mf_writer = csv.writer(mf)
    mf_lock = threading.Lock()
    if write_header:
        mf_writer.writerow(MANIFEST_HEADERS)
        mf.flush()

    # Single lock around safe_dest because it computes uniqueness via
    # filesystem existence and we don't want two threads picking the same
    # destination name.
    dest_lock = threading.Lock()

    copied = 0
    errors = 0
    counter_lock = threading.Lock()

    def worker(item: tuple[str, MediaRef]) -> tuple[str, MediaRef, str]:
        key, ref = item
        with dest_lock:
            dest = safe_dest(output, ref)
            # Reserve the path immediately so a concurrent worker sees it.
            dest.touch()
        try:
            shutil.copy2(ref.source, dest)
        except OSError as e:
            return ("err", ref, f"copy: {e}")
        d = daemon_pool.get()
        try:
            d.run(build_exif_args(ref, dest))
        finally:
            daemon_pool.put(d)
        try:
            os.utime(dest, (ref.timestamp, ref.timestamp))
        except OSError:
            pass
        with mf_lock:
            mf_writer.writerow([key, ref.timestamp, ref.kind, ref.origin,
                                str(ref.source), str(dest)])
            mf.flush()
        return ("ok", ref, str(dest))

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for i, result in enumerate(ex.map(worker, unique), 1):
                with counter_lock:
                    if result[0] == "ok":
                        copied += 1
                    else:
                        errors += 1
                        print(f"  ! {result[1].source}: {result[2]}",
                              file=sys.stderr)
                if i % 200 == 0:
                    print(f"  processed {i}/{len(unique)}")
    finally:
        while not daemon_pool.empty():
            daemon_pool.get().close()
        mf.close()

    print()
    print(f"Done. Copied {copied}; errors {errors}.")
    print(f"Manifest: {manifest_path}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
