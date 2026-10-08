"""Yandex reverse image search (https://yandex.com/images/).

No API, but no browser either. Two plain requests per image:

  * POST the picture to /images/search?rpt=imageview&format=json, which
    answers with a cbirId - Yandex's handle for the upload.
  * GET the "sites" tab for that id. The page is server-rendered, and its
    results are embedded as JSON in a data-state attribute
    (initialState.cbirSites.sites): page URL, domain, a thumbnail, and the
    size of the copy that page holds.

Measured against this app's own misses on 2026-09-25: of 72 images that
IQDB, SauceNAO and Lens had found nothing or only a poor match for, 34
had a near-identical copy somewhere on Yandex - but only 7 of those on a
post page boorus/ can read tags from. The rest were telegra.ph,
joyreactor, Pinterest, wallpaper hosts and porn aggregators: proof the
picture exists, and nothing to tag it with. So only results on a post
page of a site with a parser are kept. Listing and tag-search pages on
those same sites are dropped too - Yandex returns plenty, and a list of
posts is not the post.

Like the Google engines, Yandex reports no similarity. Results are scored
by position, below ascii2d, and measure_ordinal_similarities replaces the
number with a measured one from the thumbnail.

Yandex answers automated traffic with its SmartCaptcha. There is no
browser here for the user to answer it in, so a challenge rests the
engine for half an hour instead - the same rest Google Lens takes after
an unanswered robot check.
"""
from __future__ import annotations

import html as html_module
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

import requests
from PIL import Image

from . import boorus
from .applog import get_logger
from .hard_timeout import HardTimeoutError, run_with_hard_timeout
from .image_prep import prepare_upload_bytes
from .progress_ticker import OnTick, ProgressTicker
from .sites import canonicalize_url

log = get_logger("yandex")

SEARCH_URL = "https://yandex.com/images/search"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# What the upload endpoint is asked for. Without it the answer is a full
# HTML page rather than the small JSON that carries the cbirId.
UPLOAD_REQUEST = '{"blocks":[{"block":"b-page_type_search-by-image__link"}]}'

# Yandex matches a downscaled picture as well as the original, and a
# 40MB PNG off a network share is a long upload for nothing.
UPLOAD_MAX_SIDE = 1600
UPLOAD_JPEG_QUALITY = 90

# Level with Google Lens's start: same kind of engine, same kind of
# guess. Below ascii2d's colour search, so neither overturns a measured
# IQDB or SauceNAO match on a position alone.
SIMILARITY_START = 80.0
SIMILARITY_STEP = 2.0
SIMILARITY_FLOOR = 50.0
MAX_RESULTS = 8

# A post page, as opposed to a listing, a tag search or a profile. Every
# site boorus/ reads names its posts by an id in the URL; the listings
# Yandex returns alongside them (s=list, r=posts/index, /posts?tags=)
# carry none.
POST_URL_RE = re.compile(
    r"[?&]id=\d+"                               # gelbooru-likes, rule34.us, xbooru
    r"|/posts?/(?:show/|view/)?\d+"             # danbooru, moebooru, e621, paheal, anime-pictures
    r"|/posts?/[0-9a-z]{8,}(?:[/?#]|$)"         # sankaku's hashed ids
    r"|/artworks/\d+|illust_id=\d+"             # pixiv
    r"|/status(?:es)?/\d+"                      # twitter / x
    r"|/art/[^/?#]+-\d+"                        # deviantart
    r"|/comments/[0-9a-z]+"                     # reddit
    r"|zerochan\.net/\d+",
    re.IGNORECASE,
)

# Danbooru's mirrors. Yandex indexed some posts under them, and only the
# main host has a parser - the post id is the same on all of them.
DANBOORU_MIRROR_RE = re.compile(r"^(?:[a-z0-9-]+\.)?donmai\.(?:us|moe)$", re.IGNORECASE)

# Where a SmartCaptcha shows up: a redirect to /showcaptcha, or the
# challenge served in place of the answer.
CHALLENGE_MARKERS = ("showcaptcha", "smartcaptcha", "checkcaptcha")


@dataclass
class YandexMatch:
    url: str
    thumb_url: Optional[str]
    similarity: float
    source_name: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class YandexError(Exception):
    pass


class YandexBlockedError(YandexError):
    """Yandex challenged, or several images in a row failed. Latched, so a
    batch doesn't spend a request per image to be refused again."""


_blocked = False

# Failures in a row before the engine stands down for the run. A page
# layout change fails every image the same way, and one bad answer is not
# evidence of anything.
MAX_CONSECUTIVE_FAILURES = 3
_consecutive_failures = 0

