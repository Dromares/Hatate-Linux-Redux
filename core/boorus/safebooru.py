from __future__ import annotations

from typing import List, Optional

from ..applog import get_logger
from ..models import Tag
from . import gelbooru
from ._host import on_host

log = get_logger("boorus.safebooru")

HOST = "safebooru.org"


def matches(url: str) -> bool:
    return on_host(url, (HOST,))


def parse(html: str, url: str) -> List[Tag]:
    """Safebooru runs on the same underlying codebase lineage as Gelbooru,
    so its tag-list markup follows the same structure - reuse that parser
    rather than duplicate it. Kept as a separate module (not just added to
    Gelbooru's host list) so it can diverge independently if Safebooru's
    markup ever differs."""
    tags = gelbooru.parse(html, url)
    if not tags:
        log.debug("No tags extracted from %s (markup may differ from Gelbooru's)", url)
    return tags


def parse_rating(html: str, url: str) -> Optional[str]:
    """The Statistics sidebar's "Rating:" line - the same shared
    Danbooru 1.x markup the size scrape already reads."""
    return gelbooru.parse_rating(html, url)


def parse_file_url(html: str, url: str) -> Optional[str]:
    return gelbooru.parse_file_url(html, url)


def parse_dimensions(html: str, url: str):
    """The ORIGINAL's size, off the Statistics sidebar's "Size: WxH".

    Delegated to the HTML-only Gelbooru helper, not the API-first one:
    that path shares Gelbooru's 401 latch, and Safebooru's own API is
    still public. This was simply missing - the page states the size
    plainly, but nothing read it, so every Safebooru match arrived with
    no dimensions and the "is this an upgrade?" comparison had nothing
    to work with.
    """
    return gelbooru.parse_dimensions_from_html(html, url)


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """The post page's own #image element - Safebooru's sample where it
    made one, and the original where it didn't.

    This was simply missing, so every Safebooru match fell back to the
    search engine's ~150px thumbnail and looked blurry, even though the
    page already in hand names a much better picture. Read from the HTML
    rather than the Data API so it costs no extra request.
    """
    return gelbooru.parse_preview_url_from_html(html, url)
