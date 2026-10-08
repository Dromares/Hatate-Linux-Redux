"""Google reverse image search (Cloud Vision web detection, or the
public page).

Google is a general web index rather than a booru index, so it earns its
place for a different reason than the others: when IQDB, SauceNAO and
ascii2d all come back empty, Google is often the only engine that still
finds the page an image was posted on - a personal site, a news article,
an artist's own portfolio, a Tumblr or Blogger post. What it hands back
is a page URL, not a tagged booru post, so it fills the "where did this
come from" gap and not the "what are its tags" one.

Two routes, because the free one no longer works on its own:

  * With a Google Cloud Vision API key, WEB_DETECTION is asked directly.
    It is an official JSON API and answers with the pages carrying a
    matching image, which is exactly what this engine is for. Free tier
    is 1000 images a month at the time of writing.

  * Without a key, the image is uploaded to the public endpoint. That
    upload still works - POST /searchbyimage/upload answers 303 with a
    real result token - but CONFIRMED against the live site: every
    landing it redirects to (`udm=26`, `udm=48`, `tbm=isch`, no-udm,
    lens.google.com/v3/upload, the `asearch=arc` async fragment) is a
    92KB JavaScript bootstrap with no result markup in it at all, and an
    old-browser User-Agent now gets "Update your browser" instead of the
    basic HTML page it used to. The results are fetched and rendered by
    Google's own JavaScript, which this app does not run.

    So the keyless route reports that, once, and stands down for the
    session rather than uploading every image in a batch to read an
    empty page. The HTML parsers are kept and exercised: Google still
    serves a classic results page in some cases, and when it does there
    is no reason to refuse it.

Google reports no similarity percentage, and its ordering is by page
relevance, not by how much the picture looks like yours. Candidates get
an ordinal score starting below ascii2d's, so a Google hit can surface a
source nothing else found without ever overturning a real IQDB or
SauceNAO match.

Off by default: it needs a key to be useful, and a hit carries no tags.
"""
from __future__ import annotations

import base64
import html as html_module
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .applog import get_logger
from .hard_timeout import HardTimeoutError, run_with_hard_timeout
from .image_prep import prepare_upload_bytes
from .progress_ticker import OnTick, ProgressTicker

log = get_logger("google_images")

GOOGLE_URL = "https://www.google.com/"
SEARCH_BY_IMAGE_URL = "https://www.google.com/searchbyimage/upload"
VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Records a consent choice so the EU interstitial doesn't stand between
# the upload and the results. Without it every request from an EU exit
# lands on consent.google.com and no search ever happens.
CONSENT_COOKIE = "CAESHAgBEhIaAB"

# Below ascii2d's feature-search start (80): Google ranks by page
# relevance, not by visual similarity, so its first hit deserves less
# confidence than an engine that actually compared the pixels.
SIMILARITY_START = 78.0
# Pages Vision lists as carrying only a PARTIAL match - a crop, a
# resize, or a visually similar picture rather than this one. Scored as
# a separate, lower band so an exact match always sorts above them.
PARTIAL_SIMILARITY_START = 70.0
# A page Vision returned under "pagesWithMatchingImages" but attached no
# matching image to. MEASURED over 10 images from a real library: not one
# page entry ever carried an attached image, and the same four YouTube
# URLs came back for four unrelated files - they are a loose association
# ("cartoon"), not this picture's source. They are kept rather than
# dropped, because on other kinds of image the association may well be
# real, but they sort below every match Vision actually evidenced.
BARE_PAGE_SIMILARITY_START = 60.0
SIMILARITY_STEP = 2.0
SIMILARITY_FLOOR = 50.0
MAX_RESULTS = 8

# How many web-detection entries to ask Vision for. More than MAX_RESULTS
# because the response mixes exact and partial pages together and some
# are dropped as Google's own hosts.
VISION_MAX_RESULTS = 20

