from __future__ import annotations

import json
import re
from typing import List, Optional


from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of, soup_of

log = get_logger("boorus.pixiv")

HOST = "pixiv.net"

# The legacy member_illust.php?mode=medium&illust_id=X URL format (still
# commonly seen in older search-engine indexes, like SauceNAO's) returns
# HTTP 200 but doesn't serve the meta-preload-data JSON blob our parser
# relies on - only the modern /artworks/{id} page does. illust_id refers
# to the exact same artwork either way, so redirect the actual fetch to
# the modern URL rather than failing to extract anything.
ILLUST_ID_RE = re.compile(r"/artworks/(\d+)")
LEGACY_ILLUST_ID_RE = re.compile(r"[?&]illust_id=(\d+)")


def matches(url: str) -> bool:
    return on_host(url, (HOST,))


def illust_id(url: str) -> Optional[str]:
    """The artwork's numeric ID, from either URL form."""
    match = ILLUST_ID_RE.search(url or "") or LEGACY_ILLUST_ID_RE.search(url or "")
    return match.group(1) if match else None


def resolve_fetch_url(url: str) -> str:
    """Fetch Pixiv's AJAX API rather than the artwork page.

    The page used to embed a `meta-preload-data` JSON blob, but Pixiv
    stopped shipping it - a logged-out artwork page now comes back as
    ~93 KB of markup with no such element at all, so page scraping
    yields nothing no matter how robustly it's parsed. The data lives at
    /ajax/illust/{id} instead, which returns clean JSON containing the
    tags, the full-resolution image URL, dimensions and the artist.

    The page-scraping path is kept as a fallback (see _extract_preload_data)
    in case Pixiv reverses course or serves the blob to some clients."""
    illust = illust_id(url)
    if not illust:
        log.debug("No illust ID found in %s, fetching as-is", url)
        return url
    return f"https://www.pixiv.net/ajax/illust/{illust}"


def _illust_data(body: str, url: str) -> Optional[dict]:
    """The illust object, from either the AJAX API response or the older
    embedded page blob - whichever this response actually is."""
    # AJAX API: {"error": false, "body": {...}}
    try:
        data = json_of(body)
    except (json.JSONDecodeError, TypeError):
        data = None

    if isinstance(data, dict):
        if data.get("error"):
            msg = data.get("message") or ""
            # gallery-dl notes Pixiv returns this generic text specifically
            # when the session cookie is missing or expired.
            if msg == "An unknown error occurred":
                log.warning("Pixiv rejected the request for %s - the PHPSESSID cookie is "
                            "missing or expired (Settings > Site Logins)", url)
            else:
                log.warning("Pixiv API error for %s: %s", url, msg or "unspecified")
            return None
        body_obj = data.get("body")
        if isinstance(body_obj, dict) and (body_obj.get("urls") or body_obj.get("tags")):
            return body_obj

    # Fallback: the legacy embedded page blob.
    legacy = _extract_preload_data(body, url)
    if legacy:
        for illust in (legacy.get("illust") or {}).values():
            return illust
    return None


