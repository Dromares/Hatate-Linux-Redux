"""Sankaku Complex, via its modern API, looked up by file hash.

Sankaku's old numeric post ids are the only ones IQDB and SauceNAO know,
and they no longer lead anywhere. Verified against the live site:

  * chan.sankakucomplex.com now bounces EVERY post URL to an OIDC login
    page - ids 100 through 38,000,000 alike - so a redirect there proves
    nothing about whether a post exists. The app used to read that bounce
    as "deleted" and drop the match, which is why Sankaku matches kept
    disappearing.
  * The modern API answers anonymously and carries everything worth
    having - tags with types, dimensions, byte size, format - but is keyed
    by new-style ids like "QyMk8vZ6Kak". A numeric id returns
    {"code": "invalid id"}, legacy_id is null on every post sampled, and
    no legacy -> modern redirect service exists.

So the numeric id is a dead end, and the only key left that this app can
supply is the FILE ITSELF. Sankaku indexes by md5, so the local file's
hash finds the post whenever the local copy is byte-identical to
Sankaku's - which is the case for anything downloaded from there, and not
for a re-encode or a resize. When it misses, nothing is claimed.

That is also why the match gets repointed (parse_canonical_url): having
identified the post, the modern URL is one the user can actually open,
where the legacy one is a guaranteed login wall.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urlencode

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of
from ._rating import from_code as rating_from_code

log = get_logger("boorus.sankaku")

HOSTS = ("sankakucomplex.com", "sankaku.app")
API_ENDPOINT = "https://sankakuapi.com/posts"
POST_URL_BASE = "https://www.sankakucomplex.com/posts"

MD5_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

# A hash miss - the local file simply isn't Sankaku's copy - is the
# ordinary outcome here, not a sign the site changed. Without this every
# such match would log a "markup may have changed" warning.
EMPTY_RESULT_IS_NORMAL = True

# Verified against live posts: 1 is the artist, 3 the copyright, 4 the
# character, 8 and 9 housekeeping. 0 and 5 are both descriptive - 5 is
# Sankaku's "genre" (Hetero, BDSM), which is a content descriptor rather
# than a namespace this app has anywhere else - so both stay unnamespaced.
TAG_TYPES = {
    1: "artist",   # artist -> creator happens later via the user's remap
    3: "copyright",
    4: "character",
    8: "meta",
    9: "meta",
}
DEFAULT_NAMESPACE = "general"

# Sankaku's own rating codes. Danbooru 1.x lineage: three ratings, and
# "s" is SAFE - not Danbooru 2's "sensitive", which would be the wrong
# word in the user's library (see core/boorus/_rating.py).
#
# Pinned by the site's own search aliases rather than by assuming the
# lineage: tags=rating:safe returns posts stored as "s", while
# tags=rating:sensitive is not a rating search at all - it falls through
# to ordinary tag matching and returns "e" posts. Sampling the field
# across the API only ever produced s/q/e, and tags=rating:g resolves to
# stored "s", so "g" is an input alias here, not a fourth rating.
RATING_CODES = {"s": "safe", "q": "questionable", "e": "explicit"}

FILE_TYPE_FORMATS = {
    "image/jpeg": "JPEG", "image/jpg": "JPEG", "image/png": "PNG",
    "image/gif": "GIF", "image/webp": "WEBP", "image/avif": "AVIF",
    "video/mp4": "MP4", "video/webm": "WEBM",
}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url_with_context(url: str, context: dict) -> str:
    """Where to look this post up, given the file being searched.

    Returns the original URL when there is no hash to search by. That
    fetches the legacy page, which yields nothing - the same as before,
    rather than a new kind of failure.
    """
    md5 = (context or {}).get("local_md5")
    if not isinstance(md5, str) or not MD5_RE.match(md5):
        log.debug("No local md5 available - Sankaku's own id cannot be resolved, skipping")
        return url
    return f"{API_ENDPOINT}?{urlencode({'tags': f'md5:{md5.lower()}', 'limit': '1'})}"


def parse(body: str, url: str) -> List[Tag]:
    post = _post(body)
    if not post:
        return []
    tags: List[Tag] = []
    for raw in post.get("tags") or []:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name_en") or raw.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_type = raw.get("type")
        try:
            type_id = int(raw_type) if raw_type is not None else -1
        except (TypeError, ValueError):
            type_id = -1
        tags.append(Tag(
            name=name.strip().replace(" ", "_"), source=TagSource.BOORU,
            namespace=TAG_TYPES.get(type_id, DEFAULT_NAMESPACE),
        ))
    return tags


def parse_canonical_url(body: str, url: str) -> Optional[str]:
    """The modern post URL, now that the hash has identified the post.

    The legacy numeric URL we were handed is a login wall for everyone,
    so leaving the match pointing at it hands the user a dead link and
    associates a dead link with the file in Hydrus.
    """
    post = _post(body)
    post_id = (post or {}).get("id")
    if not isinstance(post_id, str) or not post_id:
        return None
    return f"{POST_URL_BASE}/{post_id}"


def parse_file_url(body: str, url: str) -> Optional[str]:
    return _absolute(_post(body), "file_url")


def parse_preview_url(body: str, url: str) -> Optional[str]:
    # The sample is a proper mid-size render; the "preview" is a tiny
    # thumbnail, so it's only the fallback.
    post = _post(body)
    return _absolute(post, "sample_url") or _absolute(post, "preview_url")


def parse_rating(body: str, url: str) -> Optional[str]:
    """The post's rating, from the API's own "rating" field, through
    Sankaku's own table above.

    Only the API path can answer: the legacy page this parser is
    otherwise handed is an OIDC login wall (see the module docstring),
    so there is no markup to read a rating off even in principle. A hash
    miss returns no post and therefore no rating, which is the ordinary
    outcome here rather than a fault.
    """
    post = _post(body)
    if post is None:
        return None
    return rating_from_code(post.get("rating"), RATING_CODES)


def parse_dimensions(body: str, url: str):
    post = _post(body)
    if not post:
        return None, None
    try:
        width = int(post.get("width") or 0) or None
        height = int(post.get("height") or 0) or None
    except (TypeError, ValueError):
        return None, None
    return (width, height) if width and height else (None, None)


def parse_file_info(body: str, url: str):
    """(format, size) as the API states them for the original.

    Worth taking from the API rather than a HEAD: Sankaku's file URLs are
    signed and time-limited, so a HEAD on a stale one would report
    nothing and leave the match looking sizeless.
    """
    post = _post(body)
    if not post:
        return None, None

    fmt = None
    file_type = post.get("file_type")
    if isinstance(file_type, str):
        fmt = FILE_TYPE_FORMATS.get(file_type.split(";")[0].strip().lower())

    size = post.get("file_size")
    try:
        size = int(size) if size else None
    except (TypeError, ValueError):
        size = None
    if size is not None and size <= 0:
        size = None
    return fmt, size


def _absolute(post: Optional[dict], key: str) -> Optional[str]:
    value = (post or {}).get(key)
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def _post(body: str) -> Optional[dict]:
    """The single post the hash search returned, if it found one.

    An empty list is the normal miss - the local file simply isn't
    Sankaku's copy - and is not an error.
    """
    if not body:
        return None
    try:
        data = json_of(body)
    except ValueError:
        # The legacy HTML page, reached because there was no hash to
        # search with. Nothing to read from it.
        return None
    if isinstance(data, dict):
        data = data.get("data") or data.get("posts") or []
    if not isinstance(data, list) or not data:
        return None
    post = data[0]
    return post if isinstance(post, dict) else None


def incomplete_reason(body: str, url: str) -> Optional[str]:
    """Why a Sankaku lookup came back empty.

    Both outcomes here look identical from the outside - a match with no
    tags - and they are not the same problem, so neither should be left
    to be guessed at:

    * The hash search ran and Sankaku has no such file. The post is real
      and its picture is fine; the local copy is simply not byte-identical
      to Sankaku's, which is what a re-encode or a resave produces. There
      is nothing further to try, because the numeric id in the URL is a
      dead end (verified against the live API: /posts/<numeric> answers
      "invalid id", and legacy_id searches return nothing).
    * There was no md5 to search with, so the legacy HTML page was fetched
      instead and it carries nothing. That happens when the local file
      could not be read.

    Returns None when the body did contain a post, since then the empty
    tag list is a genuinely untagged post rather than a miss.
    """
    if not body:
        return "Sankaku returned nothing for this file"
    try:
        json_of(body)
    except ValueError:
        return (
            "No local file hash to look this up with - Sankaku can only be "
            "searched by file hash, since its old numeric post ids no longer resolve"
        )
    if _post(body) is not None:
        return None
    return (
        "Sankaku has no byte-identical copy of this file, so its post cannot be "
        "identified - the local file is probably a re-encode or a resave. Sankaku's "
        "old numeric ids no longer resolve, so there is no other way to reach it."
    )
