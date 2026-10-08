"""Fetching the things a match points at - its image, a follow-up API
page, a HEAD for its format and size.

Split out of core/search_engine.py; everything here is re-exported there.
Other modules call these as remote.X(...) so a test stubs them in one
place.
"""
from __future__ import annotations

import base64
import binascii
from typing import Optional
from urllib.parse import unquote_to_bytes


import requests

from .applog import get_logger
from .config import Settings
from . import net
from .hard_timeout import HardTimeoutError
from .models import MatchCandidate
from .progress_ticker import OnTick, ProgressTicker

from .site_access import USER_AGENT, cookies_for_url, headers_for_url

log = get_logger("search")




def download_bytes(
    url: Optional[str], timeout: float, referer: Optional[str] = None, on_tick: Optional[OnTick] = None,
    cookies: Optional[dict] = None,
) -> Optional[bytes]:
    """Best-effort download of a URL's raw bytes - used for thumbnails and,
    via action_download_and_send_to_hydrus in the GUI, for downloading a
    matched image's actual full-resolution file. Failure here should never
    fail the overall search - tags/status matter more than the preview
    picture, and callers that do need the download to succeed check for
    None themselves.

    Sends a Referer header when given one, for the same hotlink-protection
    reason as fetch_remote_info below - some CDNs serve an HTML page
    instead of the real file for referer-less requests."""
    if not url:
        return None
    if url.startswith("data:"):
        # A data: URI carries its bytes inline - Google Lens's
        # exact-match tiles hand their thumbnails over that way, and
        # those thumbnails are the only picture an exact match has to be
        # compared against. Decoding costs nothing; requesting it would
        # simply fail.
        try:
            header, _, payload = url.partition(",")
            decoded = (base64.b64decode(payload) if ";base64" in header
                       else unquote_to_bytes(payload))
            # b64decode ignores invalid characters rather than raising,
            # so junk decodes to nothing. That is a failure, not a file.
            return decoded or None
        except (ValueError, binascii.Error) as exc:
            log.debug("Could not decode a data: URI (%s)", exc)
            return None
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        with ProgressTicker("Downloading image", timeout, on_tick):
            resp = net.get(url, headers=headers, timeout=timeout, deadline=timeout,
                           cookies=cookies or None)
        if resp.status_code == 200:
            return resp.content
    except (requests.RequestException, HardTimeoutError):
        pass
    return None


download_thumb = download_bytes  # back-compat alias


def fetch_text(url: str, settings: Settings, referer: Optional[str] = None) -> Optional[str]:
    """GETs a URL and returns its body as text, or None.

    Used for secondary API calls a parser can't make itself - the page
    list of a multi-image post, say - which arrive after the main fetch
    and so can't go through the parser's single-response contract.

    Site-specific headers apply here exactly as they do to the main
    fetch. Skipping them was a real failure: /ajax/illust/{id}/pages sent
    with the plain hatate User-Agent AND a Pixiv session cookie came back
    as a Cloudflare challenge (HTTP 403), so every multi-page Pixiv post
    silently fell back to page 1 - the very bug the page list exists to
    fix. Being logged in is what triggers it, which is why the main
    fetch's browser UA is not optional for this call either.
    """
    if not url:
        return None
    headers = headers_for_url(url, settings) or {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    cookies = cookies_for_url(url, settings)
    try:
        resp = net.get(url, headers=headers, timeout=settings.search_timeout,
                       deadline=settings.search_timeout, cookies=cookies)
    except requests.RequestException as exc:
        log.debug("Could not fetch %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.debug("%s returned HTTP %d", url, resp.status_code)
        return None
    return resp.text


def fetch_remote_info(
    url: Optional[str], timeout: float, referer: Optional[str] = None, on_tick: Optional[OnTick] = None,
) -> tuple:
    """Lightweight HEAD request for a match's format (from Content-Type)
    and file size (from Content-Length), without downloading the actual
    file. Returns (format_or_None, size_bytes_or_None). Best-effort - many
    sites don't support HEAD or omit these headers, so failure is normal
    and non-fatal, just leaves the info blank in the UI.

    Sends a Referer header (the originating post page) when given one -
    several image CDNs, including Gelbooru's, enforce hotlink protection
    and serve an HTML block/challenge page instead of the image when a
    request arrives with no Referer, which would otherwise look like a
    correctly-extracted image URL mysteriously "being" HTML."""
    if not url:
        return None, None
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    log.debug("HEAD %s (referer=%s)", url, referer)
    try:
        with ProgressTicker("Checking image info", timeout, on_tick):
            resp = net.head(url, headers=headers, timeout=timeout, deadline=timeout,
                            allow_redirects=True)
    except (requests.RequestException, HardTimeoutError) as exc:
        log.debug("HEAD %s failed: %s", url, exc)
        return None, None

    content_type = resp.headers.get("Content-Type", "")
    fmt = _format_from_content_type(content_type)
    log.debug(
        "HEAD %s -> HTTP %d, Content-Type=%r -> format=%s%s",
        url, resp.status_code, content_type, fmt,
        f" (final URL after redirects: {resp.url})" if resp.url != url else "",
    )

    length = resp.headers.get("Content-Length")
    size_bytes = int(length) if length and length.isdigit() else None

    return fmt, size_bytes


def _format_from_content_type(content_type: str) -> Optional[str]:
    ct = content_type.split(";")[0].strip().lower()
    known = {
        "image/jpeg": "JPEG", "image/jpg": "JPEG", "image/png": "PNG",
        "image/gif": "GIF", "image/webp": "WEBP", "image/bmp": "BMP",
        "image/avif": "AVIF", "video/webm": "WEBM", "video/mp4": "MP4",
    }
    if ct in known:
        return known[ct]
    if "/" in ct:
        return ct.split("/")[-1].upper()
    return None


def referer_for_candidate(candidate: MatchCandidate,
                          url_being_fetched: Optional[str]) -> Optional[str]:
    """Picks the Referer to send when fetching a match's own CDN, for
    sites that enforce hotlink protection. Only applies when the URL
    being fetched is actually one of the candidate's own booru-hosted
    URLs (preview_url/direct_file_url) - never for the search engine's
    own thumbnail host, which is a different origin entirely.

    Pixiv's CDN (i.pximg.net) is a special case: it's known to require
    the bare "https://www.pixiv.net/" origin specifically, not the exact
    artwork page URL - this is the value other Pixiv-downloading tools
    (yt-dlp, gallery-dl, etc.) use, and is more reliable than the general
    "use the source page" approach that works for most other booru sites.
    """
    if url_being_fetched not in (candidate.preview_url, candidate.direct_file_url):
        return None
    if candidate.url and "pixiv.net" in candidate.url:
        return "https://www.pixiv.net/"
    return candidate.url
