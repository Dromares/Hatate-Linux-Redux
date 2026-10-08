"""Anime-Pictures, via its official JSON API.

The HTML site is JS-rendered, so the page itself carries no tags. The
JSON at api.anime-pictures.net/api/v3/posts/{id} carries everything
useful, but its shape is not the obvious one:

    {
      "post":  {"id":…, "md5":…, "width":…, "height":…,
                "big_preview":…, "medium_preview":…, "small_preview":…},
      "tags":  [ {"tag": {"tag": "name", "type": 4}, …}, … ],
      "file_url": "596771-5728x4000-original-….jpeg"
    }

Three traps, all of which this parser has been bitten by:

  * "tags" sits at the TOP level, beside "post" rather than inside it,
    and each entry WRAPS the tag in another "tag" key.
  * the top-level "file_url" is a download *filename*, not a URL.
  * the full-resolution image is not reachable anonymously at all. The
    old /pictures/download_image/{md5} endpoint now redirects to the API
    host and 404s, and a constructed CDN path (oimages.…) answers 302 to
    the site's front page. Both would hand back an HTML page for the app
    to store as if it were an image, which is worse than admitting there
    is no file URL - so parse_file_url returns None and callers fall back
    to the preview, the same degradation as a Gold-only Danbooru post.

Previews ARE served anonymously and the API names them outright, so the
match still shows a picture and still reports its true dimensions.

No rating comes from here either, and like rule34.us that is a measured
result rather than an omission. DAN-73 could not settle it because the
API answered 403 to every request that run made; it IS reachable, and
that 403 was a bot check on the User-Agent rather than a network block -
curl's default UA still gets 403 where an ordinary browser UA gets 200.
CHECKED against live posts 100, 5000 and 900000, whose "post" object
holds exactly: artefacts_degree, big_preview, color, datetime,
download_count, erotics, ext, have_alpha, height, id, juser_id, md5,
md5_pixels, medium_preview, pubtime, score, score_number, size,
small_preview, smooth_degree, spoiler, status, status_type (on 900000
only), tags_count, width. No "rating" key appears anywhere in the
response.

The one rating-shaped field is "erotics", and it is an intensity LEVEL
rather than an age rating: a bare integer, observed as 0 and 1, with no
label for any value stated anywhere in the response. Turning it into
"rating:safe"/"rating:questionable"/"rating:explicit" would mean
inventing both how many levels exist and which word each one means -
exactly the guess core/boorus/_rating.py refuses, and unverifiable from
here anyway, since the higher levels sit behind the same account gate
restriction_for_status already reports. parse_rating is therefore absent
on purpose; do not derive one from the level without a source that
states the site's own words for each value.
"""
from __future__ import annotations

import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host
from ._parsed import json_of

log = get_logger("boorus.animepictures")

HOSTS = ("anime-pictures.net",)
API_HOST = "https://api.anime-pictures.net"

# Both URL forms have to match. Search engines hand us the legacy
# /pictures/view_post/{id}, which the site 301s to /posts/{id}; matching
# only the modern form meant the API was never used and the JS-rendered
# HTML page got parsed instead, yielding no tags and no picture.
POST_ID_RE = re.compile(r"/(?:pictures/view_post|posts)/(\d+)")

# Verified against live posts rather than guessed: 1 names characters,
# 4 the artist, 5 a game and 6 an anime/"original" - both of which are
# copyright in Hydrus terms. 2 and 7 are descriptive ("long hair",
# "girl", "highres"), so they stay unnamespaced like Danbooru's
# category-0. Anything unrecognised falls through to general rather than
# being dropped.
TAG_TYPES = {
    1: "character",
    4: "artist",   # artist -> creator happens later via the user's remap
    5: "copyright",
    6: "copyright",
}
DEFAULT_NAMESPACE = "general"

# The API states the original's extension; these are the same names
# fetch_remote_info derives from a Content-Type, so the UI shows one
# vocabulary no matter which source a match's format came from.
EXTENSION_FORMATS = {
    "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF",
    "webp": "WEBP", "bmp": "BMP", "avif": "AVIF", "webm": "WEBM", "mp4": "MP4",
}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url(url: str) -> str:
    match = POST_ID_RE.search(url or "")
    if not match:
        return url
    return f"{API_HOST}/api/v3/posts/{match.group(1)}"


