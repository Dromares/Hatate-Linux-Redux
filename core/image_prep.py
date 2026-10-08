"""Prepares an image for uploading to IQDB/SauceNAO.

Both services enforce their own upload size caps and reject anything over
that with HTTP 413 (Payload Too Large). Since they only need enough visual
detail to perceptually match the image - not the original file - an
oversized local file (a high-res scan, an unusually large PNG, etc.) is
downscaled and re-encoded as JPEG here rather than sent as-is.
"""
from __future__ import annotations

import io
import os
import threading
from typing import Optional, Tuple

from PIL import Image

from .applog import get_logger

log = get_logger("image_prep")

# Conservative default: IQDB and SauceNAO's own limits are commonly cited
# around 8 MiB, so this leaves headroom rather than targeting that exactly.
DEFAULT_MAX_BYTES = 7 * 1024 * 1024
DEFAULT_MAX_DIMENSION = 3000  # plenty of detail for perceptual matching

# Caches the single most recent prep result. IQDB and SauceNAO each
# independently call prepare_upload_bytes() for the same file, seconds
# apart, within one search - for a large file (a decode-heavy format like
# PNG has no "decode at reduced size" shortcut the way JPEG does), fully
# re-decoding and re-downscaling the original a second time is pure waste.
# Keyed on (path, file_size, max_bytes, max_dimension); file_size doubles
# as a cheap correctness check in case the file changes between calls.
_last_prepared: Optional[Tuple[str, int, int, int, bytes, str]] = None
_prep_lock = threading.Lock()


def prepare_upload_bytes(
    path: str,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
) -> Tuple[bytes, str]:
    """Returns (bytes, filename) suitable for uploading to a reverse image
    search service. If the file is already small enough, its raw bytes are
    used unchanged (preserves original format). Otherwise, it's resized
    down and re-encoded as JPEG, reducing quality and then dimensions
    further if needed, until it fits under max_bytes.

    Never raises for image-processing reasons - falls back to the raw
    file bytes if resizing fails for any reason, so a bug here degrades to
    the previous (occasionally-413) behavior rather than blocking search
    entirely.

    Thread-safe: IQDB and SauceNAO (and any extra engines) may call this
    for the same file at the same time when those engines run in
    parallel. The cache lock also makes the second caller reuse the
    first's decode instead of doing the work twice.
    """
    global _last_prepared
    filename = os.path.basename(path)
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        log.warning("Could not stat %s before upload prep: %s", path, exc)
        with open(path, "rb") as fh:
            return fh.read(), filename

    with _prep_lock:
        if _last_prepared is not None:
            cached_path, cached_size, cached_max_bytes, cached_max_dim, cached_data, cached_filename = _last_prepared
            if (cached_path, cached_size, cached_max_bytes, cached_max_dim) == (path, file_size, max_bytes, max_dimension):
                log.debug(
                    "Reusing already-prepared upload bytes for %s - avoids re-decoding the "
                    "original a second time for the other engine", filename,
                )
                return cached_data, cached_filename

        if file_size <= max_bytes:
            with open(path, "rb") as fh:
                data = fh.read()
            _last_prepared = (path, file_size, max_bytes, max_dimension, data, filename)
            return data, filename

        log.info(
            "%s is %.1f MB (over the %.1f MB upload cap), downscaling before search",
            filename, file_size / 1024 / 1024, max_bytes / 1024 / 1024,
        )

        try:
            data, out_filename = _downscale_to_fit(path, filename, max_bytes, max_dimension)
            log.info(
                "%s downscaled for upload: %.1f MB -> %.1f MB",
                filename, file_size / 1024 / 1024, len(data) / 1024 / 1024,
            )
            _last_prepared = (path, file_size, max_bytes, max_dimension, data, out_filename)
            return data, out_filename
        except Exception as exc:
            log.warning(
                "Could not downscale %s for upload (%s), sending original bytes - "
                "the service may reject it with HTTP 413", filename, exc,
            )
            with open(path, "rb") as fh:
                return fh.read(), filename


def _downscale_to_fit(path: str, filename: str, max_bytes: int, max_dimension: int) -> Tuple[bytes, str]:
    with Image.open(path) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")  # type: ignore[assignment]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns (JPEG has no alpha channel)

        if max(im.size) > max_dimension:
            im.thumbnail((max_dimension, max_dimension), Image.LANCZOS)  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns

        buf = io.BytesIO()
        quality = 90
        while quality >= 40:
            buf.seek(0)
            buf.truncate(0)
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                break
            quality -= 10

        # Still too big even at low quality (very large dimensions) - shrink further.
        while buf.tell() > max_bytes and max(im.size) > 500:
            im.thumbnail((int(im.width * 0.8), int(im.height * 0.8)), Image.LANCZOS)  # type: ignore[attr-defined]  # Pillow stub gap: constant/return type missing or narrower than the object PIL actually returns
            buf.seek(0)
            buf.truncate(0)
            im.save(buf, format="JPEG", quality=70, optimize=True)

        out_filename = os.path.splitext(filename)[0] + ".jpg"
        return buf.getvalue(), out_filename