# Google's own infrastructure. These appear all over both landings -
# navigation, asset hosts, the "report images" links - and none of them
# is ever the page an image came from.
GOOGLE_HOST_SUFFIXES = (
    "gstatic.com",
    "googleusercontent.com",
    "googleapis.com",
    "googleadservices.com",
    "googlesyndication.com",
    "googletagmanager.com",
    "google-analytics.com",
    "ggpht.com",
    "goo.gl",
    "schema.org",
    "w3.org",
)
GOOGLE_DOMAIN_RE = re.compile(r"(?:^|\.)google(?:\.[a-z]{2,3}){1,2}$", re.IGNORECASE)

# Hosts that serve Google's result thumbnails. Not sources, but worth
# keeping rather than discarding: they are the preview the GUI shows
# next to a candidate.
THUMBNAIL_HOST_HINTS = ("encrypted-tbn", "gstatic.com", "googleusercontent.com", "ggpht.com")

# String literals inside a Lens data blob, in document order. Slashes are
# escaped in that JSON, which is why the scheme is matched loosely.
JS_STRING_RE = re.compile(r'"((?:[^"\\]|\\.){2,400})"')

# A Google redirector wrapping the real destination, used on the classic
# results page when JavaScript is off.
REDIRECT_PATHS = ("/url", "/imgres")

CHALLENGE_MARKERS = (
    "our systems have detected unusual traffic",
    "/sorry/index",
    "recaptcha",
    "id=\"captcha",
    "unusual traffic from your computer network",
)

# The shell Google serves in place of results. Its own no-JavaScript
# notice is the reliable tell: it links to /httpservice/retry/enablejs
# and hides the page body until its scripts have run.
JS_SHELL_MARKERS = ("/httpservice/retry/enablejs", "if you are not redirected")

TAG_RE = re.compile(r"<[^>]+>")

# Image CDNs whose URL carries the id of the post it belongs to, so a
# bare image match can be turned into a page a user can actually read
# tags off. Deliberately a short, verified list rather than a guess at a
# pattern: CONFIRMED live that rule34storage.b-cdn.net/posts/1383/1383457/
# is rule34.us post 1383457, by fetching that post and finding the same
# id and its artist on the page. Hosts whose URL is only a content hash
# (img*.rule34.us, img.kemono.cr, pbs.twimg.com) carry no id to recover
# and are left as the direct image link.
CDN_POST_PATTERNS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"^https?://rule34storage\.b-cdn\.net/posts/\d+/(\d+)/", re.IGNORECASE),
     "https://rule34.us/index.php?r=posts/view&id={}"),
)


@dataclass
class GoogleImagesMatch:
    url: str
    thumb_url: Optional[str]
    similarity: float
    title: Optional[str] = None
    source_name: Optional[str] = None


class GoogleImagesError(Exception):
    pass


class GoogleImagesBlockedError(GoogleImagesError):
    """Google will not answer this app for the rest of the session, and
    the next image would meet the same wall.

    Latched like ascii2d's Cloudflare block: once Google is refusing,
    every remaining image in a batch would spend a request to be refused
    and report the identical failure. Covers a consent wall, an "unusual
    traffic" check, and a Vision key that is rejected or out of quota -
    different causes, same consequence for the batch.
    """


class GoogleImagesJavaScriptError(GoogleImagesBlockedError):
    """The upload worked and Google answered with a page that holds no
    results until its JavaScript has run.

    A subclass of the blocked error because it has the same shape: it is
    a property of Google's current front end, not of this image, so the
    remaining images in the batch are certain to hit it too. Kept
    distinct so the message can name the fix, which is a Vision API key
    rather than waiting.
    """


_blocked = False


def is_blocked() -> bool:
    return _blocked


def reset_blocked_flag() -> None:
    global _blocked
    _blocked = False


def _latch(message: str, error_type=GoogleImagesBlockedError) -> GoogleImagesBlockedError:
    global _blocked
    _blocked = True
    return error_type(message)


