"""Pawchive, via its Kemono-style JSON API.

Pawchive is a Kemono fork: posts live at /{service}/user/{uid}/post/{pid}
and the API mirroring them at /api/v1/... answers ANONYMOUSLY, even
though the HTML pages behind those same URLs are gated behind an account
("...is only available to registered users"). That gap is the whole
reason a parser is worth having here.

Only pawchive.pw is handled. pawchive.org shares the name and the label
in core/sites.py but is a different site - it 404s on both the post
routes and the API - so it stays classification-only.

What the API does and does not give:

  * tags, when the post has them - as a POSTGRES ARRAY LITERAL in a
    string, not a JSON list: {Bondage,Damsel,"Total Versext",gefesselt}.
  * no artist name, only a numeric user id. The name comes from a second
    call to the profile endpoint, cached per creator - without it most
    posts yield no tags at all, since plenty carry none of their own.
  * a preview from img.pawchive.pw, and the ORIGINAL from
    file.pawchive.pw - a separate host, which is why the original looks
    account-gated if you only probe the image host (every /data path
    there 404s). No login is involved: where the archive holds the full
    file it serves it to anyone.
  * "has_full", which says whether it holds that file at all. A false one
    goes with preview_state "pending" and its original 404s for everyone,
    so no file URL is offered for those.
  * no dimensions anywhere. The file host honours Range requests,
    though, so parse_dimensions reads them from the head of the original
    itself - a few KB, never the whole file - and only when there is an
    original to read (has_full). Format and byte size need no help: with
    a real file URL the normal HEAD reports them.

Posts are routinely multi-image; "file" is always attachments[0], so the
page count is reported honestly even though only Pixiv posts currently
get resolved to the matching page.
"""
from __future__ import annotations

import csv
import io
import re
from typing import List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

from ..applog import get_logger
from ..lru_cache import LRUCache
from ..models import Tag, TagSource
from .. import net
from . import _remote_size
from ._host import on_host
from ._parsed import json_of

log = get_logger("boorus.pawchive")

HOSTS = ("pawchive.pw",)
THUMBNAIL_BASE = "https://img.pawchive.pw/thumbnail/data"
# Originals live on their own host - NOT under img.pawchive.pw, where
# every /data path 404s and makes them look account-gated when they are
# not. This is the host the site's own download links point at.
FILE_BASE = "https://file.pawchive.pw/data"

POST_RE = re.compile(
    r"/(?P<service>[A-Za-z0-9_.-]+)/user/(?P<user>[^/?#]+)/post/(?P<post>[^/?#]+)"
)

# One small request per creator, and a batch tends to revisit the same
# few, so this keeps the artist tag from costing a lookup every post.
ARTIST_CACHE_ENTRIES = 512
ARTIST_TIMEOUT = 10.0
_artist_cache = LRUCache(ARTIST_CACHE_ENTRIES)



def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def resolve_fetch_url(url: str) -> str:
    """The API mirror of a post page, or the input unchanged.

    The page itself is account-gated, so falling back to it yields
    nothing - which is the old behaviour, not a new failure.
    """
    match = POST_RE.search(url or "")
    if not match:
        return url
    host = urlparse(url).netloc or HOSTS[0]
    return (
        f"https://{host}/api/v1/{match.group('service')}"
        f"/user/{match.group('user')}/post/{match.group('post')}"
    )


def parse(body: str, url: str) -> List[Tag]:
    post = _post(body)
    if not post:
        return []

    tags: List[Tag] = []
    for name in _tag_names(post.get("tags")):
        tags.append(Tag(name=name.replace(" ", "_"), source=TagSource.BOORU,
                        namespace="general"))

    artist = _artist_name(post.get("service"), post.get("user"), url)
    if artist:
        # The site's own namespace, so the user's artist -> creator remap
        # fires exactly as it does for every other parser here.
        tags.append(Tag(name=artist.replace(" ", "_"), source=TagSource.BOORU,
                        namespace="artist"))
    return tags


def parse_preview_url(body: str, url: str) -> Optional[str]:
    path = _primary_path(body)
    return f"{THUMBNAIL_BASE}{path}" if path else None


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The full-resolution original, when the archive actually holds it.

    "has_full" is the site's own word for that, and it is a statement
    about what has been imported, not about permissions: a false one goes
    with preview_state "pending", and its file 404s for everyone. Only a
    post the archive has finished scraping has an original to point at,
    so guessing a URL for the others would produce a dead link that looks
    like a broken parser.
    """
    post = _post(body)
    if not post or not post.get("has_full"):
        return None
    file_entry = _primary_entry(post)
    if file_entry is None:
        return None
    path = file_entry["path"]
    name = file_entry.get("name")
    # The site appends the original filename; harmless, and it gives
    # anything downloading this something better than a bare hash.
    suffix = f"?f={quote(name)}" if isinstance(name, str) and name else ""
    return f"{FILE_BASE}{path}{suffix}"


def parse_dimensions(body: str, url: str):
    """Width and height of the original, read from its first bytes.

    The API has no dimensions at all, but file.pawchive.pw serves ranged
    requests, and an image's header states its size. Only for a post whose
    original the archive holds - the same condition parse_file_url uses -
    since the others 404. Keyed by the file path, which is its SHA-256, so
    the same image in two posts is only read once.
    """
    post = _post(body)
    if not post or not post.get("has_full"):
        return None, None
    path = _primary_path(body)
    if not path:
        return None, None
    return _file_dimensions(path)


def parse_page_count(body: str, url: str) -> int:
    post = _post(body)
    if not post:
        return 1
    attachments = post.get("attachments")
    if isinstance(attachments, list) and attachments:
        return len(attachments)
    return 1


# -- which image of a post ---------------------------------------------
# Opting in to search_engine._resolve_multipage_candidate, which otherwise
# leaves every multi-image post on its first image - so a match on image 5
# was previewed and downloaded as image 1.
_SHA256_IN_PATH_RE = re.compile(r"/([0-9a-f]{64})\.[A-Za-z0-9]+$")


def pages_api_url(url: str) -> Optional[str]:
    """The post's own API record, which lists every file in it."""
    fetch_url = resolve_fetch_url(url)
    return fetch_url if fetch_url != url else None


