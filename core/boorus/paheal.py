"""rule34.paheal.net - Shimmie2, so none of the Gelbooru-family parsers fit.

Reached through Google Lens rather than IQDB or SauceNAO: neither of
those indexes Paheal, but Lens's "Exact matches" tab names its posts, so
matches arrive here with a rebuilt /post/view/{id} URL. See
core/google_lens.py for how that reconstruction works.

Everything worth having is on the post page and needs no API:

  * Tags are `<a class="tag_name" href="/post/list/Ben_10/1">Ben 10</a>`.
    The HREF is used rather than the link text, because the text is the
    display form with spaces ("Gwen Tennyson") while the href carries
    the site's own canonical underscored name ("Gwen_Tennyson") - which
    is the form every other parser here produces and the one Hydrus
    expects.
  * The original file, its dimensions and its media type are all
    attributes of `#main_image`, so no second request is needed to learn
    the size of the match.

Paheal has no tag categories - its tags are flat - so nothing here is
namespaced. That is the site being simple, not a parser gap.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import unquote, urljoin


from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import soup_of

log = get_logger("boorus.paheal")

HOSTS = ("rule34.paheal.net", "paheal.net")

# "/post/list/Gwen_Tennyson/1" -> "Gwen_Tennyson". The trailing page
# number is part of the route, not the tag.
TAG_HREF_RE = re.compile(r"/post/list/([^/]+)/\d+\s*$")

# Site navigation that uses the same route as a tag link. These are
# Shimmie's own listing shortcuts, not tags on this post.
NON_TAGS = {"", "all", "any", "none"}

MIME_TO_FORMAT = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
    "video/webm": "WEBM",
    "video/mp4": "MP4",
}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def parse(html: str, url: str) -> List[Tag]:
    soup = soup_of(html)
    tags: List[Tag] = []
    seen = set()

    for anchor in soup.select("a.tag_name[href]"):
        name = _tag_from_href(str(anchor.get("href") or ""))
        if not name:
            # Fall back to the visible text, which differs only in using
            # spaces where the canonical name uses underscores.
            name = anchor.get_text(strip=True).replace(" ", "_")
        key = name.lower()
        if not name or key in NON_TAGS or key in seen:
            continue
        seen.add(key)
        tags.append(Tag(name=name, source=TagSource.BOORU, namespace=""))

    if not tags:
        log.debug("No tags extracted from %s (markup may have changed)", url)
    return tags


def parse_file_url(html: str, url: str) -> Optional[str]:
    """The original file, which lives on Paheal's CDN.

    Its URL has no extension - the type is stated in data-mime instead -
    so nothing downstream should infer the format from the path.
    """
    image = _main_image(html)
    src = str(image.get("src") or "").strip() if image else ""
    return urljoin(url, src) if src else None


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """Paheal serves one image at full size and no sample, so the
    original is the preview. Returning None instead would make the GUI
    fall back to the search engine's thumbnail."""
    return parse_file_url(html, url)


def parse_dimensions(html: str, url: str):
    image = _main_image(html)
    if not image:
        return None, None
    try:
        return int(str(image.get("data-width"))), int(str(image.get("data-height")))
    except (TypeError, ValueError):
        return None, None


def parse_file_info(html: str, url: str):
    """(format, size_bytes) as the page states them.

    Only the format is taken from here. Paheal prints the size rounded
    to whole KB ("262KB" for 268,096 bytes), and a size that is wrong by
    a few thousand bytes is worse than no size at all - it would be
    compared against the local file's exact one. The size is left to the
    HEAD request, which the CDN answers correctly.
    """
    image = _main_image(html)
    mime = str(image.get("data-mime") or "").strip().lower() if image else ""
    return MIME_TO_FORMAT.get(mime), None


def _main_image(html: str):
    soup = soup_of(html)
    return soup.select_one("#main_image") or soup.select_one("img.shm-main-image")


def _tag_from_href(href: str) -> Optional[str]:
    match = TAG_HREF_RE.search(href or "")
    if not match:
        return None
    return unquote(match.group(1)).strip()
