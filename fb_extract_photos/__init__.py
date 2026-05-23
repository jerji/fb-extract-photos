"""fb-extract-photos.

Pull photos, videos, and GIFs that you sent or uploaded from a Facebook
data dump, restore EXIF capture-time (and any GPS/camera tags Facebook
recorded), dedupe by perceptual hash, and lay them out under
``photos/YYYY/MM/``.
"""

from __future__ import annotations

__version__ = "0.5.0"

from .cli import main

__all__ = ["main", "__version__"]
