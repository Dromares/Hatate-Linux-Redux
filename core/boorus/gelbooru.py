from __future__ import annotations

import re
from typing import List, Optional

import requests

from .. import net

from ..applog import get_logger
from ._sizes import describe_size_context, parse_size_label
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import soup_of
from ._rating import from_label as rating_from_label

log = get_logger("boorus.gelbooru")

HOSTS = ("gelbooru.com",)
USER_AGENT = net.USER_AGENT

CATEGORY_MAP = {
    "tag-type-general": "general",
    "tag-type-artist": "artist",
    "tag-type-copyright": "copyright",
    "tag-type-character": "character",
    "tag-type-metadata": "meta",
}

POST_ID_RE = re.compile(r"[?&]id=(\d+)")

# Tiny cache so parse_file_url and parse_preview_url - called back-to-back
# for the same URL by fetch_page_info - share one API call instead of two.
# Deliberately just the single most recent entry: these two calls always
# happen immediately after each other for the same post, never interleaved
# with a different post's lookup.
_last_api_post: Optional[tuple] = None  # (url, post_dict_or_None)

# Gelbooru's Data API now requires an api_key and user_id; without them
# it answers 401 to everything. Once that's been seen there is no point
# asking again this session - it's a guaranteed round trip to a known
# refusal before every single HTML fallback. Reset on restart, so
# adding credentials later just works.
_api_unauthorized = False


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


# A deleted post does not 404 here. CONFIRMED against the live site:
# gelbooru.com/index.php?page=post&s=view&id=6352196 answers HTTP 200
# after redirecting to the post LIST (s=list&tags=all) - and that list
# page carries a tag sidebar of its own, in the same markup this parser
# looks for. So a deleted post yielded 54 tags belonging to whatever was
# on the front page, byte-identical to what the list page produces, and
# they would have been written into the user's library as if they
# described their picture.
#
# A listing is told from a post by two things together, MEASURED across
# Gelbooru, rule34.xxx, Safebooru and Xbooru: a real post page carries
# its own image and NO thumbnail grid, while the listing carries a grid
# of dozens and no post image.
LISTING_THUMB_MARKER = "thumbnail-preview"
POST_MEDIA_MARKERS = ('id="image"', 'id="gelcomVideoPlayer"')
MIN_LISTING_THUMBS = 2


def looks_like_a_listing(html: str) -> bool:
    """Whether this body is a post LIST rather than a post.

    Deliberately needs both halves. Requiring only "no post image" would
    call every page whose markup shifted a listing and silently drop its
    tags; requiring only the grid would trip on a post page that happened
    to show related thumbnails.
    """
    body = html or ""
    if any(marker in body for marker in POST_MEDIA_MARKERS):
        return False
    return body.count(LISTING_THUMB_MARKER) >= MIN_LISTING_THUMBS


def parse(html: str, url: str) -> List[Tag]:
    if looks_like_a_listing(html):
        log.info("%s answered with a post LIST rather than the post - it has been "
                 "deleted, so its 'tags' would belong to other posts", url)
        return []
    soup = soup_of(html)
    tags: List[Tag] = []
    # Gelbooru itself calls this list #tag-list, but its relatives -
    # Safebooru, rule34 and Xbooru, which all delegate their tag parsing
    # here - call it #tag-sidebar. Checking only for #tag-list meant those
    # three extracted NO tags at all, which is most of the point of
    # matching them. The li markup inside is identical on all four.
    container = (
        soup.select_one("ul#tag-list") or soup.select_one("div#tag-list")
        or soup.select_one("ul#tag-sidebar") or soup.select_one("div#tag-sidebar")
    )
    if not container:
        log.debug("No tag-list/tag-sidebar container found on %s (markup may have changed)", url)
        return tags

    for li in container.select("li"):
        classes = li.get("class") or []  # type: ignore[var-annotated]  # bs4 stub: Tag.get() typed str | list[str] | None regardless of attribute
        namespace = next((CATEGORY_MAP[c] for c in classes if c in CATEGORY_MAP), "general")
        name_el = li.select_one("a[href*='page=post']") or li.select_one("a")
        if not name_el:
            continue
        name = name_el.get_text(strip=True).replace(" ", "_")
        if name:
            tags.append(Tag(name=name, source=TagSource.BOORU, namespace=namespace))
    return tags


def parse_rating(html: str, url: str) -> Optional[str]:
    """The post's rating, off the Statistics sidebar's "Rating:" line.

    Read from the page rather than the Data API on purpose: the page is
    already in hand, the API needs credentials this build does not have
    (see the 401 latch below), and the sidebar spells the rating out in
    words - no per-site letter table to get wrong.

    A post LIST carries a sidebar of its own, so it is refused for the
    same reason parse() refuses it: the rating would describe whatever
    is on the front page, not the user's picture.
    """
    if looks_like_a_listing(html):
        return None
    return parse_rating_from_html(html, url)


def parse_rating_from_html(html: str, url: str) -> Optional[str]:
    """The HTML-only helper, shared with the family parsers that must
    not route through this module's Data-API latch."""
    rating = rating_from_label(soup_of(html).get_text(" "))
    if rating is None:
        log.debug("No 'Rating:' line found on %s (markup may have changed)", url)
    return rating


