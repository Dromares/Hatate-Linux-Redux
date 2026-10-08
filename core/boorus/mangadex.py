"""MangaDex chapters.

MangaDex was classification-only for a long time, and the reason given
was that a chapter URL points at many images rather than one, which
didn't fit the one-match-one-image model the parsers assume. That stopped
being true when multi-page support arrived: "which of these images is the
one I have?" is exactly what core/multipage.py answers, by hashing each
page against the local file. A chapter is a multi-page post like any
other, so it is handled the same way.

Everything comes from the public API at api.mangadex.org rather than the
site's HTML, which is a JS front end with nothing useful in the served
markup. /at-home/server/{chapterId} returns the node serving that chapter
plus its page list, in two resolutions:

    data        the original scans
    dataSaver   the same pages recompressed, a fraction of the size

The saver images are what get hashed to find the matching page - they are
the same picture, and a perceptual hash does not care about the
compression. The original is what the match then points at.

Two things worth knowing about these URLs:

* They are short-lived. The baseUrl is an assignment to one node of the
  MangaDex@Home network, good for a few minutes, not a permanent address.
  So a stored one goes stale: fine for the preview shown during a search,
  but "Download Matched Image + Send to Hydrus" hours later may have to
  be re-run rather than reusing what the session saved.
* Chapters disappear often. In a sample of this app's own matches, four
  in ten were already 404 - re-uploads, takedowns and group changes are
  routine here. Those raise BooruContentGoneError like any other dead
  post, so the existing dead-link handling drops them.

No tags come from here. Series/volume/chapter/page are now applied by
the Hydrus importer itself, not by this app.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional
from urllib.parse import urlparse

from ..applog import get_logger
from ..models import Tag
from ._parsed import json_of

log = get_logger("boorus.mangadex")

API_BASE = "https://api.mangadex.org"

# This parser never returns tags - see the note at the top of this
# module. Without this every MangaDex match would log a "markup may have
# changed" warning and the app would call the parser broken after three.
EMPTY_RESULT_IS_NORMAL = True

# 1: stopped emitting series/volume/chapter/page. Entries cached while
# this parser still produced them restore with booru_tags_fetched=False,
# so the tags are dropped on next use rather than served from disk
# forever. See _tag_version_for in core/search_cache.py.
TAG_VERSION = 1

# MangaDex identifies everything by UUID. Anchored to the /chapter/ route
# specifically: a /title/ URL is a whole manga, which has no page list of
# its own and is left classification-only.
CHAPTER_RE = re.compile(
    r"/chapter/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def matches(url: str) -> bool:
    if not url:
        return False
    host = (urlparse(url).netloc or "").lower()
    if not (host == "mangadex.org" or host.endswith(".mangadex.org")):
        return False
    return CHAPTER_RE.search(url) is not None


def chapter_id(url: str) -> Optional[str]:
    found = CHAPTER_RE.search(url or "")
    return found.group(1) if found else None


def resolve_fetch_url(url: str) -> str:
    """Fetch the API's page list instead of the chapter page, which is a
    JS front end with nothing in its markup."""
    found = chapter_id(url)
    if not found:
        log.debug("No chapter id in %s, fetching it as-is", url)
        return url
    return f"{API_BASE}/at-home/server/{found}"


def pages_api_url(url: str) -> Optional[str]:
    """Where the page list lives, for the multi-page resolver.

    The same response the fetch above already asked for - it carries
    every page, so nothing extra is requested to work out which one
    matched.
    """
    found = chapter_id(url)
    return f"{API_BASE}/at-home/server/{found}" if found else None


def _page_urls(body: str) -> List[dict]:
    """[{'original': …, 'saver': …}] in page order, or [] if the response
    isn't one we understand."""
    try:
        data = json_of(body)
    except (json.JSONDecodeError, TypeError):
        log.warning("MangaDex response was not valid JSON (the API may have changed)")
        return []
    if not isinstance(data, dict) or data.get("result") != "ok":
        return []

    base = data.get("baseUrl")
    chapter = data.get("chapter")
    if not base or not isinstance(chapter, dict):
        return []
    chapter_hash = chapter.get("hash")
    if not chapter_hash:
        return []

    originals = chapter.get("data") or []
    savers = chapter.get("dataSaver") or []
    if not isinstance(originals, list):
        return []

    pages = []
    for index, name in enumerate(originals):
        if not isinstance(name, str) or not name:
            continue
        entry = {"original": f"{base}/data/{chapter_hash}/{name}"}
        # The two lists are parallel, but only pair them where both
        # really have an entry - a mismatched length would otherwise
        # attach the wrong page's saver image to a page, which is
        # precisely the mistake this whole mechanism exists to avoid.
        if index < len(savers) and isinstance(savers[index], str) and savers[index]:
            entry["saver"] = f"{base}/data-saver/{chapter_hash}/{savers[index]}"
        pages.append(entry)
    return pages


def parse_pages(body: str, url: str) -> List[dict]:
    """The page list in the shape the multi-page resolver expects.

    No width or height: the API does not report them. Absent is correct
    here rather than guessed - the resolver treats unknown dimensions as
    "no evidence" and falls back to hashing, whereas invented ones would
    be evidence pointing at the wrong page.
    """
    pages = []
    for page in _page_urls(body):
        saver = page.get("saver") or page["original"]
        pages.append({
            "urls": {
                "original": page["original"],
                # "regular" and "small" are both the saver image: it is
                # the only smaller size MangaDex offers, and it is what
                # both the preview and the page hashing want.
                "regular": saver,
                "small": saver,
            },
            "width": None,
            "height": None,
        })
    return pages


def parse_page_count(body: str, url: str) -> int:
    return len(_page_urls(body)) or 1


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The first page's original. Which page actually matched is settled
    later by the multi-page resolver; this is the honest default until
    then, and the one used when a chapter really is a single page."""
    pages = _page_urls(body)
    return pages[0]["original"] if pages else None


def parse_preview_url(body: str, url: str) -> Optional[str]:
    """The first page's saver image - a fraction of the original's size
    and plenty for the preview panel."""
    pages = _page_urls(body)
    if not pages:
        return None
    return pages[0].get("saver") or pages[0]["original"]


def parse(body: str, url: str) -> List[Tag]:
    """No tags - see the note at the top of this module."""
    return []
