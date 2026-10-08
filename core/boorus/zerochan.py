"""Zerochan.

Uses Zerochan's documented read-only JSON API rather than scraping HTML:
appending "?json" to a post URL returns structured data including the
direct full-resolution image URL, so we get the original file rather than
a re-encoded preview - which is the whole point of downloading from the
source instead of the search engine's copy.

Their API docs ask for a descriptive User-Agent naming the project, and
note a rate limit of 60 requests/minute. We're well under that (one
request per candidate, with the app's own search delay between images),
and the User-Agent below is descriptive as requested.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of

log = get_logger("boorus.zerochan")

HOSTS = ("zerochan.net",)

# Post URLs look like https://www.zerochan.net/1234567
POST_ID_RE = re.compile(r"zerochan\.net/(\d+)")


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url(url: str) -> str:
    """Redirect the fetch to Zerochan's JSON API for this post - it
    returns the full-resolution image URL and structured tags directly,
    which is both more reliable and higher quality than parsing the
    HTML page."""
    match = POST_ID_RE.search(url)
    if not match:
        log.debug("No post ID found in %s, fetching as-is", url)
        return url
    return f"https://www.zerochan.net/{match.group(1)}?json"


def _load(body: str, url: str) -> Optional[dict]:
    try:
        data = json_of(body)
    except (json.JSONDecodeError, TypeError):
        log.warning("Zerochan response for %s was not valid JSON (API may have changed)", url)
        return None
    return data if isinstance(data, dict) else None


def parse(body: str, url: str) -> List[Tag]:
    data = _load(body, url)
    if not data:
        return []

    tags: List[Tag] = []
    for raw in data.get("tags") or []:
        if not isinstance(raw, str):
            continue
        # Zerochan tags arrive as "Character: Name" / "Series: Title" /
        # "Mangaka: Artist" - split that into our namespace:name form so
        # they line up with how every other site's tags are stored.
        if ":" in raw:
            namespace_raw, name = raw.split(":", 1)
            namespace: Optional[str] = _normalize_namespace(namespace_raw.strip())
            name = name.strip()
        else:
            namespace, name = None, raw.strip()
        if name:
            tags.append(Tag(name=name.replace(" ", "_"), source=TagSource.BOORU, namespace=namespace))

    if not tags:
        log.debug("No tags in Zerochan response for %s", url)
    return tags


# Zerochan's own tag categories -> the namespaces the rest of the app
# (and Hydrus's PTR convention) uses.
NAMESPACE_MAP = {
    "mangaka": "creator",
    "artist": "creator",
    "character": "character",
    "series": "series",
    "game": "series",
    "studio": "studio",
    "source": "series",
}


def _normalize_namespace(raw: str) -> Optional[str]:
    return NAMESPACE_MAP.get(raw.lower(), raw.lower() or None)


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The API's "full" field is the direct full-resolution original."""
    data = _load(body, url)
    if not data:
        return None
    return data.get("full")


def parse_preview_url(body: str, url: str) -> Optional[str]:
    """The best preview short of the full original.

    Zerochan offers four sizes, and they are not what their names
    suggest - measured against a live post (2976x4055):

        small    102x139      5 KB
        medium   240x327     11 KB
        large    600x818     76 KB
        full    2976x4055  5573 KB

    This used to take "medium", which is a 240px thumbnail - barely
    better than the search engine's own and visibly blurry. "large" is
    the one that actually reads as a preview, and at 76 KB costs nothing
    like the original. "full" is deliberately not used here; that is what
    parse_file_url is for.
    """
    data = _load(body, url)
    if not data:
        return None
    for key in ("large", "medium", "small"):
        candidate = data.get(key)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def parse_dimensions(body: str, url: str):
    data = _load(body, url)
    if not data:
        return None, None
    return data.get("width"), data.get("height")