# After a challenge, how long before Yandex is asked again. Not cleared
# by starting a search - asking straight away is what earns the next one.
CHALLENGE_REST_SECONDS = 30 * 60
_resting_until: Optional[float] = None       # time.monotonic()

# The least time between the starts of two Yandex searches, however they
# were started. The batch delay paces a whole run; this also covers one
# right-click re-search after another.
MIN_SECONDS_BETWEEN_SEARCHES = 10.0
_next_turn: Optional[float] = None           # time.monotonic()
_state_lock = threading.Lock()


def is_blocked() -> bool:
    return _blocked or is_resting()


def is_resting() -> bool:
    return _resting_until is not None and time.monotonic() < _resting_until


def resting_until() -> str:
    """When the rest ends, as a local clock time, e.g. "21:41"."""
    until = _resting_until
    if until is None or not is_resting():
        return ""
    left = until - time.monotonic()
    return time.strftime("%H:%M", time.localtime(time.time() + left))


def reset_blocked_flag() -> None:
    """Called whenever a search starts. Clears a stand-down, but not the
    rest after a challenge."""
    global _blocked, _consecutive_failures
    _blocked = False
    _consecutive_failures = 0


def end_rest() -> None:
    """Lets Yandex be asked again now. For tests."""
    global _resting_until, _next_turn
    _resting_until = None
    _next_turn = None


def wait_for_turn(on_tick: Optional[OnTick] = None, should_stop=None) -> None:
    """Holds a search until MIN_SECONDS_BETWEEN_SEARCHES have passed since
    the last one started. The turn is claimed before waiting, so two
    searches arriving together are spaced out rather than both let go."""
    global _next_turn
    with _state_lock:
        now = time.monotonic()
        start_at = max(now, _next_turn or now)
        _next_turn = start_at + MIN_SECONDS_BETWEEN_SEARCHES
    wait = start_at - now
    if wait <= 0:
        return
    while True:
        left = start_at - time.monotonic()
        if left <= 0 or (should_stop is not None and should_stop()):
            return
        if on_tick:
            on_tick("Pacing Yandex", left, wait)
        time.sleep(min(1.0, left))


def _challenged() -> YandexBlockedError:
    # A rest, not a stand-down: a batch runs for hours, and Yandex should
    # come back into it when the half hour is up.
    global _resting_until
    with _state_lock:
        _resting_until = time.monotonic() + CHALLENGE_REST_SECONDS
    return YandexBlockedError(
        f"Yandex asked for a captcha, so it is resting until {resting_until()} rather than "
        "being asked again straight away. The other engines carry on."
    )


def _count_failure(message: str) -> YandexError:
    global _blocked, _consecutive_failures
    with _state_lock:
        _consecutive_failures += 1
        if _consecutive_failures < MAX_CONSECUTIVE_FAILURES:
            return YandexError(message)
        _blocked = True
    return YandexBlockedError(
        f"{message} That has now happened to {MAX_CONSECUTIVE_FAILURES} images in a row, so "
        "Yandex is standing down for the rest of this run. Start the search again to retry it."
    )


def _count_success() -> None:
    global _consecutive_failures
    with _state_lock:
        _consecutive_failures = 0


