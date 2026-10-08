"""rule34.xxx - Gelbooru-family software, so tag-list markup is reused.

Kept as its own module (not added to Gelbooru's host list) because
Gelbooru's Data API now 401s without credentials and remembers that for
the rest of the session; sharing that flag would skip rule34's still-
public API after the first Gelbooru 401.
"""
from __future__ import annotations

import re
from typing import List, Optional

import requests

from .. import net

from ..applog import get_logger
from ..models import Tag
from . import gelbooru
from ._host import on_host

log = get_logger("boorus.rule34")

HOSTS = ("rule34.xxx",)
USER_AGENT = net.USER_AGENT
POST_ID_RE = re.compile(r"[?&]id=(\d+)")

_last_api_post: Optional[tuple] = None


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def parse(html: str, url: str) -> List[Tag]:
    tags = gelbooru.parse(html, url)
    if not tags:
        log.debug("No tags extracted from %s (markup may differ from Gelbooru's)", url)
    return tags


def parse_rating(html: str, url: str) -> Optional[str]:
    """The Statistics sidebar's "Rating:" line - identical markup to
    Gelbooru's, and read from the page so it costs no extra request."""
    return gelbooru.parse_rating(html, url)


def parse_file_url(html: str, url: str) -> Optional[str]:
    post = _get_api_post(url)
    if post and post.get("file_url"):
        return post["file_url"]
    return gelbooru.parse_file_url_from_html(html, url)


def parse_preview_url(html: str, url: str) -> Optional[str]:
    post = _get_api_post(url)
    if post and post.get("sample_url"):
        return post["sample_url"]
    return gelbooru.parse_preview_url_from_html(html, url)


def parse_dimensions(html: str, url: str):
    post = _get_api_post(url)
    if post and post.get("width") and post.get("height"):
        try:
            return int(post["width"]), int(post["height"])
        except (TypeError, ValueError):
            pass
    return gelbooru.parse_dimensions_from_html(html, url)


def _get_api_post(url: str) -> Optional[dict]:
    global _last_api_post
    if _last_api_post and _last_api_post[0] == url:
        return _last_api_post[1]
    post = _fetch_post_from_api(url)
    _last_api_post = (url, post)
    return post


def _fetch_post_from_api(url: str) -> Optional[dict]:
    match = POST_ID_RE.search(url)
    if not match:
        return None
    api_url = (
        f"https://api.rule34.xxx/index.php"
        f"?page=dapi&s=post&q=index&json=1&id={match.group(1)}"
    )
    try:
        resp = net.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        log.debug("rule34 Data API request failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.debug("rule34 Data API returned HTTP %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return gelbooru.post_from_dapi(data)


# -- finding a post from a search Google indexed ---------------------------
#
# Google Lens's Exact matches tab sometimes lists a rule34.xxx post by the
# SEARCH it was reached through rather than by the post: the page title is
# "Rule 34 / parent:10755759" when the URL Google indexed was
# ...&id=10755716&tags=parent:10755759. The post id is in neither the title
# nor anything else Lens sends. CONFIRMED on a real image: the post was the
# third exact match and unrecoverable, because every other rule34 title
# form carries the id and this one does not.
#
# The search itself is still there to run, though, and the post is one of
# its results. rule34's Data API now answers "Missing authentication", so
# this reads the public listing page - the same one a browser gets.

LISTING_URL = "https://rule34.xxx/index.php"
_LISTING_POST_RE = re.compile(r'<a id="p(\d+)"[^>]*>\s*<img src="([^"]+)"')
POST_URL = "https://rule34.xxx/index.php?page=post&s=view&id={}"

# A 16x16 dhash, because the 8x8 one cannot tell variants apart. MEASURED
# on the case above: the parent and two sibling variants (the same scene
# edited) all hashed to distance 0 at 8x8. At 16x16 the right post was 2
# out of 256 and the nearest variant 9; a different picture was 20+.
LISTING_HASH_SIZE = 16
LISTING_MAX_DISTANCE = 6
LISTING_THUMB_WORKERS = 5


def listing_posts(html: str) -> List[tuple]:
    """(post id, thumbnail URL) for each post on a listing page."""
    return _LISTING_POST_RE.findall(html or "")


def find_post_by_search(tags: str, local_hash: Optional[int],
                        timeout: float = 15.0) -> Optional[tuple]:
    """(post URL, thumbnail URL, distance) of the post on the first page of
    this search that is the same picture as `local_hash`, or None.

    `local_hash` is a dhash at LISTING_HASH_SIZE. Only the first page is
    read - one request and a page of small thumbnails - which covers the
    precise searches (parent:, id:) this exists for.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..image_compare import dhash

    if local_hash is None or not (tags or "").strip():
        return None
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = net.get(LISTING_URL, headers=headers, timeout=timeout,
                       params={"page": "post", "s": "list", "tags": tags.strip()})
    except requests.RequestException as exc:
        log.debug("rule34 listing for %r failed: %s", tags, exc)
        return None
    if resp.status_code != 200:
        log.debug("rule34 listing for %r returned HTTP %d", tags, resp.status_code)
        return None
    posts = listing_posts(resp.text)
    if not posts:
        return None

    def distance(post):
        post_id, thumb = post
        try:
            data = net.get(thumb, headers=headers, timeout=timeout).content
        except requests.RequestException:
            return None
        other = dhash(data, LISTING_HASH_SIZE)
        if other is None:
            return None
        return bin(local_hash ^ other).count("1"), post_id, thumb

    with ThreadPoolExecutor(max_workers=LISTING_THUMB_WORKERS) as pool:
        scored = [r for r in pool.map(distance, posts) if r is not None]
    if not scored:
        return None
    best_distance, post_id, thumb = min(scored)
    log.debug("rule34 listing %r: %d post(s), closest is %s at distance %d",
              tags, len(posts), post_id, best_distance)
    if best_distance > LISTING_MAX_DISTANCE:
        return None
    return POST_URL.format(post_id), thumb, best_distance