def _extract_preload_data(html: str, url: str) -> Optional[dict]:
    """Pulls Pixiv's embedded JSON blob out of the page.

    Uses a real HTML parser rather than a regex. The previous regex
    required `id="meta-preload-data"` to be followed IMMEDIATELY by
    `content='...'`, with single quotes specifically - so any attribute
    reordering, an extra attribute between the two, or a switch to double
    quotes broke extraction for every artwork at once, which is exactly
    the failure pattern seen in the wild (HTTP 200, no blob found).
    BeautifulSoup is immune to all three, and it also un-escapes HTML
    entities in the attribute value for free.
    """
    soup = soup_of(html)

    for tag_id in ("meta-preload-data", "meta-global-data"):
        tag = soup.find("meta", id=tag_id)
        if tag is None:
            continue
        content = tag.get("content")
        if not content:
            log.debug("<meta id=%s> on %s had no content attribute", tag_id, url)
            continue
        try:
            data = json.loads(content)  # type: ignore[arg-type]  # bs4 stub: Tag.__getitem__/.get() typed str | AttributeValueList regardless of attribute
        except (json.JSONDecodeError, TypeError):
            log.warning("<meta id=%s> on %s did not contain valid JSON", tag_id, url)
            continue
        if isinstance(data, dict) and data.get("illust"):
            return data
        log.debug("<meta id=%s> on %s parsed but had no illust data", tag_id, url)

    # Nothing usable - work out WHY so the log actually helps next time,
    # rather than just saying "not found".
    if soup.find("meta", id="meta-preload-data") is None:
        lowered = html.lower()
        if "login" in lowered and ("signup" in lowered or "sign up" in lowered):
            log.warning(
                "No preload data on %s and the page looks like a login wall - this artwork may be "
                "restricted to logged-in users (Pixiv added that per-work setting in 2025)", url,
            )
        else:
            log.warning(
                "No <meta id=meta-preload-data> element on %s at all (page was %d bytes) - "
                "Pixiv may have changed its page structure", url, len(html),
            )
    return None


def parse(body: str, url: str) -> List[Tag]:
    """Tags plus the artist. The artist lives outside the tag list, so
    it has to be pulled separately or it's lost entirely."""
    illust = _illust_data(body, url)
    if not illust:
        return []

    tags: List[Tag] = []
    for t in ((illust.get("tags") or {}).get("tags") or []):
        name = t.get("tag")
        if name:
            tags.append(Tag(name=name.replace(" ", "_"), source=TagSource.BOORU, namespace=None))

    artist = illust.get("userName")
    if artist:
        tags.append(Tag(name=artist.replace(" ", "_"), source=TagSource.BOORU, namespace="creator"))

    if not tags:
        log.debug("Pixiv data found for %s but it contained no tags", url)
    return tags


def _url(body: str, url: str, key: str) -> Optional[str]:
    illust = _illust_data(body, url)
    if not illust:
        return None
    return (illust.get("urls") or {}).get(key)


def parse_file_url(body: str, url: str) -> Optional[str]:
    """Full-resolution original - what download-and-send grabs."""
    return _url(body, url, "original")


def parse_preview_url(body: str, url: str) -> Optional[str]:
    """Pixiv's own display-size copy, for the preview thumbnail."""
    return _url(body, url, "regular") or _url(body, url, "small")


def parse_page_count(body: str, url: str) -> int:
    """How many images this artwork holds. 1 for an ordinary post.

    A Pixiv artwork can carry dozens of images under one URL, and
    /ajax/illust/{id} describes only the FIRST - so on a multi-page post
    the file URL, preview and dimensions above all belong to page 1
    whatever page actually matched. Callers use this to decide whether
    the extra work of identifying the right page is warranted.
    """
    illust = _illust_data(body, url)
    if not illust:
        return 1
    try:
        return max(1, int(illust.get("pageCount") or 1))
    except (TypeError, ValueError):
        return 1


def pages_api_url(url: str) -> Optional[str]:
    """Pixiv's per-page listing for an artwork, which /ajax/illust/{id}
    doesn't include."""
    illust = illust_id(url)
    if not illust:
        return None
    return f"https://www.pixiv.net/ajax/illust/{illust}/pages"


def parse_pages(body: str, url: str) -> List[dict]:
    """The page list from /ajax/illust/{id}/pages: one entry per image,
    each with its own urls (original/regular/small)."""
    try:
        data = json_of(body)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict) or data.get("error"):
        return []
    pages = data.get("body")
    if not isinstance(pages, list):
        return []
    return [p for p in pages if isinstance(p, dict) and p.get("urls")]


def parse_dimensions(body: str, url: str):
    illust = _illust_data(body, url)
    if not illust:
        return None, None
    return illust.get("width"), illust.get("height")
