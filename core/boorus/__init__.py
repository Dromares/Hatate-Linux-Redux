"""Booru page parsers.

Each parser turns a booru's HTML page into a list of namespaced Tag
objects, plus (when the page exposes one) the direct URL to the actual
full-resolution image file - as opposed to the booru's own post/viewer
page, which serves HTML rather than the image itself and would give
wrong results if used for anything that expects an image response (like
checking the real file's size or format).

To add support for a new site, add a module exposing `matches(url) ->
bool`, `parse(html, url) -> List[Tag]`, and `parse_file_url(html, url) ->
Optional[str]`, then register it in PARSERS below. If the site has an API
that's more reliable to fetch than its HTML page (see e621.py), also
expose `resolve_fetch_url(url) -> str` to redirect what actually gets
fetched - `parse`/`parse_file_url` still receive that response body, with
`url` remaining the original post-page URL for logging/context. A parser
can also optionally expose `parse_preview_url(html, url) -> Optional[str]`
for a mid-resolution "sample" image, if the site has one distinct from
both the tiny search-engine thumbnail and the full-resolution original -
used to show a sharper preview without the cost of downloading the
original just for display.

A site that STATES its original's format and byte size (rather than
leaving them to be measured) can expose `parse_file_info(body, url) ->
(format_or_None, size_bytes_or_None)`, where format uses the same names
as fetch_remote_info's ("JPEG", "PNG", …). That saves a HEAD request,
and it is the only way to report either at all for a site whose original
file isn't reachable - otherwise the HEAD falls back to the search
engine's thumbnail and describes that instead of the match.

A site whose own URLs no longer resolve needs something else to look a
post up by. Such a parser exposes `resolve_fetch_url_with_context(url,
context) -> str` INSTEAD of resolve_fetch_url; context carries
"local_md5", the hash of the file being searched, computed only when a
parser actually asks for it. It can then expose `parse_canonical_url(
body, url) -> Optional[str]` to say where the post really lives, and the
caller repoints the match there so the user isn't handed a dead link.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, runtime_checkable

import requests

from ..lru_cache import LRUCache
from ..parser_health import record_empty, record_failure, record_success
from ..models import Tag
from . import animepictures, danbooru, deviantart, e621, eshuushuu, gelbooru, joyreactor, mangadex, moebooru, paheal, pawchive, pixiv, redditpost, rule34, rule34us, safebooru, sankaku, twitter, xbooru, zerochan
from ..applog import get_logger
from .. import net
from ..site_access import capture_rotated_deviantart_cookies
from ..hard_timeout import HardTimeoutError
from ..progress_ticker import OnTick, ProgressTicker

log = get_logger("boorus")

# Mirrors the bound used for availability checks: far enough to reach a
# deletion notice, never the whole page.
SOFT_404_SCAN_BYTES = 60_000

PARSERS = [danbooru, gelbooru, safebooru, moebooru, pixiv, e621, zerochan, eshuushuu, animepictures, rule34, xbooru, paheal, rule34us, redditpost, deviantart, pawchive, sankaku, twitter, mangadex, joyreactor]

# Some APIs (e621's in particular) require a descriptive User-Agent and
# block generic or browser-impersonating ones.
USER_AGENT = net.USER_AGENT


@runtime_checkable
class SiteParser(Protocol):
    """What a module in PARSERS has to provide, written down.

    Until this existed the contract lived entirely in the hasattr() calls
    in fetch_page_info, which has two consequences worth naming. A hook
    with a misspelled name is not an error - it is a hook that silently
    never runs, forever, exactly like the parsers that returned zero tags
    for as long as they had been supported (see core/parser_health.py).
    And nothing told a new parser's author what to write.

    Only two members are REQUIRED, and they are the two every parser has
    to have for the registry to route anything to it at all:

        matches(url)          claim a URL as this site's
        parse(body, url)      the tags, possibly none

    Everything else is optional and discovered by hasattr at the call
    site, which is deliberate: most sites are single-image, and a parser
    that says nothing about page lists or restrictions should not have to
    write stubs to say so. They are declared here anyway, so that the set
    is discoverable in one place rather than by reading fetch_page_info.

    This is checked by a test over PARSERS rather than only by mypy,
    because the registry is a list of MODULES - which no type checker
    inspects - and a typo is the failure being guarded against.
    """

    def matches(self, url: str) -> bool: ...
    def parse(self, body: str, url: str) -> List[Tag]: ...


# The optional hooks, as (name, what it is for). fetch_page_info and the
# multi-page resolver look each of these up with hasattr; this list is
# what lets a test assert that any parser defining one has spelled it
# correctly, which is the failure a duck-typed registry cannot otherwise
# catch.
OPTIONAL_PARSER_HOOKS = {
    "resolve_fetch_url": "fetch something other than the URL itself (an API, usually)",
    "resolve_fetch_url_with_context": "same, when the choice needs the local file's hash",
    "parse_file_url": "the full-resolution image",
    "parse_preview_url": "a mid-size sample image",
    "parse_dimensions": "width and height of the original",
    "parse_rating": "the post's age rating, where the site states one",
    "parse_file_info": "format and byte size of the original",
    "parse_page_count": "how many images the post holds",
    "parse_pages": "the per-page URLs, for multi-page posts",
    "pages_api_url": "where that page list is fetched from",
    "parse_restriction": "the post exists but the site won't serve it",
    "restriction_for_status": "the same, recognised from an HTTP status",
    "parse_canonical_url": "where a legacy URL actually lives now",
    "incomplete_reason": "why a match came back without its tags or its file",
}


class BooruError(Exception):
    pass


class BooruContentGoneError(BooruError):
    """The post itself is definitively gone (HTTP 404/410) - the content
    was deleted or removed, as opposed to a transient failure like a
    timeout, a rate-limit, or a server error. Subclasses BooruError so
    existing handlers that just want "the fetch didn't work" keep
    working unchanged, while callers that care about the difference can
    catch this specifically."""


@dataclass
class BooruPageInfo:
    tags: List[Tag] = field(default_factory=list)
    file_url: Optional[str] = None     # direct link to the actual full-resolution image file
    preview_url: Optional[str] = None  # mid-resolution "sample" image, if the site has one
    width: Optional[int] = None        # the source image's real dimensions, if the site reports them
    height: Optional[int] = None
    file_format: Optional[str] = None  # e.g. "JPEG" - only when the site STATES it, which beats
                                        # HEADing the file, and is the only way to know at all for a
                                        # site whose original isn't reachable (Anime-Pictures)
    file_size_bytes: Optional[int] = None  # the ORIGINAL's size, likewise stated rather than measured
    rating: Optional[str] = None       # the post's age rating as the SITE states it ("explicit"),
                                        # from a named API field or the Statistics sidebar - never
                                        # inferred. Kept out of `tags` on purpose: whether it
                                        # becomes a tag at all is the user's setting, and the
                                        # parser-health counters below judge a parser by the tags
                                        # it actually scraped
    gone_reason: Optional[str] = None  # set when the fetched page itself says the post is
                                        # deleted. Moebooru sites keep a deleted post's URL alive
                                        # and answer HTTP 200, so the status code reveals nothing
                                        # and only the body does
    page_count: int = 1                # images under this one URL. >1 means file_url, preview_url
                                        # and the dimensions above all describe only the FIRST of
                                        # them, so the caller has to establish which one matched
    incomplete_reason: Optional[str] = None  # why this parser produced no tags, when it KNOWS why.
                                        # Distinct from restricted (the post is gated) and from a
                                        # dead link (it is gone): the post is fine and reachable,
                                        # this app just cannot get its tags. Without it a hash-keyed
                                        # miss is indistinguishable from a silently broken parser -
                                        # which is exactly how it looked from the outside
    restricted: Optional[str] = None   # set when the site holds the post back behind an account
                                        # tier (Danbooru's Gold-only banned and censored posts),
                                        # with a short reason. The post still EXISTS - it just
                                        # can't be viewed - so this is deliberately kept distinct
                                        # from a dead link
    canonical_url: Optional[str] = None  # where this post ACTUALLY lives now, when the URL we were
                                        # given is a dead legacy route. Sankaku's old numeric links
                                        # are the case: they cannot be opened at all any more, so
                                        # leaving one in place hands the user a broken link
    fetched_url: Optional[str] = None  # what was actually requested (an API mirror, for some sites)
    body: Optional[str] = field(default=None, repr=False)  # and what came back - kept so a
                                        # follow-up that wants the very same URL (Pawchive's page
                                        # list IS its post record) doesn't fetch it a second time
    fetched: bool = False              # True only if a parser actually fetched the page. False means
                                        # no parser matched this site, so nothing was requested at all -
                                        # importantly NOT the same as "fetched and found nothing".


def find_parser(url: str):
    for parser in PARSERS:
        if parser.matches(url):
            return parser
    return None


# Reading a file again to hash it is the expensive part on a network
# share, and a post's several candidates would each ask for the same one.
_md5_cache = LRUCache(256)


def local_md5(path: Optional[str]) -> Optional[str]:
    """The local file's MD5, or None if it can't be read.

    Only used by parsers that have no other way to identify a post -
    Sankaku, whose legacy URLs no longer resolve to anything, so the file
    itself is the only key left. Keyed on the file's identity rather than
    its name so an edited file isn't matched by a stale digest.
    """
    if not path:
        return None
    try:
        stat = os.stat(path)
        key = (path, stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        log.debug("Could not stat %s for hashing: %s", path, exc)
        return None

    cached = _md5_cache.get(key)
    if cached is not None:
        return cached or None

    digest = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        log.debug("Could not read %s for hashing: %s", path, exc)
        _md5_cache[key] = ""
        return None

    value = digest.hexdigest()
    _md5_cache[key] = value
    return value


def fetch_page_info(
    url: str, timeout: float = 30.0, on_tick: Optional[OnTick] = None,
    cookies: Optional[dict] = None, headers: Optional[dict] = None,
    gone_markers: tuple = (), local_path: Optional[str] = None,
    gone_redirect: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
    settings=None,
) -> BooruPageInfo:
    """Fetches a booru post page once and extracts its tags, direct file
    URL, and preview URL, so callers needing all three don't make three
    requests. If given, on_tick(label, remaining, total) fires roughly
    once a second while the request is in flight.

    `settings`, when given, lets a DeviantArt fetch save a renewed session
    cookie it gets back (see capture_rotated_deviantart_cookies) - every
    other parser ignores it. Omitted by callers using ad hoc cookies that
    were never read from settings in the first place (the Settings
    dialog's "Test DeviantArt…" button), so an in-progress, unsaved paste
    can't get merged into the saved cookie string."""
    parser = find_parser(url)
    if parser is None:
        log.debug("No parser matches %s, skipping booru page fetch", url)
        return BooruPageInfo()

    # A parser can define resolve_fetch_url to fetch something other than
    # the post page itself - e621's parser uses this to hit its official
    # JSON API instead of scraping HTML.
    fetch_url = url
    if hasattr(parser, "resolve_fetch_url_with_context"):
        # A site whose own URLs can't be resolved any more needs something
        # else to look the post up by. The local file's md5 is computed
        # only for parsers that ask for it, since on a network share it
        # means reading the file again.
        try:
            context = {"local_md5": local_md5(local_path) if local_path else None}
            fetch_url = parser.resolve_fetch_url_with_context(url, context)
        except Exception as exc:
            log.warning("resolve_fetch_url_with_context raised for %s: %s "
                        "(fetching the page URL instead)", url, exc)
            fetch_url = url
    elif hasattr(parser, "resolve_fetch_url"):
        try:
            fetch_url = parser.resolve_fetch_url(url)
        except Exception as exc:
            log.warning("resolve_fetch_url raised for %s: %s (fetching the page URL instead)", url, exc)
            fetch_url = url

    log.debug("Fetching %s (parser: %s)", fetch_url, parser.__name__.rsplit(".", 1)[-1])
    try:
        with ProgressTicker("Fetching booru page", timeout, on_tick):
            resp = net.get(fetch_url, headers=headers or {"User-Agent": USER_AGENT},
                           timeout=timeout, deadline=timeout, cookies=cookies or None)
    except HardTimeoutError as exc:
        log.error("Fetching %s exceeded the hard deadline: %s", fetch_url, exc)
        raise BooruError(f"Fetching {fetch_url} timed out: {exc}") from exc
    except requests.RequestException as exc:
        log.error("Could not fetch %s: %s", fetch_url, exc)
        record_failure(parser.__name__.rsplit(".", 1)[-1], url, str(exc)[:120])
        raise BooruError(f"Could not fetch {fetch_url}: {exc}") from exc
    if settings is not None:
        capture_rotated_deviantart_cookies(fetch_url, resp, settings)
    # A server that sends "text/html" with no charset makes requests fall
    # back to ISO-8859-1, which turns every UTF-8 character into mojibake:
    # "×" becomes "Ã—", and a Japanese artist tag becomes noise. e-shuushuu
    # does exactly this, and it was silently corrupting its non-ASCII tags
    # and defeating the dimension parser. Assume UTF-8 when nothing was
    # declared - it is what these sites actually serve.
    if resp.encoding and "charset" not in (resp.headers.get("Content-Type") or "").lower():
        log.debug("%s declared no charset; decoding as UTF-8 rather than %s",
                  fetch_url, resp.encoding)
        resp.encoding = "utf-8"
    # Decoded ONCE. resp.text decodes the whole body again on every read,
    # and every hook below reads it - and handing each the same string is
    # also what lets _parsed.soup_of/json_of find their cached parse by
    # identity.
    body = resp.text

    log.debug("%s -> HTTP %d", fetch_url, resp.status_code)
    if resp.status_code in (404, 410):
        log.info("Post is gone (HTTP %d): %s", resp.status_code, fetch_url)
        raise BooruContentGoneError(f"{fetch_url} returned HTTP {resp.status_code} - the post is gone")
    if resp.status_code != 200:
        # A parser can recognise its site's "you need an account" status
        # and say so. Without this the response is just a failed fetch,
        # indistinguishable from a timeout or a rate-limit, and the user
        # is left with a blank match and no idea that signing in would
        # fill it in.
        if hasattr(parser, "restriction_for_status"):
            try:
                reason = parser.restriction_for_status(resp.status_code, body, url)
            except Exception as exc:
                log.warning("restriction_for_status raised for %s: %s", url, exc)
                reason = None
            if reason:
                log.info("%s is account-restricted (HTTP %d): %s", url, resp.status_code, reason)
                return BooruPageInfo(restricted=reason, fetched=True)
        raise BooruError(f"{fetch_url} returned HTTP {resp.status_code}")

    # Some sites keep a deleted post's URL alive and answer HTTP 200 with
    # a notice where the image should be. The body is already in hand
    # here, so checking costs nothing - and it's the ONLY check a
    # single-candidate match gets, since the availability sweep only runs
    # when there's more than one candidate to choose between.
    gone_reason = None
    # A redirect can be the "gone" signal instead of any body text.
    # Gelbooru has no deleted-post wording at all: it answers HTTP 200
    # and bounces to its post LIST, so nothing in the body says
    # anything. That left a single-candidate Gelbooru match with NO
    # check whatsoever - the sweep below only runs when there is more
    # than one candidate - and a deleted post went straight into the
    # dropdown. The final URL is already in hand here, so this costs
    # nothing.
    if gone_redirect is not None:
        landed = gone_redirect(url, getattr(resp, "url", None))
        if landed:
            gone_reason = f"redirected to {landed}"
            log.info("%s redirected to the site's \"no such post\" page (%r)", url, landed)
    if gone_reason is None and gone_markers:
        lowered_body = body[:SOFT_404_SCAN_BYTES].lower()
        for marker in gone_markers:
            if marker in lowered_body:
                gone_reason = marker
                log.info("%s says the post has been deleted (matched %r)", url, marker)
                break

    # Guarded like every other hook below. It used to be a bare call, and
    # it was the only one: a parser that raised here lost the whole match
    # rather than just its tags, and one that didn't define parse() at all
    # raised AttributeError and dropped every match for its site. Losing
    # the tags is the correct blast radius - the URL, file and preview are
    # parsed further down and are still worth having.
    tags: List[Tag] = []
    if hasattr(parser, "parse"):
        try:
            tags = parser.parse(body, url) or []
        except Exception as exc:
            log.warning("parse raised for %s: %s (continuing without its tags)", url, exc)
    else:
        log.warning("Parser %s defines no parse(); no tags from %s",
                    parser.__name__.rsplit(".", 1)[-1], url)

    file_url = None
    if hasattr(parser, "parse_file_url"):
        try:
            file_url = parser.parse_file_url(body, url)
        except Exception as exc:
            log.warning("parse_file_url raised for %s: %s (continuing without it)", url, exc)
            file_url = None  # never let a file-url parsing slip take down tag fetching

    preview_url = None
    if hasattr(parser, "parse_preview_url"):
        try:
            preview_url = parser.parse_preview_url(body, url)
        except Exception as exc:
            log.warning("parse_preview_url raised for %s: %s (continuing without it)", url, exc)
            preview_url = None

    restricted = None
    if hasattr(parser, "parse_restriction"):
        try:
            restricted = parser.parse_restriction(body, url)
        except Exception as exc:
            log.warning("parse_restriction raised for %s: %s (continuing without it)", url, exc)

    page_count = 1
    if hasattr(parser, "parse_page_count"):
        try:
            page_count = max(1, int(parser.parse_page_count(body, url) or 1))
        except Exception as exc:
            log.warning("parse_page_count raised for %s: %s (assuming a single image)", url, exc)

    width, height = None, None
    if hasattr(parser, "parse_dimensions"):
        try:
            width, height = parser.parse_dimensions(body, url)
        except Exception as exc:
            log.warning("parse_dimensions raised for %s: %s (continuing without it)", url, exc)

    rating = None
    if hasattr(parser, "parse_rating"):
        try:
            rating = parser.parse_rating(body, url)
        except Exception as exc:
            log.warning("parse_rating raised for %s: %s (continuing without it)", url, exc)

    file_format, file_size_bytes = None, None
    if hasattr(parser, "parse_file_info"):
        try:
            file_format, file_size_bytes = parser.parse_file_info(body, url)
        except Exception as exc:
            log.warning("parse_file_info raised for %s: %s (falling back to a HEAD)", url, exc)
            file_format, file_size_bytes = None, None

    canonical_url = None
    if hasattr(parser, "parse_canonical_url"):
        try:
            canonical_url = parser.parse_canonical_url(body, url)
        except Exception as exc:
            log.warning("parse_canonical_url raised for %s: %s (keeping the original)", url, exc)

    parser_name = parser.__name__.rsplit(".", 1)[-1]
    incomplete_reason = None
    if (not tags or not file_url) and hasattr(parser, "incomplete_reason"):
        # Asked when the match came back missing something a complete one
        # would have - its tags, its file, or both - and only of a parser
        # with something to say about why.
        #
        # The file matters as much as the tags here, and measurably more
        # for some sites: e621 hands back a withheld post's tags in full
        # and nulls only its file url, so a tags-only condition would
        # never fire for the case that actually needs explaining.
        try:
            incomplete_reason = parser.incomplete_reason(body, url)
        except Exception as exc:
            log.warning("incomplete_reason raised for %s: %s", url, exc)
    if not tags:
        # For most sites an empty result means the markup moved. For one
        # that finds posts by file hash, a miss just means the local file
        # isn't the site's copy - the common case, and not worth a warning
        # on every single match.
        if getattr(parser, "EMPTY_RESULT_IS_NORMAL", False):
            log.debug("No match for %s (parser: %s)", url, parser_name)
        else:
            log.warning(
                "No tags extracted from %s - the site's markup may have changed "
                "(parser: %s)", url, parser_name,
            )
            # Counted, so a parser that has quietly stopped working can be
            # noticed by the app rather than only by the user's eye.
            record_empty(parser_name, url)
    else:
        record_success(parser_name, len(tags))
        log.debug(
            "Extracted %d tag(s), file_url=%s, preview_url=%s, dimensions=%sx%s from %s",
            len(tags), file_url, preview_url, width, height, url,
        )

    if restricted:
        log.info("%s is restricted: %s", url, restricted)

    return BooruPageInfo(
        tags=tags, file_url=file_url, preview_url=preview_url,
        width=width, height=height, page_count=page_count,
        file_format=file_format, file_size_bytes=file_size_bytes, rating=rating,
        canonical_url=canonical_url,
        restricted=restricted, gone_reason=gone_reason, fetched=True,
        incomplete_reason=incomplete_reason, fetched_url=fetch_url, body=body,
    )


def fetch_tags(url: str, timeout: float = 30.0) -> List[Tag]:
    """Back-compat wrapper for callers that only want the tags."""
    return fetch_page_info(url, timeout).tags
