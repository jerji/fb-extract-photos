"""Command-line entry point and the top-level orchestration.

The four phases are:

1. **Scan** — walk every relevant JSON file and resolve URIs to on-disk
   files. (:mod:`scanners`)
2. **Hash** — compute a perceptual hash (images) or sha256 (videos/gifs)
   for each file in parallel, with a persistent cache. (:mod:`hashing`)
3. **Dedupe + resume** — collapse refs sharing a key, then drop any
   keys already recorded in the manifest. (:mod:`hashing`, :mod:`output`)
4. **Copy + EXIF** — copy each unique file to ``YYYY/MM/`` and write
   EXIF via a pool of long-lived exiftool daemons. (:mod:`exif`)

``activity_you're_tagged_in/`` is intentionally not scanned: in every
export shape I've seen, those entries are facebook.com URLs with no
local media — scanning produces nothing and just wastes IO.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from queue import Queue

from . import __doc__ as PACKAGE_DOC
from . import __version__
from .exif import ExifToolDaemon, build_exif_args
from .hashing import dedupe, hash_refs
from .output import load_manifest_keys, safe_dest
from .scanners import detect_user_name, gather_all
from .types import CACHE_NAME, MANIFEST_HEADERS, MANIFEST_NAME, MediaRef


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse parser and parse ``argv`` (or ``sys.argv``)."""
    here = Path.cwd()
    default_workers = os.cpu_count() or 4
    ap = argparse.ArgumentParser(
        prog="fb-extract-photos",
        description=PACKAGE_DOC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    ap.add_argument("--source", type=Path, default=here / "output",
                    help="Facebook dump root (default: ./output)")
    ap.add_argument("--output", type=Path, default=here / "photos",
                    help="Destination photo folder (default: ./photos)")
    ap.add_argument("--user", default=None,
                    help="Your sender_name in messages JSON. "
                         "Auto-detected from profile_information.json or "
                         "the most-frequent sender if omitted.")
    ap.add_argument("--exiftool", default="exiftool",
                    help="Path to exiftool binary (default: PATH lookup)")
    ap.add_argument("--phash-size", type=int, default=8,
                    help="Perceptual hash size; larger = stricter "
                         "(default: 8)")
    ap.add_argument("--workers", type=int, default=default_workers,
                    help="Parallel workers for hashing + exiftool "
                         f"(default: {default_workers})")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="Copy every referenced file, no dedupe")
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore existing _manifest.csv; process everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't copy or modify anything; just print")
    return ap.parse_args(argv)


def _print_scan_summary(refs: list[MediaRef]) -> None:
    """Pretty-print the scan stats (origin breakdown, kinds, GPS count)."""
    by_origin: dict[str, int] = defaultdict(int)
    by_kind: dict[str, int] = defaultdict(int)
    with_gps = 0
    for r in refs:
        # collapse e.g. "msg:<thread>" → "msg" so the breakdown is readable
        by_origin[r.origin.split(":", 1)[0]] += 1
        by_kind[r.kind] += 1
        if "GPSLatitude" in r.extra_exif:
            with_gps += 1
    print(f"  found {len(refs)} unique source files "
          f"({with_gps} with GPS)")
    for k, v in sorted(by_origin.items()):
        print(f"    {k:>14}: {v}")
    for k, v in sorted(by_kind.items()):
        print(f"    {k:>14}: {v}")


def _run_copy_phase(
    unique: list[tuple[str, MediaRef]],
    output: Path,
    exiftool: str,
    workers: int,
    manifest_path: Path,
) -> tuple[int, int]:
    """Copy every (key, ref) and write its EXIF in parallel.

    Returns ``(copied, errors)``.

    Concurrency model
    -----------------
    * ``workers`` long-lived exiftool daemons are spawned upfront and
      checked out from a thread-safe :class:`Queue`. Each thread holds
      one daemon for the duration of a single ``run()`` call.
    * A short ``dest_lock`` serialises :func:`safe_dest` + ``.touch()``
      so two threads can't race for the same destination filename.
    * The manifest is appended-to from inside another lock; the file is
      flushed on every row so a Ctrl-C leaves a valid resume state.
    """
    daemon_pool: Queue[ExifToolDaemon] = Queue()
    for _ in range(workers):
        daemon_pool.put(ExifToolDaemon(exiftool))

    # Open in append mode so resume runs extend the manifest rather than
    # truncate it. Header only on first creation.
    write_header = not manifest_path.exists()
    mf = open(manifest_path, "a", newline="", encoding="utf-8")
    mf_writer = csv.writer(mf)
    mf_lock = threading.Lock()
    if write_header:
        mf_writer.writerow(MANIFEST_HEADERS)
        mf.flush()

    dest_lock = threading.Lock()
    counter_lock = threading.Lock()
    copied = 0
    errors = 0

    def worker(item: tuple[str, MediaRef]) -> tuple[str, MediaRef, str]:
        """Process one (key, ref): pick dest → copy → exif → mtime → log."""
        key, ref = item
        # Reserve the destination slot under the lock so concurrent
        # workers see the file and pick a different name.
        with dest_lock:
            dest = safe_dest(output, ref)
            dest.touch()
        try:
            shutil.copy2(ref.source, dest)
        except OSError as e:
            return ("err", ref, f"copy: {e}")

        # Audio + generic files get no EXIF (build_exif_args returns
        # None). Skip the daemon round-trip entirely; the mtime set
        # below is the only timestamp metadata they carry.
        exif_args = build_exif_args(ref, dest)
        if exif_args is not None:
            daemon = daemon_pool.get()
            try:
                daemon.run(exif_args)
            finally:
                daemon_pool.put(daemon)

        try:
            os.utime(dest, (ref.timestamp, ref.timestamp))
        except OSError:
            pass

        with mf_lock:
            mf_writer.writerow([
                key, ref.timestamp, ref.kind, ref.origin,
                str(ref.source), str(dest),
            ])
            mf.flush()
        return ("ok", ref, str(dest))

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, result in enumerate(ex.map(worker, unique), 1):
                with counter_lock:
                    if result[0] == "ok":
                        copied += 1
                    else:
                        errors += 1
                        print(
                            f"  ! {result[1].source}: {result[2]}",
                            file=sys.stderr,
                        )
                if i % 200 == 0:
                    print(f"  processed {i}/{len(unique)}")
    finally:
        while not daemon_pool.empty():
            daemon_pool.get().close()
        mf.close()

    return copied, errors


