"""Long-lived exiftool subprocesses and EXIF-argument construction.

The biggest performance lever in this tool is *not* re-spawning the
exiftool perl interpreter for every file. With its ``-stay_open True``
protocol we keep one process per worker thread, feed it commands
followed by ``-execute``, and read back until a ``{ready}`` sentinel.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from .types import MediaRef


class ExifToolDaemon:
    """A persistent ``exiftool -stay_open True -@ -`` process.

    Usage
    -----
    >>> d = ExifToolDaemon("exiftool")
    >>> d.run(["-DateTimeOriginal=2020:01:01 00:00:00", "/path/to/file.jpg"])
    >>> d.close()

    Threading
    ---------
    A single instance is **not** safe to share across threads — each
    ``run()`` call writes to stdin and reads from stdout, so concurrent
    callers would interleave commands and parse each other's output.
    Hand each thread its own daemon (we use a ``queue.Queue`` pool).

    Subtleties
    ----------
    * ``-q`` is intentionally **omitted** from ``-common_args``. With
      ``-q`` set, exiftool suppresses the ``{ready}`` sentinel too, and
      :meth:`run` deadlocks. We just consume the harmless
      ``"N image files updated"`` lines instead.
    * stderr is merged into stdout (``stderr=STDOUT``) so warnings can't
      fill an unread pipe and block exiftool mid-write.
    * Bufsize=1 + ``text=True`` gives us line-buffered IO which makes
      the readline-until-sentinel loop terminate promptly.
    """

    SENTINEL: str = "{ready}"

    def __init__(self, exe: str) -> None:
        """Spawn the exiftool subprocess.

        Parameters
        ----------
        exe:
            Path to (or name on PATH of) the exiftool executable.
        """
        self.proc: subprocess.Popen[str] = subprocess.Popen(
            [
                exe,
                "-stay_open", "True",
                "-@", "-",
                # Applied to every -execute group. See class docstring
                # for why -q is missing.
                "-common_args",
                "-overwrite_original",
                "-P",   # preserve filesystem mtime — we set our own after
                "-m",   # ignore minor warnings (e.g. non-fatal tag types)
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )

    def run(self, args: list[str]) -> str:
        """Send one ``-execute`` group and return the merged stdout/stderr
        captured between the write and the ``{ready}`` sentinel.

        The output is mostly status chatter that callers can ignore;
        we return it so debug code can inspect it if needed.
        """
        assert self.proc.stdin is not None and self.proc.stdout is not None
        # Each arg on its own line; trailing -execute kicks the run.
        self.proc.stdin.write("\n".join(args) + "\n-execute\n")
        self.proc.stdin.flush()

        captured: list[str] = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                # exiftool exited unexpectedly. Don't block further.
                break
            if line.strip() == self.SENTINEL:
                break
            captured.append(line)
        return "".join(captured)

    def close(self) -> None:
        """Ask exiftool to exit cleanly, killing it if it doesn't."""
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.write("-stay_open\nFalse\n")
                self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def build_exif_args(ref: MediaRef, dest: Path) -> list[str] | None:
    """Construct the exiftool argument list for one media file, or
    return ``None`` if this kind doesn't get EXIF.

    For ``audio`` and ``file`` kinds the format diversity is too wide
    (mp3, pdf, docx, mid, …) to write meaningful EXIF safely — we just
    set the filesystem mtime later instead and skip exiftool entirely.

    For photos/GIFs/videos we always write: ``DateTimeOriginal``,
    ``CreateDate``, ``ModifyDate``, ``FileModifyDate``,
    ``FileCreateDate``. Videos additionally get QuickTime atoms so
    Apple Photos and friends pick them up.

    ``ref.extra_exif`` is appended verbatim — that's where GPS /
    camera tags pulled out of ``media_metadata.exif_data`` end up.
    """
    if ref.kind in ("audio", "file"):
        return None

    stamp = datetime.fromtimestamp(ref.timestamp).strftime("%Y:%m:%d %H:%M:%S")

    args: list[str] = []
    if ref.kind == "video":
        # `-api quicktime=1` lets exiftool write the QT-only tags below.
        args += ["-api", "quicktime=1"]
        for tag in (
            "CreationDate", "CreateDate", "ModifyDate",
            "TrackCreateDate", "TrackModifyDate",
            "MediaCreateDate", "MediaModifyDate",
        ):
            args.append(f"-{tag}={stamp}")

    args += [
        f"-DateTimeOriginal={stamp}",
        f"-CreateDate={stamp}",
        f"-ModifyDate={stamp}",
        f"-FileModifyDate={stamp}",
        f"-FileCreateDate={stamp}",
    ]
    for tag, value in ref.extra_exif.items():
        args.append(f"-{tag}={value}")
    args.append(str(dest))
    return args