def search(
    image_path: str,
    timeout: float = 30.0,
    on_tick: Optional[OnTick] = None,
    api_key: str = "",
) -> List[GoogleImagesMatch]:
    """Return the pages Google knows this image from.

    Uses the Cloud Vision web-detection API when a key is configured,
    and the public upload endpoint when one is not.
    """
    try:
        data, filename = prepare_upload_bytes(image_path)
    except OSError as exc:
        log.error("Could not read %s for the Google search: %s", image_path, exc)
        raise GoogleImagesError(f"Could not read image: {exc}") from exc

    if (api_key or "").strip():
        return _search_vision(data, api_key.strip(), timeout, on_tick)
    return _search_public(data, filename, timeout, on_tick)


# ----------------------------------------------------------------------
# Cloud Vision web detection
# ----------------------------------------------------------------------

def _search_vision(data: bytes, api_key: str, timeout: float,
                   on_tick: Optional[OnTick]) -> List[GoogleImagesMatch]:
    # Annotated loosely on purpose: the request body is a JSON tree,
    # and inferring a precise type for it buys nothing.
    body: dict = {
        "requests": [{
            "image": {"content": base64.b64encode(data).decode("ascii")},
            "features": [{"type": "WEB_DETECTION", "maxResults": VISION_MAX_RESULTS}],
        }],
    }
    try:
        with ProgressTicker("Waiting on Google (Vision)", timeout, on_tick):
            resp = run_with_hard_timeout(
                lambda: requests.post(
                    VISION_URL,
                    params={"key": api_key},
                    json=body,
                    headers={"User-Agent": USER_AGENT},
                    timeout=timeout,
                ),
                timeout,
            )
    except HardTimeoutError as exc:
        log.error("Google Vision request exceeded the hard deadline: %s", exc)
        raise GoogleImagesError(f"Google Vision request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("Google Vision request failed: %s", exc)
        raise GoogleImagesError(f"Google Vision request failed: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code != 200:
        raise _vision_error(resp.status_code, payload)

    # A per-image error is reported inside a 200 response rather than as
    # a status code, so it has to be read out of the body.
    inner = (payload.get("responses") or [{}])[0].get("error")
    if inner:
        raise GoogleImagesError(f"Google Vision: {inner.get('message') or inner}")

    matches = parse_vision_results(payload)
    log.debug("Google Vision returned %d match(es)", len(matches))
    return matches


def _vision_error(status: int, payload: dict) -> GoogleImagesError:
    """Turn a Vision failure into the right kind of error.

    A rejected key, a disabled API and an exhausted quota all fail every
    image in the batch identically, so they latch the engine off instead
    of being reported once per image. A malformed request is about this
    image, and does not.
    """
    error = payload.get("error") or {}
    message = (error.get("message") or "").strip() or f"HTTP {status}"
    reason = (error.get("status") or "").upper()

    if status == 429 or reason == "RESOURCE_EXHAUSTED":
        return _latch(
            "Google Vision quota is exhausted, so it can't answer any more images this "
            f"month: {message}. Turn Google Images off under Settings > Engine, or raise "
            "the quota in the Google Cloud console."
        )
    if status in (401, 403) or reason in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return _latch(
            f"Google Vision refused the API key: {message}. Check the key under "
            "Settings > Engine, and that the Cloud Vision API is enabled for its project."
        )
    if status == 400 and "api key" in message.lower():
        return _latch(f"Google Vision rejected the API key: {message}")
    return GoogleImagesError(f"Google Vision: {message}")


def parse_vision_results(payload: dict) -> List[GoogleImagesMatch]:
    """Turn a Vision WEB_DETECTION body into matches. Public for tests.

    Vision answers in several arrays, and the useful one is not the
    obvious one. MEASURED over 10 images from a real library:

      * `fullMatchingImages` - exact matches, present on 6 of the 10, and
        pointing at the booru CDNs the pictures actually came from. This
        is where the answer lives.
      * `pagesWithMatchingImages` - present on 6 of the 10, but not one
        entry carried an attached matching image, and the same four
        YouTube URLs came back for four unrelated files.
      * `visuallySimilarImages` - 20 entries on every single image,
        related only by subject. Never read: it would bury every real
        match under twenty near-misses.

    So results are banded by how strongly Vision evidenced them, and
    within a band they keep Vision's own order.
    """
    responses = payload.get("responses") or []
    detection = (responses[0] if responses else {}).get("webDetection") or {}

    exact: List[Tuple[str, Optional[str], Optional[str]]] = []
    partial: List[Tuple[str, Optional[str], Optional[str]]] = []
    bare: List[Tuple[str, Optional[str], Optional[str]]] = []
    seen: set = set()

    def collect(bucket, raw_url, title, thumb) -> None:
        url = (raw_url or "").strip().split("#", 1)[0]
        if not url.startswith(("http://", "https://")):
            return
        # A bare CDN image is not somewhere a user can read tags. Where
        # the post it belongs to can be recovered, the candidate becomes
        # that post and the image becomes its thumbnail.
        post_url = post_url_for_image(url)
        if post_url:
            thumb = thumb or url
            url = post_url
        if url in seen or is_google_host(host_label(url) or ""):
            return
        seen.add(url)
        bucket.append((url, title, thumb))

    for page in detection.get("pagesWithMatchingImages") or []:
        if not isinstance(page, dict):
            continue
        full_images = page.get("fullMatchingImages") or []
        part_images = page.get("partialMatchingImages") or []
        bucket = exact if full_images else (partial if part_images else bare)
        collect(bucket, page.get("url"), _clean_title(page.get("pageTitle")),
                _first_image_url(full_images or part_images))

    # The exact matches themselves, which carry no page of their own.
    for image in detection.get("fullMatchingImages") or []:
        if isinstance(image, dict):
            collect(exact, image.get("url"), None, image.get("url"))
    for image in detection.get("partialMatchingImages") or []:
        if isinstance(image, dict):
            collect(partial, image.get("url"), None, image.get("url"))

    matches: List[GoogleImagesMatch] = []
    bands = (
        (exact, SIMILARITY_START),
        (partial, PARTIAL_SIMILARITY_START),
        (bare, BARE_PAGE_SIMILARITY_START),
    )
    for group, start in bands:
        for rank, (url, title, thumb) in enumerate(group):
            if len(matches) >= MAX_RESULTS:
                return matches
            matches.append(GoogleImagesMatch(
                url=url,
                thumb_url=thumb,
                similarity=_score(rank, start),
                title=title,
                source_name=host_label(url),
            ))
    return matches


def post_url_for_image(image_url: str) -> Optional[str]:
    """The post page a CDN image belongs to, when its URL names it.

    Public so tests can pin the mapping. Returns None for anything not
    on the verified list, which leaves the match as the image link it
    already was - no worse than before, and never a guessed URL.
    """
    for pattern, template in CDN_POST_PATTERNS:
        found = pattern.match(image_url or "")
        if found:
            return template.format(found.group(1))
    return None


def _first_image_url(images) -> Optional[str]:
    for image in images or []:
        if isinstance(image, dict):
            url = (image.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return url
    return None


def _clean_title(title) -> Optional[str]:
    """Vision returns the page's own <title>, sometimes with the matched
    words wrapped in <b> and always with its entities unresolved."""
    if not isinstance(title, str):
        return None
    text = html_module.unescape(TAG_RE.sub("", title)).strip()
    return text or None


# ----------------------------------------------------------------------
# The public endpoint, for users without a Vision key
# ----------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": GOOGLE_URL,
    })
    session.cookies.set("SOCS", CONSENT_COOKIE, domain=".google.com")
    return session


def _search_public(data: bytes, filename: str, timeout: float,
                   on_tick: Optional[OnTick]) -> List[GoogleImagesMatch]:
    log.debug("Uploading to Google Images (no Vision key configured)")
    session = build_session()
    files = {"encoded_image": (filename, data, "application/octet-stream")}
    try:
        with ProgressTicker("Waiting on Google Images", timeout, on_tick):
            resp = run_with_hard_timeout(
                lambda: session.post(
                    SEARCH_BY_IMAGE_URL,
                    files=files,
                    params={"hl": "en"},
                    timeout=timeout,
                    allow_redirects=True,
                ),
                timeout,
            )
    except HardTimeoutError as exc:
        log.error("Google request exceeded the hard deadline: %s", exc)
        raise GoogleImagesError(f"Google request timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("Google request failed: %s", exc)
        raise GoogleImagesError(f"Google request failed: {exc}") from exc

    if resp.status_code == 413:
        raise GoogleImagesError("Google rejected the image as too large, even after downscaling")

    # Checked before the status code, because a consent wall and a bot
    # check are both served as a perfectly ordinary HTTP 200.
    if _looks_like_a_challenge(resp):
        log.error(
            "Google served a consent wall or bot check (HTTP %d) instead of results - "
            "skipping it for the rest of this search", resp.status_code,
        )
        raise _latch(
            "Google is answering with a consent page or an \"unusual traffic\" check "
            "instead of results, so it won't accept uploads from this app right now. "
            "No setting fixes it - it clears on Google's schedule. Turn Google Images "
            "off under Settings > Engine, or try again later."
        )
    if resp.status_code != 200:
        raise GoogleImagesError(f"Google returned HTTP {resp.status_code}")

    final_url = resp.url or GOOGLE_URL
    body = resp.text or ""
    if "lens.google." in final_url.lower():
        matches = parse_lens_results(body)
        log.debug("Google Lens parsed into %d match(es)", len(matches))
    else:
        matches = parse_results(body, final_url)
        log.debug("Google results page parsed into %d match(es)", len(matches))

    if not matches and looks_like_a_javascript_shell(body):
        log.error("Google answered with a JavaScript-only results page - standing down "
                  "for the rest of this search")
        raise _latch(
            "Google accepted the image but answers with a page that only fills in its "
            "results once its own JavaScript has run, which this app does not do. Add a "
            "Google Cloud Vision API key under Settings > Engine to use Google's web "
            "detection API instead, or turn Google Images off.",
            GoogleImagesJavaScriptError,
        )
    return matches


def looks_like_a_javascript_shell(html: str) -> bool:
    """True when this page holds no results until Google's scripts run.

    Told apart from a genuinely empty result page so that "Google found
    nothing" is never recorded for an image Google was never asked
    about - a false NOT_FOUND would be cached and stop the image being
    searched again.
    """
    lowered = (html or "")[:20000].lower()
    return any(marker in lowered for marker in JS_SHELL_MARKERS)


def _looks_like_a_challenge(resp) -> bool:
    """True when this response is a consent wall or a bot check."""
    final_url = (getattr(resp, "url", "") or "").lower()
    if "consent.google." in final_url or "/sorry/" in final_url:
        return True
    try:
        body = (resp.text or "")[:6000].lower()
    except Exception:
        body = ""
    return any(marker in body for marker in CHALLENGE_MARKERS)


def parse_results(html: str, page_url: str) -> List[GoogleImagesMatch]:
    """Turn a classic Google results page into matches. Public so tests
    can feed fixture HTML without hitting the network.

    Google's result markup is generated and its class names change, so
    the anchor is found by structure instead: a link wrapping a heading
    is a result, and a link through /url?q= is one on the no-JavaScript
    page. Neither depends on a class name surviving the next redesign.
    """
    soup = BeautifulSoup(html, "lxml")
    found: List[GoogleImagesMatch] = []
    seen = set()

    links = [(a, str(a.get("href") or "").strip()) for a in soup.find_all("a", href=True)]
    anchors = [(a, href) for a, href in links if href and a.find(["h3", "h2"])]
    if not anchors:
        anchors = [(a, href) for a, href in links
                   if href and urlparse(href).path in REDIRECT_PATHS]

    for a, href in anchors:
        url = _resolve_href(href, page_url)
        if not url or url in seen:
            continue
        seen.add(url)

        heading = a.find(["h3", "h2"])
        title = (heading.get_text(" ", strip=True) if heading else a.get_text(" ", strip=True)) or None
        found.append(GoogleImagesMatch(
            url=url,
            thumb_url=_thumb_near(a, page_url),
            similarity=_score(len(found), SIMILARITY_START),
            title=title,
            source_name=host_label(url),
        ))
        if len(found) >= MAX_RESULTS:
            break
    return found


def parse_lens_results(html: str) -> List[GoogleImagesMatch]:
    """Pull matches out of a Google Lens page's embedded data.

    Lens renders from a JSON blob whose arrays are positional and
    undocumented, so nothing here counts fields. It reads the blob's
    string literals in order and uses two facts that have held across
    every layout change so far: a result's page URL is followed closely
    by its title, and its thumbnail (on a Google asset host) appears
    just before it.
    """
    found: List[GoogleImagesMatch] = []
    seen = set()
    literals = [_unescape(m.group(1)) for m in JS_STRING_RE.finditer(html or "")]

    pending_thumb: Optional[str] = None
    for index, literal in enumerate(literals):
        if not literal.startswith(("http://", "https://")):
            continue
        host = host_label(literal) or ""
        if is_google_host(host):
            if any(hint in host for hint in THUMBNAIL_HOST_HINTS):
                pending_thumb = literal
            continue
        url = literal.split("#", 1)[0]
        if url in seen:
            continue
        seen.add(url)
        found.append(GoogleImagesMatch(
            url=url,
            thumb_url=pending_thumb,
            similarity=_score(len(found), SIMILARITY_START),
            title=_title_after(literals, index),
            source_name=host or None,
        ))
        pending_thumb = None
        if len(found) >= MAX_RESULTS:
            break
    return found


def _title_after(literals: List[str], index: int) -> Optional[str]:
    """The nearest following literal that reads like a page title.

    Bounded to a few positions: in every Lens layout seen so far the
    title sits immediately after the URL, and searching further would
    start picking up an unrelated result's text.
    """
    for candidate in literals[index + 1:index + 4]:
        text = candidate.strip()
        if not (3 <= len(text) <= 200):
            continue
        if text.startswith(("http://", "https://", "/", "data:")):
            continue
        if not any(ch.isalpha() for ch in text):
            continue
        return text
    return None


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def _score(rank: int, start: float) -> float:
    return max(start - rank * SIMILARITY_STEP, SIMILARITY_FLOOR)


def _resolve_href(href: str, page_url: str) -> Optional[str]:
    """The real destination behind a result link.

    Google's no-JavaScript page wraps every result in /url?q=..., and a
    relative href is one of its own pages. Both are unwrapped here so a
    candidate is stored as the URL a user could actually open.
    """
    href = (href or "").strip()
    if not href:
        return None
    absolute = urljoin(page_url or GOOGLE_URL, href)
    parsed = urlparse(absolute)
    if parsed.path in REDIRECT_PATHS:
        query = parse_qs(parsed.query)
        for key in ("q", "url", "imgrefurl"):
            target = (query.get(key) or [""])[0]
            if target.startswith(("http://", "https://")):
                return target
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if is_google_host(parsed.netloc.lower()):
        return None
    return absolute


def _thumb_near(anchor, page_url: str) -> Optional[str]:
    """The preview image belonging to a result, if the page carries one.

    Looks inside the anchor first, then at its containing block - Google
    puts the thumbnail in either place depending on the layout.
    """
    for scope in (anchor, anchor.parent, getattr(anchor.parent, "parent", None)):
        if scope is None:
            continue
        img = scope.find("img")
        src = str(img.get("src") or img.get("data-src") or "").strip() if img else ""
        if src.startswith("data:"):
            continue
        if src:
            return urljoin(page_url or GOOGLE_URL, src)
    return None


def is_google_host(host: str) -> bool:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if GOOGLE_DOMAIN_RE.search(host):
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in GOOGLE_HOST_SUFFIXES)


def host_label(url: str) -> Optional[str]:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _unescape(literal: str) -> str:
    """JSON-escaped slashes and unicode escapes back to plain text.

    Done by hand rather than with json.loads: these literals are pulled
    out of a much larger blob one at a time, and a single one that
    happens to carry an escape json cannot parse must not throw away
    every other result on the page.
    """
    text = literal.replace("\\/", "/").replace('\\"', '"')

    def _replace(match):
        try:
            return chr(int(match.group(1), 16))
        except ValueError:
            return match.group(0)

    return re.sub(r"\\u([0-9a-fA-F]{4})", _replace, text)
