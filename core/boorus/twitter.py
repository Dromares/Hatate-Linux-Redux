"""Twitter/X, via FxTwitter's public JSON API.

Twitter's own pages are JS-driven and need a logged-in session, so there
has never been anything here for a parser to read. That left every
Twitter match with no dimensions, no artist, and - worse - a "file size"
that was really the SEARCH ENGINE'S THUMBNAIL: with no file URL of its
own, the app fell back to HEADing SauceNAO's ~30KB preview and reported
that as the match. The thumbnail shown was that same preview.

api.fxtwitter.com answers anonymously with everything needed: the
author, and for each photo its ORIGINAL pbs.twimg.com URL plus real
width and height. It is a third-party service (the same one behind the
fxtwitter.com/vxtwitter.com links these search engines already hand out),
so a tweet id is sent to it; nothing else about the user is.

Two things it fixes beyond the obvious:

  * Deleted-tweet detection. twitter.com/i/web/status/{id} answers 404 to
    an anonymous client whether or not the tweet exists, so matches were
    being dropped as gone while the tweet was perfectly alive. The API
    404s only when the tweet really has gone.
  * Picture quality. file_url is the uncapped original and the preview is
    the 2048px render, rather than the engine's thumbnail.

No tags beyond the author: a tweet has no booru-style vocabulary, and its
text is prose rather than tags.
"""
from __future__ import annotations

import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of

log = get_logger("boorus.twitter")

# Deliberately not t.co (a shortener, with no id to read) or
# pbs.twimg.com (media itself, belonging to no identifiable tweet) -
# those stay classification-only, exactly as before.
HOSTS = ("twitter.com", "x.com", "fxtwitter.com", "vxtwitter.com", "fixvx.com", "fixupx.com")

API_ENDPOINT = "https://api.fxtwitter.com/status"
TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
# Search engines sometimes name which image of a multi-photo tweet
# matched, right in the URL.
PHOTO_INDEX_RE = re.compile(r"/photo/(\d+)")

# Twitter's own render sizes. "orig" is uncapped; "large" is the 2048px
# render - plenty for a preview without pulling the full file to show it.
ORIGINAL_SIZE = "orig"
PREVIEW_SIZE = "large"
# The 680px render - small enough to hash a whole tweet's photos for less
# than one preview costs.
THUMBNAIL_SIZE = "small"


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url(url: str) -> str:
    match = TWEET_ID_RE.search(url or "")
    if not match:
        return url
    return f"{API_ENDPOINT}/{match.group(1)}"


def parse(body: str, url: str) -> List[Tag]:
    """The author, as a creator tag - the only tag a tweet really has.

    Stored under "artist" so the user's artist -> creator remap fires,
    matching every other parser here.
    """
    tweet = _tweet(body)
    if not tweet:
        return []
    author = (tweet.get("author") or {}).get("screen_name")
    if not isinstance(author, str) or not author.strip():
        return []
    return [Tag(name=author.strip().replace(" ", "_"), source=TagSource.BOORU,
                namespace="artist")]


def parse_file_url(body: str, url: str) -> Optional[str]:
    return _photo_url(body, url, ORIGINAL_SIZE)


def parse_preview_url(body: str, url: str) -> Optional[str]:
    return _photo_url(body, url, PREVIEW_SIZE)


def parse_dimensions(body: str, url: str):
    photo = _photo(body, url)
    if not photo:
        return None, None
    width = _as_int(photo.get("width"))
    height = _as_int(photo.get("height"))
    return (width, height) if width and height else (None, None)


def parse_page_count(body: str, url: str) -> int:
    photos = _photos(body)
    return len(photos) if photos else 1


# -- which image of the tweet ------------------------------------------
# Opting in to core.multipage_resolve._resolve_multipage_candidate, which
# leaves a multi-image post on its FIRST image unless the parser can list
# the others. Twitter could not, so a four-photo tweet whose third photo
# was the local file was shown, downloaded and compared as photo 1 - the
# wrong picture, with nothing to say so. The /photo/N suffix _photo()
# reads is no substitute: SauceNAO's result URLs carry no such suffix,
# and the API's own media facets name /photo/1 for every photo in the
# tweet, so in practice index 0 was all there ever was.
def pages_api_url(url: str) -> Optional[str]:
    """Where the photo list comes from - the same API record the tags were
    read from, so the resolver reuses that body instead of refetching."""
    fetch_url = resolve_fetch_url(url)
    return fetch_url if fetch_url != url else None


def parse_pages(body: str, url: str) -> List[dict]:
    """One entry per photo, in the tweet's order, shaped like Pixiv's page
    list.

    Every photo carries its real width and height, which the resolver can
    often settle the question with outright - no thumbnails downloaded.
    "small" is Twitter's 680px render, which is what gets hashed when it
    can't.
    """
    pages = []
    for photo in _photos(body):
        original = _sized(photo.get("url"), ORIGINAL_SIZE)
        if not original:
            continue
        pages.append({
            "urls": {
                "original": original,
                "regular": _sized(photo.get("url"), PREVIEW_SIZE),
                "small": _sized(photo.get("url"), THUMBNAIL_SIZE),
            },
            "width": _as_int(photo.get("width")),
            "height": _as_int(photo.get("height")),
        })
    return pages


# -- internals --------------------------------------------------------
def _tweet(body: str) -> Optional[dict]:
    if not body:
        return None
    try:
        data = json_of(body)
    except ValueError:
        # Twitter's own HTML, reached because the URL carried no tweet id.
        return None
    if not isinstance(data, dict):
        return None
    tweet = data.get("tweet")
    return tweet if isinstance(tweet, dict) else None


def _photos(body: str) -> List[dict]:
    tweet = _tweet(body)
    media = (tweet or {}).get("media")
    if not isinstance(media, dict):
        return []
    photos = media.get("photos")
    if not isinstance(photos, list):
        return []
    return [p for p in photos if isinstance(p, dict)]


def _photo(body: str, url: str) -> Optional[dict]:
    """The photo this match is about.

    A tweet can hold four images. When the URL names which one (the
    /photo/N suffix some engines produce) that is used; otherwise the
    first, which is the same default the app applies to every other
    multi-image site it can't resolve.
    """
    photos = _photos(body)
    if not photos:
        return None
    index = 0
    named = PHOTO_INDEX_RE.search(url or "")
    if named:
        candidate = int(named.group(1)) - 1  # /photo/1 is the first
        if 0 <= candidate < len(photos):
            index = candidate
        else:
            log.debug("%s names photo %s but the tweet has %d - using the first",
                      url, named.group(1), len(photos))
    return photos[index]


def _photo_url(body: str, url: str, size: str) -> Optional[str]:
    return _sized((_photo(body, url) or {}).get("url"), size)


def _sized(raw, size: str) -> Optional[str]:
    """One photo's URL at the render size wanted."""
    if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
        return None
    # The API hands out ...?name=orig; swap in the size we want rather
    # than assuming which one it used.
    if "name=" in raw:
        return re.sub(r"name=[^&]*", f"name={size}", raw)
    joiner = "&" if "?" in raw else "?"
    return f"{raw}{joiner}name={size}"


def _as_int(value) -> Optional[int]:
    try:
        return int(value or 0) or None
    except (TypeError, ValueError):
        return None
