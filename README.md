# fb-extract-photos

Extract the photos, videos, and GIFs that **you** sent or uploaded from a
Facebook data dump, restore creation-time EXIF metadata, and lay them out
under `photos/YYYY/MM/`. Duplicates are collapsed by perceptual hash for
images and sha256 for videos/GIFs (keeping the earliest timestamp).

## Sources scanned

Only entries where `sender_name` matches your user name, plus things you
uploaded yourself (albums, posts, uncategorized):

- `messages/inbox/<thread>/message_*.json`
- `messages/{message_requests,filtered_threads,archived_threads,e2ee_cutover}/**/message_*.json`
- `groups/your_group_messages/*.json`
- `posts/your_uncategorized_photos.json`
- `posts/your_posts__check_ins__photos_and_videos_*.json`
- `posts/album/*.json`
- `posts/your_videos.json`
- `posts/birthday_media.json`

`activity_you're_tagged_in/*.json` is intentionally **not** scanned —
its entries are facebook.com URLs with no local files in any export
shape I've seen.

## Output layout

```
<output>/
  photos/YYYY/MM/   # jpg, png, webp, heic, nef, gif, mp4, mov, webm, wmv, …
  audio/YYYY/MM/    # mp3, aac, m4a, ogg, opus, flac, mid, …
  files/YYYY/MM/    # pdf, docx, xlsx, txt, stl, csv, … (anything else)
  _manifest.csv
  .hash_cache.json
```

Pass `--only-media` to keep just `photos/` (skips audio attachments
and document files).

## Requirements

- Python 3.10+
- [`exiftool`](https://exiftool.org/) on `PATH` (or passed via `--exiftool`)

## Install

### With [`uv`](https://docs.astral.sh/uv/) (no install needed)

Run the latest version straight from GitHub — `uvx` handles the venv and
deps transparently:

```bash
uvx --from git+https://github.com/jerji/fb-extract-photos \
    fb-extract-photos --source ./output --output ./photos
```

Or install it as a persistent tool on your `PATH`:

```bash
uv tool install git+https://github.com/jerji/fb-extract-photos
fb-extract-photos --help
```

### With `pip`

From a clone:

```bash
git clone https://github.com/jerji/fb-extract-photos.git
cd fb-extract-photos
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Or directly from GitHub:

```bash
pip install git+https://github.com/jerji/fb-extract-photos.git
```

## Usage

```bash
fb-extract-photos --source ./output --output ./photos
# or:
python -m fb_extract_photos --source ./output --output ./photos
```

## Layout

```
fb_extract_photos/
  cli.py        # arg parsing + the 4-phase pipeline
  scanners.py   # JSON manifest parsers + metadata extraction
  hashing.py    # phash / sha256 dedup with persistent cache
  exif.py       # ExifToolDaemon (-stay_open) + EXIF arg builder
  output.py     # destination paths + manifest reader
  types.py      # MediaRef, RawMedia, type aliases, constants
```

Common flags:

| Flag             | Default          | Notes                                            |
| ---------------- | ---------------- | ------------------------------------------------ |
| `--source`       | `./output`       | Facebook dump root                               |
| `--output`       | `./photos`       | Destination folder                               |
| `--user`         | auto-detected    | Your `sender_name` in messages (see below)       |
| `--exiftool`     | `exiftool`       | Path to the exiftool binary                      |
| `--phash-size`   | `8`              | Perceptual hash size; larger = stricter dedupe   |
| `--workers`      | `os.cpu_count()` | Parallel workers for hashing + exiftool          |
| `--only-media`   | off              | Skip audio/file attachments; photos+videos only  |
| `--no-dedupe`    | off              | Copy every reference, no dedupe                  |
| `--no-resume`    | off              | Ignore `_manifest.csv` and process everything    |
| `--dry-run`      | off              | Don't copy or modify anything                    |

### User name auto-detection

You don't normally need to pass `--user`. The tool figures out who you
are by:

1. Reading `personal_information/profile_information/profile_information.json`
   from a full dump (the canonical source).
2. Otherwise picking the most-frequent `sender_name` across every
   message and group-message JSON. In your own export this is always
   you by a wide margin (you appear in every thread; nobody else does).

Pass `--user 'Your Full Name'` to override.

### Incremental re-runs

Two state files are written next to your output:

- `photos/_manifest.csv` — one row per copied file (`dedupe_key`, `timestamp`,
  `kind`, `origin`, `source`, `dest`). On the next run, any `dedupe_key`
  already in this file is skipped.
- `photos/.hash_cache.json` — keyed by source path → (`mtime`, `size`, `key`).
  Unchanged source files are not re-hashed.

So a re-run after extending the dump only does work for the new files.

## Output

```
photos/
  2017/
    01/
      10154829988729277.jpg
  2023/
    09/
      858694815692675.mp4
```

Each file gets:

- EXIF `DateTimeOriginal`, `CreateDate`, `ModifyDate` (and QuickTime
  `CreationDate`/`MediaCreateDate` for videos)
- Filesystem mtime/atime aligned to the same timestamp

## License

MIT.

## Bugs / contributions

Issues and pull requests welcome at
<https://github.com/jerji/fb-extract-photos>.
