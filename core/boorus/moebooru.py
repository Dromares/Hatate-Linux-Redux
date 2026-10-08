from __future__ import annotations

import re
from typing import List, Optional


from ..applog import get_logger
from ..models import Tag, TagSource
from ._sizes import describe_size_context, parse_bare_dimensions, parse_size_label
from ._host import on_host
from ._parsed import soup_of
from ._rating import from_label as rating_from_label

log = get_logger("boorus.moebooru")

HOSTS = ("yande.re", "konachan.com", "konachan.net")

# The Statistics sidebar, which is where both "Size: WxH" and
# "Rating: Safe" live. Named as a selector rather than scraped out of
# the whole page for the reason spelled out in parse_rating.
STATS_SELECTOR = "#stats"

# Moebooru embeds the post's own record in a Post.register(...) call.
# Matching on the QUOTED key is what keeps this off the sample: the
# page also carries "sample_width" and "actual_preview_width", and
# neither has a quote immediately before "width", so neither can match.
JSON_WIDTH_RE = re.compile(r'"width"\s*:\s*(\d+)')
JSON_HEIGHT_RE = re.compile(r'"height"\s*:\s*(\d+)')

CATEGORY_MAP = {
    "tag-type-general": "general",
    "tag-type-artist": "artist",
    "tag-type-copyright": "copyright",
    "tag-type-character": "character",
    "tag-type-circle": "circle",
}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def parse(html: str, url: str) -> List[Tag]:
    soup = soup_of(html)
    tags: List[Tag] = []
    container = soup.select_one("ul#tag-sidebar")
    if not container:
        log.debug("No #tag-sidebar container found on %s (markup may have changed)", url)
        return tags

    for li in container.select("li"):
        classes = li.get("class") or []  # type: ignore[var-annotated]  # bs4 stub: Tag.get() typed str | list[str] | None regardless of attribute
        namespace = next((CATEGORY_MAP[c] for c in classes if c in CATEGORY_MAP), "general")
        name_el = li.select_one("a[href*='/post?tags=']") or li.select_one("a")
        if not name_el:
            continue
        name = name_el.get_text(strip=True).replace(" ", "_")
        if name:
            tags.append(Tag(name=name, source=TagSource.BOORU, namespace=namespace))
    return tags


def parse_file_url(html: str, url: str) -> Optional[str]:
    """Moebooru sites (Yande.re/Konachan) link the full-resolution file
    from an anchor commonly id'd 'highres' in the sidebar; fall back to
    the on-page #image element (may be a resized 'sample') if that's
    not present."""
    soup = soup_of(html)

    highres = soup.select_one("#highres")
    if highres and highres.get("href"):
        return highres["href"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute

    img = soup.select_one("#image")
    if img and img.get("src"):
        return img["src"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    log.debug("Could not find any direct file URL on %s", url)
    return None


def parse_preview_url(html: str, url: str) -> Optional[str]:
    """The on-page #image element is the resized 'sample' Moebooru shows
    by default - a good mid-resolution preview, reusing the same HTML
    already fetched for tags/file_url."""
    soup = soup_of(html)
    img = soup.select_one("#image")
    if img and img.get("src"):
        return img["src"]  # type: ignore[return-value]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
    return None


def parse_rating(html: str, url: str) -> Optional[str]:
    """The post's rating, off the Statistics sidebar's "Rating:" line -
    the same block the "Size: WxH" scrape below already walks.

    Read from the sidebar rather than from the embedded Post.register
    record, because the page registers a record for every RELATED post
    as well as this one and each carries its own "rating" - there is no
    way to tell from the JSON which one is the picture being looked at.
    The Statistics block holds exactly one Rating line.

    Scoped to that one ELEMENT rather than run over the page text, which
    is the one thing this does differently from gelbooru.py. The page's
    text includes the post's comments, and a comment is user-supplied
    prose: "mis-tagged, Rating: explicit surely?" is an ordinary thing
    for someone to write, and RATING_LABEL_RE would match it. Today
    yande.re happens to render comments after the sidebar, so the first
    match is still the right one - but that is page layout, not a
    guarantee, and the failure mode is a wrong rating in the user's
    library presented as fact. Reading the element that actually states
    it does not depend on the ordering.

    (Scripts are NOT the hazard here, checked rather than assumed:
    _parsed.soup_of parses with lxml and bs4's get_text() omits <script>
    text, so neither the Post.register records nor the default blacklist
    yande.re ships - which does contain the string "rating:e" - can
    reach a page-text scrape at all.)

    Words, not letters, so no per-site code table is needed here - which
    matters, because Moebooru's "s" is *safe* while Danbooru 2's is
    *sensitive* (see core/boorus/_rating.py).
    """
    stats = soup_of(html).select_one(STATS_SELECTOR)
    if stats is None:
        log.debug("No %s Statistics block found on %s (markup may have changed)",
                  STATS_SELECTOR, url)
        return None
    rating = rating_from_label(stats.get_text(" "))
    if rating is None:
        log.debug("No 'Rating:' line in the Statistics block on %s", url)
    return rating


def parse_dimensions(html: str, url: str):
    """The ORIGINAL image's dimensions - never the sample's.

    That distinction is the whole point: Moebooru shows a downscaled
    sample by default, so reading width/height off the visible <img>
    would report the sample and make a genuinely larger match look like
    a downgrade - the opposite of what the size comparison is for.

    Three sources, all from the page already fetched for tags, so this
    costs no extra request:

    1. The embedded Post.register record, which states the original's
       width/height outright.
    2. The "Size: WxH" Statistics sidebar, shared with the other
       Danbooru 1.x descendants (see _sizes.py).
    3. The full-resolution download link's own text, which usually reads
       like "PNG (1500x2000, 2.5 MB)". Last because it's unlabelled
       digits - safe only because it's read from that single link rather
       than the whole page.
    """
    width = JSON_WIDTH_RE.search(html or "")
    height = JSON_HEIGHT_RE.search(html or "")
    if width and height:
        return int(width.group(1)), int(height.group(1))

    soup = soup_of(html)
    text = soup.get_text(" ")

    labelled_width, labelled_height = parse_size_label(text)
    if labelled_width and labelled_height:
        return labelled_width, labelled_height

    highres = soup.select_one("#highres")
    if highres:
        link_width, link_height = parse_bare_dimensions(highres.get_text(" "))
        if link_width and link_height:
            return link_width, link_height

    log.debug(
        "No dimensions found on %s - no Post.register record, no 'Size:' label, and no "
        "dimensions in the highres link. Context: %r",
        url, describe_size_context(text),
    )
    return None, None
