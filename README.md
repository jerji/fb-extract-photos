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
- `activity_you're_tagged_in/*.json` (URL-only in practice; no local files)

## Requirements

- Python 3.10+
- [`exiftool`](https://exiftool.org/) on `PATH` (or passed via `--exiftool`)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Or without installing:

```bash
python3 -m venv .venv
.venv/bin/pip install Pillow ImageHash
.venv/bin/python extract_photos.py --help
```

## Usage

```bash
fb-extract-photos --source ./output --output ./photos
```

Common flags:

| Flag             | Default          | Notes                                            |
| ---------------- | ---------------- | ------------------------------------------------ |
| `--source`       | `./output`       | Facebook dump root                               |
| `--output`       | `./photos`       | Destination folder                               |
| `--user`         | `Angel Ouellet`  | Your `sender_name` in messages                   |
| `--exiftool`     | `exiftool`       | Path to the exiftool binary                      |
| `--phash-size`   | `8`              | Perceptual hash size; larger = stricter dedupe   |
| `--no-dedupe`    | off              | Copy every reference, no dedupe                  |
| `--dry-run`      | off              | Don't copy or modify anything                    |

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