def parse_file_url(html: str, url: str) -> Optional[str]:
    """Gelbooru (and Safebooru, which runs compatible software and
    delegates here) has an official public Data API that returns the
    exact direct file URL - prefer that over guessing at page markup,
    since a wrong guess here previously caused format/size checks to
    HEAD the HTML post page itself (reporting "HTML" as the format)
    rather than the actual image. Falls back to scraping the already-
    fetched page if the API call doesn't pan out for any reason (rate
    limited, post ID not found in the URL, etc.) - never worse than the
    old behavior."""
    post = _get_api_post(url)
    if post and post.get("file_url"):
        return post["file_url"]
    return parse_file_url_from_html(html, url)


def parse_file_url_from_html(html: str, url: str) -> Optional[str]:
    """HTML-only fallback, used by Gelbooru-family parsers that must not
    share this module's Data-API 401 latch (rule34, xbooru)."""
    soup = soup_of(html)

    original_link = soup.find("a", string=lambda s: s and "original image" in s.lower())
    if original_link and original_link.get("href"):
        return original_link["href"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

    img = soup.select_one("#image")
    if img and img.get("src"):
        return img["src"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    log.debug("Could not find any direct file URL on %s (API and HTML scrape both failed)", url)
    return None


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """The Data API's sample_url is a mid-resolution downscaled version
    Gelbooru itself generates for display - sharper than the search
    engine's tiny thumbnail, much smaller than the original. Falls back
    to the on-page #image src (usually the same sample) if the API
    didn't come through."""
    post = _get_api_post(url)
    if post and post.get("sample_url"):
        return post["sample_url"]
    return parse_preview_url_from_html(html, url)


def parse_preview_url_from_html(html: str, url: str) -> Optional[str]:
    soup = soup_of(html)
    img = soup.select_one("#image")
    if img and img.get("src"):
        return img["src"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    return None


# The Statistics sidebar on a post page lists the ORIGINAL file's
# dimensions as "Size: 1152x1440" - verified against a live post. Worth
# parsing because the Data API needs credentials this build doesn't have
# and answers 401, so without this fallback every Gelbooru match arrives
# with no dimensions at all: nothing to compare the local file against,
# and no way to tell whether the match is an upgrade.
#
# Deliberately anchored to the "Size:" label rather than hunting for any
# NxN in the page - a booru page is full of numbers (tag counts, ids,
# scores), and a looser pattern would happily match one of those.
# Size parsing is shared with the other Danbooru 1.x descendants -
# see core/boorus/_sizes.py for why it lives in one place.


def parse_dimensions(html: str, url: str):
    """The ORIGINAL image's dimensions - not the sample's.

    The distinction matters: the page displays a downscaled sample, so
    reading the dimensions off the visible <img> would report the sample
    size and make a genuinely larger match look like a downgrade.
    """
    post = _get_api_post(url)
    if post and post.get("width") and post.get("height"):
        return post["width"], post["height"]

    return parse_dimensions_from_html(html, url)


def parse_dimensions_from_html(html: str, url: str):
    soup = soup_of(html)
    text = soup.get_text(" ")
    width, height = parse_size_label(text)
    if width and height:
        return width, height
    log.debug(
        "No 'Size: WxH' found on %s (markup may have changed). Context: %r",
        url, describe_size_context(text),
    )
    return None, None


def post_from_dapi(data) -> Optional[dict]:
    """Pulls the first post dict out of a Gelbooru-family Data API body.

    Gelbooru wraps it as `{"post": [ {...} ]}` (or `{"post": {...}}`).
    rule34.xxx and xbooru return a bare list of posts. A caller that
    only handled the wrapped form silently got nothing from those two
    even when the API answered 200 with a perfectly good record.
    """
    if isinstance(data, list):
        post = data[0] if data else None
        return post if isinstance(post, dict) else None
    if not isinstance(data, dict):
        return None
    post = data.get("post")
    if isinstance(post, list):
        post = post[0] if post else None
    return post if isinstance(post, dict) else None


def _get_api_post(url: str) -> Optional[dict]:
    global _last_api_post
    if _last_api_post and _last_api_post[0] == url:
        return _last_api_post[1]
    post = _fetch_post_from_api(url)
    _last_api_post = (url, post)
    return post


def _fetch_post_from_api(url: str) -> Optional[dict]:
    global _api_unauthorized
    if _api_unauthorized:
        return None  # already refused once this session; go straight to HTML

    match = POST_ID_RE.search(url)
    if not match:
        log.debug("No post ID found in %s, can't query the Data API", url)
        return None

    host = _host_of(url) or "gelbooru.com"
    api_url = (
        f"https://{host}/index.php"
        f"?page=dapi&s=post&q=index&json=1&id={match.group(1)}"
    )
    try:
        resp = net.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        log.debug("Data API request failed for %s: %s (falling back to HTML scrape)", url, exc)
        return None
    if resp.status_code in (401, 403):
        # Not a per-post problem - the API needs credentials this build
        # doesn't have, so every future call would fail identically.
        _api_unauthorized = True
        log.info(
            "%s's Data API returned HTTP %d (it needs an api_key/user_id) - using page "
            "scraping for the rest of this session instead of retrying it per match",
            _host_of(url) or "Gelbooru", resp.status_code,
        )
        return None
    if resp.status_code != 200:
        log.debug("Data API returned HTTP %d for %s", resp.status_code, url)
        return None

    try:
        data = resp.json()
    except ValueError:
        log.debug("Data API response for %s was not valid JSON", url)
        return None

    post = post_from_dapi(data)
    if post:
        return post

    log.debug("Data API response for %s had no post data", url)
    return None


def _host_of(url: str) -> Optional[str]:
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None