def prepare_upload(image_path: str) -> bytes:
    """The picture as a JPEG no larger than UPLOAD_MAX_SIDE.

    Always re-encoded: Yandex reads JPEG reliably and the app takes
    formats it may not. An animation gives its first frame. Anything
    Pillow can't open - a video, JPEG XL - is refused with a reason.
    """
    data, _ = prepare_upload_bytes(image_path)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            picture = im.convert("RGB")
    except (OSError, ValueError, EOFError, Image.DecompressionBombError) as exc:
        raise YandexError(
            "not in a format that can be sent to Yandex (video and JPEG XL are the usual "
            "reasons); the other engines can still search it"
        ) from exc
    picture.thumbnail((UPLOAD_MAX_SIDE, UPLOAD_MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=UPLOAD_JPEG_QUALITY)
    return out.getvalue()


def _looks_like_a_challenge(resp) -> bool:
    if any(marker in (resp.url or "").lower() for marker in CHALLENGE_MARKERS):
        return True
    try:
        head = (resp.text or "")[:6000].lower()
    except Exception:
        return False
    return any(marker in head for marker in CHALLENGE_MARKERS)


def search(image_path: str, timeout: float = 30.0,
           on_tick: Optional[OnTick] = None) -> List[YandexMatch]:
    """Upload to Yandex and return the results on post pages of sites the
    app can read tags from, in Yandex's order."""
    try:
        jpeg = prepare_upload(image_path)
    except OSError as exc:
        raise YandexError(f"Could not read image: {exc}") from exc

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": "https://yandex.com/images/"})
    try:
        with ProgressTicker("Waiting on Yandex", timeout, on_tick):
            upload = run_with_hard_timeout(
                lambda: session.post(
                    SEARCH_URL,
                    params={"rpt": "imageview", "format": "json", "request": UPLOAD_REQUEST},
                    files={"upfile": ("image.jpg", jpeg, "image/jpeg")},
                    timeout=timeout,
                ),
                timeout,
            )
            if _looks_like_a_challenge(upload):
                raise _challenged()
            cbir_id = _cbir_id(upload)
            if cbir_id is None:
                raise _count_failure(
                    f"Yandex's upload answer (HTTP {upload.status_code}) carried no search id - "
                    "the endpoint may have changed.")

            page = run_with_hard_timeout(
                lambda: session.get(
                    SEARCH_URL,
                    params={
                        "cbir_id": cbir_id, "rpt": "imageview", "cbir_page": "sites",
                        "url": f"https://avatars.mds.yandex.net/get-images-cbir/{cbir_id}/orig",
                    },
                    timeout=timeout,
                ),
                timeout,
            )
    except HardTimeoutError as exc:
        raise YandexError(f"Yandex request timed out: {exc}") from exc
    except requests.RequestException as exc:
        raise YandexError(f"Yandex request failed: {exc}") from exc

    sites = parse_sites(page.text)
    if sites is None:
        if _looks_like_a_challenge(page):
            raise _challenged()
        raise _count_failure(
            f"Yandex's results page (HTTP {page.status_code}) had no results block in it - "
            "the page layout may have changed.")
    _count_success()

    matches = keep_post_pages(sites)
    log.debug("Yandex: %d site(s) for %s, %d on a post page the app can read",
              len(sites), image_path, len(matches))
    return matches


def _cbir_id(resp) -> Optional[str]:
    try:
        blocks = resp.json().get("blocks") or []
        return (blocks[0].get("params") or {}).get("cbirId") or None
    except (ValueError, AttributeError, IndexError, TypeError):
        return None


_DATA_STATE_RE = re.compile(r'data-state="([^"]*)"')


def parse_sites(page: str) -> Optional[list]:
    """The "sites" results embedded in a results page, as Yandex gives
    them. None when the page has no results block at all - which is not
    the same as a block with nothing in it. Public so tests can feed a
    saved page."""
    for m in _DATA_STATE_RE.finditer(page or ""):
        if "cbirSites" not in m.group(1):
            continue
        try:
            state = json.loads(html_module.unescape(m.group(1)))
        except ValueError:
            continue
        block = (state.get("initialState") or {}).get("cbirSites")
        if isinstance(block, dict):
            sites = block.get("sites")
            return sites if isinstance(sites, list) else []
    return None


def keep_post_pages(sites: list) -> List[YandexMatch]:
    """The results worth a row: a post page on a site with a parser.

    Scored by position among the ones kept, not among everything Yandex
    returned - the Pinterest pins ahead of a booru post say nothing
    about how good the post is."""
    matches: List[YandexMatch] = []
    seen = set()
    for site in sites:
        if not isinstance(site, dict):
            continue
        url = post_url(site.get("url") or "")
        if url is None or url in seen:
            continue
        seen.add(url)
        thumb = (site.get("thumb") or {}).get("url")
        if thumb and thumb.startswith("//"):
            thumb = "https:" + thumb
        original = site.get("originalImage") or {}
        matches.append(YandexMatch(
            url=url,
            thumb_url=thumb or original.get("url"),
            similarity=max(SIMILARITY_START - len(matches) * SIMILARITY_STEP, SIMILARITY_FLOOR),
            source_name=_host_label(url),
            width=_int_or_none(original.get("width")),
            height=_int_or_none(original.get("height")),
        ))
        if len(matches) >= MAX_RESULTS:
            break
    return matches


def post_url(url: str) -> Optional[str]:
    """The canonical post URL, or None if this isn't a post page on a
    site boorus/ can read."""
    # Yandex tags every outbound link with its own utm_ parameters.
    url = re.sub(r"[?&]utm_[a-z]+=[^&#]*", "", url.strip())
    if "?" not in url and "&" in url:
        url = url.replace("&", "?", 1)
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if DANBOORU_MIRROR_RE.match(parts.netloc or "") and parts.netloc != "danbooru.donmai.us":
        url = parts._replace(netloc="danbooru.donmai.us").geturl()
    if not POST_URL_RE.search(url):
        return None
    url = canonicalize_url(url)
    return url if boorus.find_parser(url) is not None else None


def _host_label(url: str) -> Optional[str]:
    host = (urlparse(url).netloc or "").lower()
    return (host[4:] if host.startswith("www.") else host) or None


def _int_or_none(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
