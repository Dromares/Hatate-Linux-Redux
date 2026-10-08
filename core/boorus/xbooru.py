"""xbooru.com - Gelbooru-family software, so tag-list markup is reused.

Own module for the same reason as rule34.py: Gelbooru's Data API 401s
without credentials and must not poison this host's still-public API.
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

log = get_logger("boorus.xbooru")

HOSTS = ("xbooru.com",)
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
        f"https://xbooru.com/index.php"
        f"?page=dapi&s=post&q=index&json=1&id={match.group(1)}"
    )
    try:
        resp = net.get(api_url, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        log.debug("xbooru Data API request failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        log.debug("xbooru Data API returned HTTP %d for %s", resp.status_code, url)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return gelbooru.post_from_dapi(data)
