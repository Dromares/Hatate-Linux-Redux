"""ascii2d reverse image search (https://ascii2d.net).

ascii2d has no JSON API, so this uploads the file and scrapes the HTML
results. Two searches run per image:

  * /search/color/{hash}  - colour histogram; strong at exact Pixiv/Twitter
    reposts IQDB often misses
  * /search/bovw/{hash}   - feature/BOVW search; looser, catches crops

ascii2d does not report a similarity percentage. Candidates get an
ordinal score (high 80s for the first colour hit, stepping down) that
sits below a typical IQDB "best match" so a 95% IQDB hit still wins,
but above a weak SauceNAO maybe-match.

The first item-box on a results page is the uploaded image itself and
has no external source links - those boxes are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .applog import get_logger
from .hard_timeout import HardTimeoutError, run_with_hard_timeout
from .image_prep import prepare_upload_bytes
from .progress_ticker import OnTick, ProgressTicker

log = get_logger("ascii2d")

ASCII2D_URL = "https://ascii2d.net/"
SEARCH_FILE_URL = "https://ascii2d.net/search/file"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Colour search is the more precise of the two; start just under a
# typical IQDB best-match so it can still beat a 70% SauceNAO maybe.
COLOR_SIMILARITY_START = 86.0
BOVW_SIMILARITY_START = 80.0
SIMILARITY_STEP = 2.0
MAX_RESULTS_PER_SEARCH = 6

COLOR_HASH_RE = re.compile(r"/search/color/([0-9a-f]+)", re.IGNORECASE)
DIMS_RE = re.compile(r"(\d+)\s*[x\u00d7]\s*(\d+)")


@dataclass
class Ascii2dMatch:
    url: str
    thumb_url: Optional[str]
    similarity: float
    width: Optional[int]
    height: Optional[int]
    source_name: Optional[str]
    search_kind: str  # "color" or "bovw"


class Ascii2dError(Exception):
    pass


class Ascii2dBlockedError(Ascii2dError):
    """ascii2d answered with a Cloudflare bot challenge rather than a
    result. Nothing about the request is wrong and nothing the user can
    configure will fix it: the challenge is decided below the HTTP layer,
    so no combination of User-Agent, Referer, CSRF token or other headers
    gets past it - all of those were tried against the live site.

    Kept distinct from a plain error so the caller can stop asking. Once
    ascii2d is challenging, every remaining image in a batch would spend a
    request to be refused, and report the same failure."""


# Latched for the session once ascii2d starts challenging, so a long batch
# doesn't repeat a request that cannot succeed. Cleared when a new search
# is started, in case the block has lifted since.
_blocked = False

# Cloudflare's interstitial. Checked so an ordinary 403 - which would mean
# something else entirely - is not mistaken for a bot challenge.
CHALLENGE_MARKERS = ("just a moment", "cf-browser-verification", "cf_chl_", "challenge-platform")


def is_blocked() -> bool:
    return _blocked


def reset_blocked_flag() -> None:
    global _blocked
    _blocked = False


def _looks_like_a_challenge(resp) -> bool:
    """True when this response is an anti-bot interstitial, not an answer."""
    try:
        body = (resp.text or "")[:4000].lower()
    except Exception:
        body = ""
    return any(marker in body for marker in CHALLENGE_MARKERS)


def search(image_path: str, timeout: float = 30.0, on_tick: Optional[OnTick] = None) -> List[Ascii2dMatch]:
    """Upload to ascii2d and return colour + feature matches, colour first."""
    log.debug("Uploading %s to ascii2d", image_path)
    try:
        data, filename = prepare_upload_bytes(image_path)
        files = {"file": (filename, data, "application/octet-stream")}
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": ASCII2D_URL,
        })
        with ProgressTicker("Waiting on ascii2d", timeout, on_tick):
            resp = run_with_hard_timeout(
                lambda: session.post(
                    SEARCH_FILE_URL, files=files, timeout=timeout, allow_redirects=True,
                ),
                timeout,
            )
    except OSError as exc:
        log.error("Could not read %s for ascii2d upload: %s", image_path, exc)
        raise Ascii2dError(f"Could not read image: {exc}") from exc
    except HardTimeoutError as exc:
        log.error("ascii2d request for %s exceeded the hard deadline: %s", image_path, exc)
        raise Ascii2dError(f"ascii2d request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("ascii2d request failed for %s: %s", image_path, exc)
        raise Ascii2dError(f"ascii2d request failed: {exc}") from exc

    if resp.status_code == 413:
        raise Ascii2dError("ascii2d rejected the image as too large, even after downscaling")
    if resp.status_code != 200:
        if _looks_like_a_challenge(resp):
            global _blocked
            _blocked = True
            log.error(
                "ascii2d served a Cloudflare bot challenge (HTTP %d) instead of results - "
                "skipping it for the rest of this search", resp.status_code,
            )
            raise Ascii2dBlockedError(
                "ascii2d is behind a Cloudflare bot check and won't accept uploads from "
                "this app. Nothing is wrong with your setup and there is no setting that "
                "fixes it - turn ascii2d off under Settings > Engine until the site stops "
                "challenging."
            )
        raise Ascii2dError(f"ascii2d returned HTTP {resp.status_code}")

    color_html = resp.text
    color_url = resp.url or ""
    if "ascii2d" not in color_html.lower() and "item-box" not in color_html:
        raise Ascii2dError(
            "ascii2d's response didn't look like a real results page "
            "(possible block/CAPTCHA/rate limit)"
        )

    matches = parse_results(color_html, color_url, kind="color")
    log.debug("ascii2d colour search parsed into %d match(es)", len(matches))

    color_hash = _hash_from_url(color_url)
    if color_hash:
        bovw_url = f"https://ascii2d.net/search/bovw/{color_hash}"
        try:
            with ProgressTicker("Waiting on ascii2d (feature)", timeout, on_tick):
                bovw_resp = run_with_hard_timeout(
                    lambda: session.get(bovw_url, timeout=timeout),
                    timeout,
                )
            if bovw_resp.status_code == 200:
                bovw_matches = parse_results(bovw_resp.text, bovw_resp.url or bovw_url, kind="bovw")
                log.debug("ascii2d feature search parsed into %d match(es)", len(bovw_matches))
                matches.extend(bovw_matches)
        except (requests.RequestException, HardTimeoutError) as exc:
            # Colour results are already in hand; a failed feature search
            # must not throw those away.
            log.debug("ascii2d feature search failed (colour results kept): %s", exc)

    return _dedupe(matches)


def parse_results(html: str, page_url: str, kind: str = "color") -> List[Ascii2dMatch]:
    """Turn one ascii2d results page into matches. Public so tests can
    feed fixture HTML without hitting the network."""
    soup = BeautifulSoup(html, "lxml")
    start = COLOR_SIMILARITY_START if kind == "color" else BOVW_SIMILARITY_START
    matches: List[Ascii2dMatch] = []
    rank = 0

    for box in soup.select(".item-box"):
        source_urls = _source_urls(box)
        if not source_urls:
            continue  # the uploaded image itself, or a box with no sources
        if rank >= MAX_RESULTS_PER_SEARCH:
            break

        thumb_url = None
        img = box.find("img")
        if img and img.get("src"):
            thumb_url = urljoin(page_url or ASCII2D_URL, img["src"].strip())  # type: ignore[union-attr]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

        width, height = _extract_dimensions(box.get_text(" ", strip=True))
        similarity = max(start - rank * SIMILARITY_STEP, 50.0)
        rank += 1

        for url in source_urls:
            matches.append(Ascii2dMatch(
                url=url,
                thumb_url=thumb_url,
                similarity=similarity,
                width=width,
                height=height,
                source_name=_host_label(url),
                search_kind=kind,
            ))
    return matches


def _source_urls(box) -> List[str]:
    """External source links from one result box, in document order.

    ascii2d's own thumbnail / hash / search links are skipped. A box
    with none of these is the query image (or an ad) and is ignored.
    """
    urls: List[str] = []
    seen = set()
    detail = box.select_one(".detail-box") or box
    for a in detail.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.startswith("//"):
            href = "https:" + href
        if not (href.startswith("http://") or href.startswith("https://")):
            continue
        host = (urlparse(href).netloc or "").lower()
        if "ascii2d.net" in host:
            continue
        if href in seen:
            continue
        seen.add(href)
        urls.append(href)
    return urls


def _extract_dimensions(text: str) -> tuple:
    match = DIMS_RE.search(text or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _hash_from_url(url: str) -> Optional[str]:
    match = COLOR_HASH_RE.search(url or "")
    return match.group(1) if match else None


def _host_label(url: str) -> Optional[str]:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _dedupe(matches: List[Ascii2dMatch]) -> List[Ascii2dMatch]:
    """Same source URL can appear in both colour and feature results -
    keep the higher (colour-first) score."""
    best: dict[str, Ascii2dMatch] = {}
    for m in matches:
        existing = best.get(m.url)
        if existing is None or m.similarity > existing.similarity:
            best[m.url] = m
    return list(best.values())
