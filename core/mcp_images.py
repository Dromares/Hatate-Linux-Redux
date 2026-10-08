"""Fetching the actual pictures an MCP tool call hands back.

This is the part of DAN-707 that is easiest to get wrong by skipping it:
"pick the best picture" is unanswerable from metadata, so get_images and
get_diff must return the real images, and at FULL resolution - the
match as it actually is, not the downscaled sample the review screen
shows for speed. gui/compare_dialog.py's load_comparison() already does
exactly this for the Wipe/Differences views; this module is the same
policy with the Qt types (QPixmap/QImage) taken back out, so it can run
on the MCP server's own thread rather than needing the GUI thread, and
so it can be unit-tested without a display.

Qt-free on purpose, like core/shortcuts.py and core/mcp_tools.py.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

from .applog import get_logger
from .config import Settings
from .image_diff import DifferenceMap, difference_map
from .models import ImageEntry, MatchCandidate
from .remote import download_bytes
from .search_engine import fetch_candidate_details

log = get_logger("mcp_images")


@dataclass
class FullResolutionMatch:
    """The result of trying to fetch a candidate's real picture."""
    data: Optional[bytes]
    url: Optional[str]
    is_full_resolution: bool     # True iff `data` came from direct_file_url, not a fallback
    fallback_reason: Optional[str] = None


def local_image_bytes(entry: ImageEntry) -> Optional[bytes]:
    """The local file's own bytes, read fresh rather than from any cache -
    there is no downscaled version of this one to confuse it with."""
    try:
        return Path(entry.path).read_bytes()
    except OSError as exc:
        log.warning("Could not read local file %s for an MCP image tool: %s", entry.path, exc)
        return None


def fetch_full_resolution_match(
    candidate: MatchCandidate, settings: Settings, local_path: Optional[str] = None,
) -> FullResolutionMatch:
    """The candidate's picture at full resolution - the same policy as
    gui/compare_dialog.py's load_comparison(), without the Qt types.

    Falls back to the preview or thumbnail only when no direct file URL
    is available at all, and says so in the result rather than silently
    handing back a sample: a comparison against a downscaled copy
    misrepresents exactly the quality difference it exists to judge.
    """
    if not candidate.direct_file_url and not candidate.preview_url:
        log.info("Fetching match details before returning its image: %s", candidate.url)
        try:
            fetch_candidate_details(candidate, settings, local_path=local_path)
        except Exception as exc:  # noqa: BLE001 - a fetch failure must not break the tool call
            log.warning("Could not fetch match details for %s: %s", candidate.url, exc)

    url = candidate.direct_file_url
    is_full_resolution = True
    fallback_reason = None
    if not url:
        url = candidate.preview_url or candidate.thumb_url
        is_full_resolution = False
        fallback_reason = "No direct full-resolution file URL was available; returned the preview/thumbnail instead."

    if not url:
        return FullResolutionMatch(None, None, False, "This match has no image URL at all.")

    data = download_bytes(
        url, settings.hydrus.timeout,
        referer=candidate.url if url == candidate.direct_file_url else None,
    )
    if data is None:
        return FullResolutionMatch(None, url, False, "The fetch failed - see the app log.")
    return FullResolutionMatch(data, url, is_full_resolution, fallback_reason)


@dataclass
class DiffResult:
    png_bytes: Optional[bytes]
    changed_percent: float
    verdict: str
    reliable: bool


def compute_diff(local_bytes: Optional[bytes], match_bytes: Optional[bytes]) -> DiffResult:
    """core/image_diff.py's structural difference map, as PNG bytes ready
    to hand back as an image content block."""
    local_image = _decode(local_bytes)
    match_image = _decode(match_bytes)
    if local_image is None or match_image is None:
        return DiffResult(None, 0.0, "Could not decode one or both images for comparison.", False)

    result: DifferenceMap = difference_map(local_image, match_image)
    png_bytes = None
    if result.mask is not None:
        buffer = io.BytesIO()
        result.mask.save(buffer, format="PNG")
        png_bytes = buffer.getvalue()
    return DiffResult(png_bytes, result.changed_percent, result.verdict, result.reliable)


def _decode(data: Optional[bytes]) -> Optional[Image.Image]:
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - a bad/truncated download, not a bug to raise on
        log.warning("Could not decode image data for an MCP image tool: %s", exc)
        return None