def parse(body: str, url: str) -> List[Tag]:
    tags: List[Tag] = []
    for raw in _tag_entries(body):
        if not isinstance(raw, dict):
            continue
        # Normally {"tag": {"tag": "name", "type": 4}}; tolerate a flat
        # {"tag": "name", "type": 4} in case the API is ever simplified.
        info = raw.get("tag")
        if not isinstance(info, dict):
            info = raw
        name = info.get("tag")
        if not isinstance(name, str):
            continue
        name = name.strip().replace(" ", "_")
        if not name:
            continue
        raw_type = info.get("type")
        try:
            type_id = int(raw_type) if raw_type is not None else -1
        except (TypeError, ValueError):
            type_id = -1
        tags.append(Tag(
            name=name, source=TagSource.BOORU,
            namespace=TAG_TYPES.get(type_id, DEFAULT_NAMESPACE),
        ))
    return tags


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The full image, or None when there isn't one we can actually get.

    Only an absolute http(s) URL counts. The API's own "file_url" is a
    filename, and every constructed CDN path tried so far answers with
    the site's front page - returning either would have the app download
    HTML and treat it as the matched image.
    """
    for holder in _holders(body):
        for key in ("file_url", "url"):
            candidate = holder.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
    return None


def parse_preview_url(body: str, url: str) -> Optional[str]:
    post = _post(body)
    if not post:
        return None
    for key in ("big_preview", "medium_preview", "small_preview"):
        candidate = post.get(key)
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    return None


def restriction_for_status(status_code: int, body: str, url: str) -> Optional[str]:
    """Why a non-200 means "sign in", rather than "the fetch failed".

    Account-only posts answer the API with
    403 {"errormsg": "Forbidden", "success": false} - which carries none
    of the post: no tags, no dimensions, not even a preview. Saying so
    turns a blank match into one the user can act on, since adding
    cookies for a logged-in account does fill it in.

    Deliberately narrow. A 403 with a Cloudflare challenge body is a bot
    check that a login would not fix, and this site does throw those
    occasionally, so only the API's own JSON error counts.
    """
    if status_code != 403:
        return None
    try:
        data = json_of(body)
    except ValueError:
        return None  # an HTML 403 is a bot check, not an account gate
    if not isinstance(data, dict) or data.get("success") is not False:
        return None
    reason = str(data.get("errormsg") or "Forbidden").strip()
    return (
        f"Anime-Pictures returned {reason} for this post - it is account-only. "
        "Add cookies from a logged-in account under Settings > Site Logins to see it."
    )


def parse_file_info(body: str, url: str):
    """(format, size_in_bytes) for the original file, straight from the API.

    Normally these come from a HEAD on the direct file URL, but this site
    has none we can reach, so that HEAD would fall back to measuring the
    search engine's THUMBNAIL - reporting a few KB and the thumbnail's
    format as though they described the match. The API states both
    outright for the original, which is better than a HEAD anyway: it is
    authoritative and costs no extra request.
    """
    post = _post(body)
    if not post:
        return None, None

    fmt = None
    ext = post.get("ext")
    if isinstance(ext, str) and ext.strip():
        fmt = EXTENSION_FORMATS.get(ext.strip().lower().lstrip("."))

    size = post.get("size")
    try:
        size = int(size) if size else None
    except (TypeError, ValueError):
        size = None
    if size is not None and size <= 0:
        size = None

    return fmt, size


def parse_dimensions(body: str, url: str):
    post = _post(body)
    if not post:
        return None, None
    width, height = post.get("width"), post.get("height")
    try:
        width = int(width) if width else None
        height = int(height) if height else None
    except (TypeError, ValueError):
        return None, None
    if width and height:
        return width, height
    return None, None


def _document(body: str) -> Optional[dict]:
    if not body:
        return None
    try:
        data = json_of(body)
    except ValueError:
        log.debug(
            "Anime-Pictures response was not JSON - expected the v3 API, so this is "
            "most likely the JS-rendered HTML page (check resolve_fetch_url matched "
            "the post id in the URL)"
        )
        return None
    return data if isinstance(data, dict) else None


def _holders(body: str) -> List[dict]:
    """The document and its "post", in that order.

    Fields have moved between the two across API revisions, so look in
    both rather than committing to one and silently returning nothing.
    """
    doc = _document(body)
    if doc is None:
        return []
    post = doc.get("post")
    return [doc, post] if isinstance(post, dict) else [doc]


def _post(body: str) -> Optional[dict]:
    for holder in reversed(_holders(body)):  # the "post" sub-object first
        if any(key in holder for key in ("id", "md5", "width")):
            return holder
    return None


def _tag_entries(body: str) -> list:
    for holder in _holders(body):
        tags = holder.get("tags")
        if isinstance(tags, list):
            return tags
    return []
