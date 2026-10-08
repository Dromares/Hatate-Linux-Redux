"""DeviantArt, via the JSON state embedded in its deviation pages.

DeviantArt publishes no booru-style tag vocabulary, so this is a limited
parser by nature: what comes out is the artist, the real file's size and
dimensions, and a picture - not a tag list.

It reads window.__INITIAL_STATE__ out of the deviation page rather than
using the public oEmbed endpoint, which was the obvious first choice and
is the wrong one. oEmbed describes only the downscaled render it hands
over: its width/height are the render's, and it names no file size at
all. The page state carries the ORIGINAL's type, dimensions and byte
size, even for an adult deviation viewed logged-out, where only the
picture is withheld and the facts about it are not.

Fetching the page also needs no URL juggling. oEmbed accepts only the
canonical /{artist}/art/{slug}-{id} form and 404s on the /view/{id} links
search engines produce, which meant resolving a redirect first; the page
answers on any of them.

Two things this deliberately does NOT report:

  * A file URL. The original answers 403 even with the page's own signing
    token, so every URL here is a render. Passing a render off as the
    match's file would have it downloaded and sent to Hydrus as though it
    were the artwork.
  * Anything for a deviation whose state is missing. A page that didn't
    load its state yields nothing rather than a guess.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from ..applog import get_logger
from ..models import Tag, TagSource
from ._host import on_host

log = get_logger("boorus.deviantart")

HOSTS = ("deviantart.com", "fav.me")

# The page embeds its state as JSON.parse("…") - a JSON document inside a
# JS string literal, so it needs unwrapping twice.
#
# The end anchor has to be the end of the LINE, not just the first ");".
# That string contains inline JS of its own (…__REER__.push({n,d});…), so
# a non-greedy match to ");" alone stops 13KB in and silently truncates a
# 427KB document - which parses as nothing and looks exactly like a page
# that carried no state at all.
STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*JSON\.parse\((.*?)\);\s*$", re.S | re.M)

# Renders are served from paths carrying transformation parameters; the
# unadorned baseUri is the original (which DeviantArt will not serve us).
RENDER_MARKER = "/v1/"

# Best picture first. "fullview" is the largest render DeviantArt offers
# without a download, and is what the site itself shows.
PREVIEW_TYPES = ("fullview", "preview", "400T", "350T", "300W")

# originalFile.type -> the names fetch_remote_info derives from a
# Content-Type, so the UI shows one vocabulary whatever the source.
FILE_TYPE_FORMATS = {
    "jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "gif": "GIF",
    "webp": "WEBP", "bmp": "BMP", "avif": "AVIF", "mp4": "MP4", "webm": "WEBM",
}


def matches(url: str) -> bool:
    return on_host(url, HOSTS)


def parse(body: str, url: str) -> List[Tag]:
    """The artist, as a creator tag. The page state exposes no others.

    Stored under the site's own "artist" namespace so the user's
    artist -> creator remap fires, matching every other parser here.
    """
    deviation, _, username = _deviation(body, url)
    if not username:
        return []
    return [Tag(name=username.replace(" ", "_"), source=TagSource.BOORU, namespace="artist")]


def parse_file_info(body: str, url: str):
    """(format, size_in_bytes) of the ORIGINAL file.

    This is the reason the page is parsed rather than oEmbed. There is no
    reachable file URL to HEAD, so without the site stating these they
    could not be reported at all - and the HEAD would instead measure the
    search engine's thumbnail and present that as the match's size.
    """
    original = _original_file(body, url)
    if not original:
        return None, None

    fmt = None
    file_type = original.get("type")
    if isinstance(file_type, str):
        fmt = FILE_TYPE_FORMATS.get(file_type.strip().lower().lstrip("."))

    size = original.get("filesize")
    try:
        size = int(size) if size else None
    except (TypeError, ValueError):
        size = None
    if size is not None and size <= 0:
        size = None

    return fmt, size


def parse_dimensions(body: str, url: str):
    """The ORIGINAL's dimensions - not the render's.

    The app compares these against the local file to decide whether a
    match is an upgrade, so the render's size (which is what oEmbed
    reports) would present real upgrades as downgrades.
    """
    original = _original_file(body, url)
    if not original:
        return None, None
    try:
        width = int(original.get("width") or 0) or None
        height = int(original.get("height") or 0) or None
    except (TypeError, ValueError):
        return None, None
    return (width, height) if width and height else (None, None)


def parse_preview_url(body: str, url: str) -> Optional[str]:
    return _render_url(body, url)


def parse_file_url(body: str, url: str) -> Optional[str]:
    """The original, which DeviantArt does not serve.

    Its baseUri answers 403 even with the page's own token, so anything
    obtainable here is a render. Only a URL that carries no render
    parameters is offered, on the chance that ever changes.
    """
    rendered = _render_url(body, url)
    if rendered and RENDER_MARKER not in rendered:
        return rendered
    return None


def parse_restriction(body: str, url: str) -> Optional[str]:
    """Why this deviation isn't fully visible, or None if it is.

    An adult deviation viewed logged-out has the blur baked into the
    render path DeviantArt signs (blur_73), so nothing recovers the real
    picture but a login.
    """
    rendered = _render_url(body, url)
    if rendered and re.search(r"[,/]blur_\d+", rendered):
        return (
            "DeviantArt is only serving a blurred copy of this adult deviation, which is "
            "what it shows when logged out. Add cookies from a logged-in account under "
            "Settings > Site Logins to see it properly."
        )
    return None


# -- state extraction -------------------------------------------------
def _state(body: str) -> Optional[dict]:
    match = STATE_RE.search(body or "")
    if not match:
        log.debug("No __INITIAL_STATE__ in the DeviantArt response")
        return None
    raw = match.group(1)
    try:
        # \' is legal in a JS string and rejected by JSON, and the page
        # does contain it. Nothing else in there needs fixing.
        return json.loads(json.loads(raw.replace("\\'", "'")))
    except ValueError as exc:
        log.debug("Could not parse DeviantArt page state: %s", exc)
        return None


def _deviation(body: str, url: str):
    """(deviation, extended, artist_username) for the page's OWN deviation.

    A page also carries entries for related deviations, so the right one
    has to be picked deliberately. "deviationExtended" holds only the one
    the page is about, which makes it the anchor; the id in the URL is
    used to disambiguate if that ever stops being true.
    """
    state = _state(body)
    if not state:
        return None, None, None
    entities = state.get("@@entities")
    if not isinstance(entities, dict):
        return None, None, None
    extended_table = entities.get("deviationExtended")
    deviation_table = entities.get("deviation")
    if not isinstance(extended_table, dict) or not extended_table:
        return None, None, None

    key = None
    wanted = re.search(r"(\d+)(?:\?|#|$)", url or "")
    if wanted and wanted.group(1) in extended_table:
        key = wanted.group(1)
    elif len(extended_table) == 1:
        key = next(iter(extended_table))
    if key is None:
        log.debug("Could not tell which of %d deviations this page is about", len(extended_table))
        return None, None, None

    extended = extended_table.get(key) or {}
    deviation = (deviation_table or {}).get(key) or {}

    username = None
    users = entities.get("user")
    author_id = deviation.get("author")
    if isinstance(users, dict) and author_id is not None:
        username = (users.get(str(author_id)) or {}).get("username")
    return deviation, extended, username


def _original_file(body: str, url: str) -> Optional[dict]:
    _, extended, _ = _deviation(body, url)
    original = (extended or {}).get("originalFile")
    return original if isinstance(original, dict) else None


def _render_url(body: str, url: str) -> Optional[str]:
    """The best picture DeviantArt will actually serve for this deviation.

    Built rather than read: the state gives a base URI, a signing token
    and a set of path templates with the file's name substituted in.
    """
    deviation, _, _ = _deviation(body, url)
    media = (deviation or {}).get("media")
    if not isinstance(media, dict):
        return None
    base = media.get("baseUri")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        return None

    types = {t.get("t"): t for t in media.get("types") or [] if isinstance(t, dict)}
    chosen = next((types[name] for name in PREVIEW_TYPES if name in types), None)
    if chosen is None:
        return None
    path = chosen.get("c")
    if not isinstance(path, str):
        return None

    pretty = media.get("prettyName")
    if isinstance(pretty, str):
        path = path.replace("<prettyName>", pretty)

    tokens = media.get("token")
    token = tokens[0] if isinstance(tokens, list) and tokens else None
    rendered = base + path
    return f"{rendered}?token={token}" if isinstance(token, str) and token else rendered
