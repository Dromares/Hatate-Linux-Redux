"""File types this app will add from a folder, a file dialog, or a drop.

IQDB/SauceNAO/ascii2d/Google want still images; trace.moe wants a
screenshot.
Hydrus libraries also hold AVIF, JPEG XL, TIFF, and video, so those are
accepted here even when a given engine later rejects them - the other
engines still get a chance, and a rejection is an engine error, not a
silent skip at add time.
"""
from __future__ import annotations

from typing import FrozenSet

IMAGE_EXTENSIONS: FrozenSet[str] = frozenset({
    ".jpg", ".jpeg", ".jfif",
    ".png", ".apng",
    ".gif",
    ".bmp",
    ".webp",
    ".avif",
    ".jxl",
    ".tif", ".tiff",
    ".webm", ".mp4", ".mkv",
})

# Qt file-dialog filter. Keep in sync with IMAGE_EXTENSIONS.
FILE_DIALOG_FILTER = (
    "Images and video ("
    "*.jpg *.jpeg *.jfif *.png *.apng *.gif *.bmp *.webp "
    "*.avif *.jxl *.tif *.tiff *.webm *.mp4 *.mkv"
    ");;All files (*)"
)
