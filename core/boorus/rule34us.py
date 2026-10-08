"""rule34.us - its own software, unrelated to rule34.xxx despite the name.

Reached through Google Lens: neither IQDB nor SauceNAO indexes it, but
Lens's "Exact matches" tab names its posts, and the URL is rebuilt as
index.php?r=posts/view&id={id} (see core/google_lens.py).

Without this parser such a match had no image at all but the one Lens
paired with it - and Lens's tile can be a 1x1 lazy-load placeholder,
which made the side-by-side comparison show a blank. Everything worth
having is on the post page, no API needed:

  * Tags are links to the tag's listing, r=posts/index&q=<tag>, inside
    an <li class="{artist,copyright,character,metadata,general}-tag">.
    The q parameter is the canonical underscored name; the link text is
    the display form with spaces. The same li classes are also used for
    the post's info box ("Id: ...", "Original"), which is why only a li
    holding a tag-listing link counts as a tag.
  * The original is the "Original" link in that info box; the page's
    main <img> (or <video>) is the same file, used as a fallback.
  * "Size: 2039w x 2894h" states the dimensions, for videos too.

No rating comes from here, and that is a measured result rather than an
omission. The other Danbooru 1.x descendants spell the rating out in the
same Statistics block this parser already walks for "Size:" (see
core/boorus/moebooru.py and core/boorus/gelbooru.py), so rule34.us looks
like it should too - it does not. CHECKED against live posts 6000000,
5000000 and 4000000: the block holds Id, Added by, Created, Score and
Size, full stop. The only occurrence of the string "rating" on the page
is a static <meta name="rating" content="adult" /> in <head>, served
byte-identically on every page of the site - boilerplate about the site,
not a statement about the post. Reading it would stamp the same word on
every rule34.us match while looking like per-post fact, which is exactly
what core/boorus/_rating.py refuses to do.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import parse_qs, urljoin, urlparse


from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import soup_of

log = get_logger("boorus.rule34us")

HOSTS = ("rule34.us",)

CATEGORY_MAP = {
    "artist-tag": "artist",
    "copyright-tag": "copyright",
    "character-tag": "character",
    "metadata-tag": "meta",
    "general-tag": "general",
}

SIZE_RE = re.compile(r"Size:\s*(\d+)\s*w\s*x\s*(\d+)\s*h", re.IGNORECASE)
MEDIA_HOST_RE = re.compile(r"^https?://[^/]*rule34\.us/(?:images|videos)/", re.IGNORECASE)


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def parse(html: str, url: str) -> List[Tag]:
    soup = soup_of(html)
    tags: List[Tag] = []
    seen = set()
    for item in soup.select("li"):
        namespace = next((CATEGORY_MAP[c] for c in (item.get("class") or [])
                          if c in CATEGORY_MAP), None)
        if namespace is None:
            continue
        anchor = item.find("a", href=True)
        name = _tag_from_href(str(anchor.get("href"))) if anchor else None
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        tags.append(Tag(name=name, source=TagSource.BOORU, namespace=namespace))
    if not tags:
        log.debug("No tags extracted from %s (markup may have changed)", url)
    return tags


def parse_file_url(html: str, url: str) -> Optional[str]:
    soup = soup_of(html)
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "")
        if anchor.get_text(strip=True).lower() == "original" and MEDIA_HOST_RE.match(href):
            return href
    for media in soup.select("video source[src], video[src], img[src]"):
        src = urljoin(url, str(media.get("src") or ""))
        if MEDIA_HOST_RE.match(src):
            return src
    return None


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """The page shows the original at full size and offers no sample, so
    the original is the preview. Returning None would leave the GUI with
    the search engine's thumbnail - which for a Lens match can be a
    blank placeholder."""
    return parse_file_url(html, url)


def parse_dimensions(html: str, url: str):
    text = soup_of(html).get_text(" ")
    found = SIZE_RE.search(text)
    if not found:
        return None, None
    return int(found.group(1)), int(found.group(2))


def _tag_from_href(href: str) -> Optional[str]:
    query = parse_qs(urlparse(href).query)
    if query.get("r", [""])[0] != "posts/index":
        return None
    name = (query.get("q") or [""])[0].strip()
    return name or None