def main(argv: list[str] | None = None) -> int:
    """Run the full pipeline; return a shell-style exit code.

    ``0`` on clean completion, ``1`` on a pre-flight failure (bad
    paths), ``2`` if some files errored during copy/EXIF.
    """
    # Line-buffer stdout so progress shows promptly through pipes
    # (``tee``, ``tail -f``, etc.) — otherwise Python's default block
    # buffering hides everything until the script exits.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    args = _parse_args(argv)

    source = args.source.resolve()
    output = args.output.resolve()
    if not (source / "your_facebook_activity").exists():
        print(
            f"error: {source}/your_facebook_activity not found",
            file=sys.stderr,
        )
        return 1
    if (
        shutil.which(args.exiftool) is None
        and not Path(args.exiftool).exists()
    ):
        print(
            f"error: exiftool not found at '{args.exiftool}'",
            file=sys.stderr,
        )
        return 1

    user = args.user
    user_source = "from --user"
    if user is None:
        user = detect_user_name(source)
        user_source = "auto-detected"
        if user is None:
            print(
                "error: could not auto-detect your name from the dump. "
                "Pass --user 'Your Full Name' explicitly.",
                file=sys.stderr,
            )
            return 1

    print(f"Source:   {source}")
    print(f"Output:   {output}")
    print(f"User:     {user}  ({user_source})")
    print(f"Workers:  {args.workers}")
    print(f"Dry-run:  {args.dry_run}")
    print()

    # --- Phase 1: scan ---
    print("Step 1/4: scanning JSON manifests...")
    refs = gather_all(source, user)
    _print_scan_summary(refs)

    if not refs:
        print("Nothing to do.")
        return 0

    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    cache_path = output / CACHE_NAME
    manifest_path = output / MANIFEST_NAME

    # --- Phase 2 + 3: hash + dedupe ---
    if args.no_dedupe:
        # Each ref gets a synthetic per-path "key" so the rest of the
        # pipeline (resume tracking, manifest rows) works uniformly.
        unique: list[tuple[str, MediaRef]] = [
            (f"path:{r.source}", r) for r in refs
        ]
        print("\nStep 2/4: dedupe skipped (--no-dedupe).")
        print("\nStep 3/4: -- skipped --")
    else:
        print(
            f"\nStep 2/4: hashing with {args.workers} workers "
            f"(phash for images, sha256 otherwise)..."
        )
        source_to_key = hash_refs(
            refs, args.phash_size, args.workers, cache_path
        )
        print("\nStep 3/4: deduping...")
        by_key = dedupe(refs, source_to_key)
        print(
            f"  {len(refs)} -> {len(by_key)} after dedupe "
            f"({len(refs) - len(by_key)} duplicates collapsed)"
        )
        unique = list(by_key.items())

    # Resume: drop anything already recorded.
    if not args.no_resume:
        done = load_manifest_keys(manifest_path)
        if done:
            before = len(unique)
            unique = [(k, r) for k, r in unique if k not in done]
            print(
                f"  resume: {before - len(unique)} already in manifest, "
                f"{len(unique)} new"
            )

    if args.dry_run:
        print("\nStep 4/4: (dry-run) would copy:")
        for _, r in unique[:10]:
            dt = datetime.fromtimestamp(r.timestamp)
            gps = " +GPS" if "GPSLatitude" in r.extra_exif else ""
            print(
                f"  [dry] {r.source.name:>40} -> "
                f"{dt:%Y}/{dt:%m}/  ({r.kind}, {r.origin}){gps}"
            )
        if len(unique) > 10:
            print(f"  [dry] ... and {len(unique) - 10} more")
        return 0

    if not unique:
        print("\nNothing new to copy.")
        return 0

    # --- Phase 4: copy + EXIF ---
    print(
        f"\nStep 4/4: copying + writing EXIF "
        f"({args.workers} exiftool daemons)..."
    )
    copied, errors = _run_copy_phase(
        unique, output, args.exiftool, args.workers, manifest_path
    )

    print()
    print(f"Done. Copied {copied}; errors {errors}.")
    print(f"Manifest: {manifest_path}")
    return 0 if errors == 0 else 2
