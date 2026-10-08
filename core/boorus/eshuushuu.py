"""e-shuushuu.

The site was rebuilt at some point after this app's booru parsers were
first written, so nothing here matches the old e-shuushuu markup that
tutorials and older scrapers describe. What the current site actually
serves, verified against live pages:

- Post pages live at /images/{id} (plural). The older /image/{id} form
  still appears in some search-engine results; we match on host so both
  reach this parser either way.
- Every tag on a post page is a link to /search?tags={tag_id}, followed
  by a separate "jump to tag page" link to /tags/{tag_id}.
- The full-resolution file is a direct link to
  cdn.e-shuushuu.net/fullsize/{date}-{id}.{ext}, and the thumbnail is
  the same path with /thumbs/ and a .webp extension.

Selectors here key off those URL PATTERNS rather than CSS classes or
element ids. That's deliberate: a restyle changes class names far more
often than it changes route shapes, and this site has already been
rebuilt once. `a[href*="/search?tags="]` keeps working across a
redesign in a way that `.tag_list li` would not.

NAMESPACES. e-shuushuu classifies its tags (Artist / Source / Character
/ Theme) but the post page's tag links are all the same shape, with no
per-tag type marker in the markup. The classification is recoverable
from the page's own og:description, which the site generates in a
consistent form:

    Cute anime artwork by <ARTIST> from <SOURCE>. 800×600. Tagged: <THEMES>
    Cute anime artwork by <ARTIST> of <CHARACTER>. 984×1392. Tagged: <THEMES>

Rather than parse that prose into names - which would break on any
artist called "X of Y", on multi-word sources containing commas, and on
posts with several characters - the tag NAMES are taken from the links
(authoritative and complete), and the description is only consulted to
ask where each already-known name sits: inside the "Tagged:" run, or
immediately after "by" / "of" / "from". Anything that doesn't match one
of those positions is left unnamespaced rather than guessed at, because
a wrong namespace is worse than none: it would file a character under
creator: in Hydrus, where it is far more annoying to find and undo than
a merely flat tag.

Verified against two live posts whose correct classification is known
independently from e-shuushuu's own Atom feed (which does expose tag
types, but only in per-site and per-tag feeds - there is no per-image
feed, so it can't be used for a single lookup here).
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ._sizes import BARE_DIMENSIONS_RE
from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import soup_of

log = get_logger("boorus.eshuushuu")

HOSTS = ("e-shuushuu.net",)

# "Dimensions: 800 × 600" on the post page. Matched against the page's
# flattened text (get_text(" ")) rather than a specific element, so the
# label and value being in separate tags doesn't matter. Accepts the
# real multiplication sign as well as a plain "x".
DIMENSIONS_RE = re.compile(r"Dimensions:\s*([\d,]+)\s*[×x]\s*([\d,]+)", re.IGNORECASE)

# Where a tag sits in og:description -> the namespace the rest of the
# app uses. Matches core/boorus/zerochan.py, which maps the same
# Artist/Source/Character vocabulary onto creator/series/character.
ROLE_NAMESPACES = {
    "by": "creator",
    "of": "character",
    "from": "series",
}

# The run of comma-separated theme tags at the end of og:description.
TAGGED_SPLIT_RE = re.compile(r"\bTagged:\s*", re.IGNORECASE)


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def _description(soup: BeautifulSoup) -> str:
    meta = (soup.select_one('meta[property="og:description"]')
            or soup.select_one('meta[name="description"]'))
    return (meta.get("content") or "") if meta else ""  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute


def _namespace_for(name: str, head: str, themes: set) -> Optional[str]:
    """Which namespace a known tag name belongs to, judged only by where
    that exact name appears in og:description. Returns None when the
    name isn't found in any recognised position - deliberately, so an
    unfamiliar description format degrades to flat tags rather than
    mislabelled ones."""
    if name.casefold() in themes:
        # Checked first: a theme that happens to also read like a role
        # ("of" a character named the same as a theme word) should stay
        # a theme, since the Tagged: run is the unambiguous signal.
        return None

    for preposition, namespace in ROLE_NAMESPACES.items():
        # The name must directly follow the preposition AND end at a
        # real boundary - sentence end, comma, or the next preposition -
        # so "by Fagi" can't also match an artist called "Fagi Something".
        pattern = (
            r"\b" + preposition + r"\s+" + re.escape(name)
            + r"(?=$|[.,]|\s+(?:by|of|from)\b)"
        )
        if re.search(pattern, head, re.IGNORECASE):
            return namespace
    return None


def parse(html: str, url: str) -> List[Tag]:
    soup = soup_of(html)

    # Each tag links to its search page. The adjacent /tags/{id} link is
    # a "go to tag page" affordance, not a second tag, so it's excluded
    # here - including it would duplicate every tag under a numeric name.
    anchors = soup.select('a[href*="search?tags="]')
    if not anchors:
        log.debug("No tag links found on %s (markup may have changed)", url)
        return []

    description = _description(soup)
    parts = TAGGED_SPLIT_RE.split(description, maxsplit=1)
    head = parts[0]
    themes = set()
    if len(parts) > 1:
        themes = {t.strip().rstrip(".").casefold() for t in parts[1].split(",") if t.strip()}

    tags: List[Tag] = []
    seen = set()
    namespaced = 0
    for a in anchors:
        name = a.get_text(strip=True)
        if not name:
            continue
        # The page also carries navigational links to the same route (a
        # tag sidebar, "related tags", etc). Deduplicate by name so a tag
        # listed twice on the page only becomes one Tag.
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)

        namespace = _namespace_for(name, head, themes)
        if namespace:
            namespaced += 1
        # Underscored to match every other parser in this package, so a
        # tag from here dedupes against the same tag from another site.
        tags.append(Tag(name=name.replace(" ", "_"), source=TagSource.BOORU, namespace=namespace))

    if tags and not namespaced and description:
        log.debug(
            "No tag on %s could be classified from its description - e-shuushuu may have "
            "changed its og:description format; tags are still returned, just unnamespaced", url,
        )
    return tags


def parse_file_url(html: str, url: str) -> Optional[str]:
    """The full-resolution file, linked directly from the post page as
    cdn.e-shuushuu.net/fullsize/... - not to be confused with the
    /thumbs/ URL, which is the small webp preview."""
    soup = soup_of(html)

    link = soup.select_one('a[href*="/fullsize/"]')
    if link and link.get("href"):
        return _absolute(link["href"])  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

    # Some layouts show the full image inline rather than as a link.
    img = soup.select_one('img[src*="/fullsize/"]')
    if img and img.get("src"):
        return _absolute(img["src"])  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

    log.debug("Could not find a /fullsize/ file URL on %s", url)
    return None


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """The CDN's own webp thumbnail. Worth using as the preview here
    because e-shuushuu's full-resolution files can be very large (tens
    of megabytes), so pulling the original just to render a preview
    would be wasteful."""
    soup = soup_of(html)

    meta = soup.select_one('meta[property="og:image"]')
    if meta and meta.get("content"):
        return _absolute(meta["content"])  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

    img = soup.select_one('img[src*="/thumbs/"]')
    if img and img.get("src"):
        return _absolute(img["src"])  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    return None


def parse_dimensions(html: str, url: str):
    soup = soup_of(html)
    match = DIMENSIONS_RE.search(soup.get_text(" "))
    if match:
        try:
            return int(match.group(1).replace(",", "")), int(match.group(2).replace(",", ""))
        except ValueError:
            return None, None

    # The site's redesign moved the word "Dimensions" out of the text and
    # into a class name, leaving just "640 × 480" on its own - so the
    # label-anchored pattern above stopped matching and every match
    # arrived with no size at all. Read the element itself instead.
    element = soup.select_one('[class*="dimensions"]')
    if element:
        found = BARE_DIMENSIONS_RE.search(element.get_text(" "))
        if found:
            try:
                return int(found.group(1)), int(found.group(2))
            except ValueError:
                pass
    return None, None


def _absolute(href: str) -> str:
    """The CDN links are absolute already, but protocol-relative
    ("//cdn...") URLs would otherwise be handed downstream as-is and fail
    to fetch, so normalize them to https."""
    href = unquote(href.strip()) if "%" in href else href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://e-shuushuu.net" + href
    return href