def parse_pages(body: str, url: str) -> List[dict]:
    """One entry per file, in the site's order, shaped like Pixiv's page
    list. Each carries the file's SHA-256 - its path IS the hash - so the
    page holding the local file can be named exactly, without comparing
    a single picture."""
    post = _post(body)
    if not post:
        return []
    has_full = bool(post.get("has_full"))
    pages, seen = [], set()
    for holder in (post.get("file"), *(post.get("attachments") or [])):
        if not isinstance(holder, dict):
            continue
        path = holder.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path in seen:
            continue
        seen.add(path)
        thumbnail = f"{THUMBNAIL_BASE}{path}"
        sha = _SHA256_IN_PATH_RE.search(path)
        pages.append({
            "urls": {
                "original": f"{FILE_BASE}{path}" if has_full else None,
                "regular": thumbnail,
                "small": thumbnail,
            },
            "width": None,
            "height": None,
            "sha256": sha.group(1) if sha else None,
        })
    return pages


# -- internals --------------------------------------------------------
def _post(body: str) -> Optional[dict]:
    if not body:
        return None
    try:
        data = json_of(body)
    except ValueError:
        # The account-gated HTML page, because the URL had no post id to
        # turn into an API call. Not an error - just nothing.
        log.debug("Pawchive response was not JSON (the account-gated page?)")
        return None
    if not isinstance(data, dict):
        return None
    # Some Kemono revisions wrap the post; others are the post.
    inner = data.get("post")
    if isinstance(inner, dict):
        data = inner
    return data if ("id" in data or "file" in data or "attachments" in data) else None


def _primary_entry(post: dict) -> Optional[dict]:
    """The post's main image entry. "file" and attachments[0] are usually
    the same entry, and "file" is preferred as the field the site itself
    calls primary - but "file" can be an empty {} with every image under
    attachments (patreon/6714576/post/154701291), so it is only a first
    choice, never the only one."""
    for holder in (post.get("file"), *(post.get("attachments") or [])):
        if isinstance(holder, dict):
            path = holder.get("path")
            if isinstance(path, str) and path.startswith("/"):
                return holder
    return None


def _primary_path(body: str) -> Optional[str]:
    post = _post(body)
    entry = _primary_entry(post) if post else None
    return entry["path"] if entry else None


def _file_dimensions(path: str) -> Tuple[Optional[int], Optional[int]]:
    from . import USER_AGENT  # local: core.boorus imports this module
    return _remote_size.image_size(f"{FILE_BASE}{path}", USER_AGENT)


def _tag_names(raw) -> List[str]:
    """Tag names out of whatever the API put in "tags".

    Normally a Postgres array literal in a string - {a,b,"c, d"} - where
    a quoted element can itself contain the separator, so this cannot be
    a plain split. csv handles the quoting rules for us. A real JSON list
    is accepted too, in case the API is ever tidied up.
    """
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
    if not text:
        return []
    try:
        fields = next(csv.reader(io.StringIO(text), quotechar='"', escapechar="\\"))
    except (csv.Error, StopIteration):
        log.debug("Could not read Pawchive tag list: %r", raw[:120])
        return []
    return [f.strip() for f in fields if f and f.strip()]


def _artist_name(service, user, url: str) -> Optional[str]:
    """The creator's display name, which the post itself doesn't carry.

    Worth a second request: plenty of posts have no tags of their own, so
    without this they would contribute nothing at all. Cached per creator,
    bounded by its own timeout, and any failure just means no artist tag.
    """
    if not service or not user:
        return None
    key = f"{service}/{user}"
    cached = _artist_cache.get(key)
    if cached is not None:
        return cached or None  # "" is a remembered miss, not a cache gap

    from . import USER_AGENT  # local: core.boorus imports this module

    host = urlparse(url).netloc or HOSTS[0]
    profile_url = f"https://{host}/api/v1/{service}/user/{user}/profile"
    try:
        resp = net.get(
            profile_url, timeout=ARTIST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        name = (resp.json() or {}).get("name") if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as exc:
        log.debug("Could not read the Pawchive creator name from %s: %s", profile_url, exc)
        name = None

    name = name.strip() if isinstance(name, str) else None
    _artist_cache[key] = name or ""
    return name
