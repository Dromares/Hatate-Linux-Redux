"""Is a match's page still there? Status codes, soft 404s, redirects to
a site's index, and Sankaku's API - one definition of "gone".

Split out of core/search_engine.py; everything here is re-exported there.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlparse

import re

import requests

from .applog import get_logger
from .config import Settings
from . import net
from .hard_timeout import HardTimeoutError, run_with_hard_timeout
from .models import MatchCandidate
from .progress_ticker import OnTick, ProgressTicker

from .site_access import USER_AGENT, capture_rotated_deviantart_cookies, cookies_for_url, headers_for_url

log = get_logger("search")


# Hosts with no boorus/ parser (so nothing else establishes whether their
# content still exists) that are nonetheless worth an explicit
# availability check. Deliberately an opt-in list rather than "check every
# unknown host": each entry costs an extra request per candidate, and for
# a site whose deletion behaviour we haven't verified, a check could
# easily produce misleading results rather than useful ones.
AVAILABILITY_CHECK_HOSTS = (
    "deviantart.com",
    "fav.me",
    # Sankaku is classification-only (no parser), so without this nothing
    # would ever notice its posts had gone. It also doesn't 404 them - see
    # SOFT_404_REDIRECTS.
    "sankakucomplex.com",
    # Moebooru sites keep a deleted post's PAGE - tags, dimensions and all
    # - and answer HTTP 200 for it. Only the body says the file is gone.
    "yande.re",
    "konachan.com",
    "konachan.net",
    # JoyReactor/reactor.cc has no parser either, and canonicalize_url
    # (core/sites.py) optimistically rewrites its matches to a full/ file
    # that has never actually been fetched - see _reactor_full_fallback
    # below, which this check feeds.
    "reactor.cc",
    "joyreactor.com",
)


def _should_availability_check(url: str) -> bool:
    lowered = url.lower()
    return any(host in lowered for host in AVAILABILITY_CHECK_HOSTS)


# The plain (watermarked) sibling of a canonicalize_url-rewritten
# JoyReactor/reactor.cc full/ URL. Matches REACTOR_PICS_POST_RE's own
# shape once the full/ segment is in place, so it can be undone exactly.
REACTOR_FULL_RE = re.compile(r"^(https?://[^/]+/pics/post/)full/(.+)$", re.IGNORECASE)


def _reactor_full_fallback(candidate: MatchCandidate, available: Optional[bool]) -> Optional[bool]:
    """canonicalize_url rewrites a JoyReactor/reactor.cc match to its
    full/ (unwatermarked) file before anyone has confirmed that file
    exists. If the availability check just found it definitively gone,
    that isn't the post being gone - the ORIGINAL (watermarked) file the
    site actually served in its results very likely still is, so fall
    back to that rather than reporting the whole match dead over a
    rewrite that didn't pan out.

    Returns None (unknown - the fallback URL itself hasn't been checked)
    when a fallback happens, otherwise `available` unchanged."""
    if available is not False or not candidate.url:
        return available
    match = REACTOR_FULL_RE.match(candidate.url)
    if not match:
        return available
    fallback = match.group(1) + match.group(2)
    log.info("%s: full-res file is gone, falling back to %s", candidate.url, fallback)
    candidate.url = fallback
    return None


# Concurrency for the up-front availability sweep. These hit many
# different hosts, so running several at once is safe and much faster
# than waiting on each in turn - but keep it bounded so a long candidate
# list doesn't fire a dozen simultaneous requests.
AVAILABILITY_SWEEP_WORKERS = 5


def _sweep_candidate_availability(
    candidates: List[MatchCandidate], settings: Settings, filename: str,
    on_tick: Optional[OnTick] = None,
) -> None:
    """Checks every candidate's source URL up front so dead ones can be
    dropped before the user ever sees them, rather than being discovered
    one at a time as they're clicked. Mutates each candidate's
    remote_available in place.

    Only a definite 404/410 marks a candidate dead - anything ambiguous
    stays None, which is treated as alive everywhere downstream."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # Imported here rather than at module level: hydrus_import imports
    # from this module, so a top-level import would be circular.
    from .hydrus_import import normalize_url_for_hydrus

    def _check(candidate: MatchCandidate):
        if candidate.remote_available is not None:
            return candidate, candidate.remote_available  # already known
        if not candidate.url:
            return candidate, None
        # Check the post page, not the direct file - a deleted post is
        # what we care about, and for Pixiv the page must be the modern
        # /artworks/ form for the 404 to be reported correctly.
        target = normalize_url_for_hydrus(candidate.url)
        # Sent logged-in where configured: Sankaku hides account-only
        # posts from anonymous requests behind a blank page, so checking
        # without cookies would report a perfectly good post as dead.
        available = check_url_available(
            target, settings.search_timeout, cookies=cookies_for_url(target, settings),
            settings=settings,
        )
        return candidate, _reactor_full_fallback(candidate, available)

    pending = [c for c in candidates if c.remote_available is None and c.url]
    if not pending:
        return

    if on_tick:
        on_tick(f"Checking {len(pending)} match(es)", float(len(pending)), float(len(pending)))

    workers = min(AVAILABILITY_SWEEP_WORKERS, len(pending))
    gone = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="avail-sweep") as executor:
        futures = [executor.submit(_check, c) for c in pending]
        for future in as_completed(futures):
            try:
                candidate, available = future.result()
            except Exception as exc:
                log.warning("Availability sweep check raised: %s", exc)
                continue
            candidate.record_availability(available)
            if available is False:
                gone += 1

    if on_tick:
        on_tick("", 0.0, 0.0)
    if gone:
        log.info("%s: availability sweep found %d/%d match(es) gone", filename, gone, len(pending))


# HTTP statuses that mean the resource is definitively gone, as opposed to
# temporarily unreachable. Deliberately narrow: a 403 often just means
# hotlink protection or a bot check (the post is usually still there), a
# 5xx is a server problem rather than a deleted post, and a timeout says
# nothing at all about whether the content exists.
DEFINITELY_GONE_STATUSES = (404, 410)

# Some sites serve a "this post was deleted" ERROR PAGE with HTTP 200
# rather than a proper 404 - a "soft 404". Checking the status code alone
# would call these available when they're actually gone. Pixiv is a
# confirmed case: a deleted artwork renders a page reading "該当作品は削除
# されたか、存在しない作品IDです" ("this work has been deleted or the ID
# does not exist") - see the Japanese-language reports of exactly this
# error text on deleted works.
#
# Keyed by a hostname fragment, with marker strings to look for in the
# page body. Kept deliberately specific: a generic "not found" substring
# search across all sites would false-positive on any page that happens
# to contain those words in a comment or tag.
SOFT_404_MARKERS = {
    # Sankaku keeps the requested URL and answers HTTP 200 even when the
    # post shows nothing - the redirect to /posts/show_empty happens in
    # the browser, after the HTML has already been delivered, so nothing
    # about the response itself gives it away. CONFIRMED against the real
    # page source of an empty post:
    #
    #   <title>No Content | Sankaku</title>
    #   <link rel="alternate" hreflang="en"
    #         href="https://chan.sankakucomplex.com/en/posts/show_empty" />
    #   <h3>Nothing is visible to you here.</h3>
    #
    # The first two sit in <head>, within the first couple of KB, so they
    # are found well inside the bounded prefix that gets read. The <h3>
    # is listed last because it appears after the site's whole navigation
    # and news carousel, which on a big page could fall beyond the scan
    # limit - useful when it's reached, not relied upon.
    #
    # These are specific to the empty page: a real post's hreflang links
    # point at that post's own id, and its title is the post's title.
    "sankakucomplex.com": (
        "<title>no content | sankaku",
        "/posts/show_empty",
        "nothing is visible to you here",
    ),
    "pixiv.net": (
        "該当作品は削除されたか",           # "the work has been deleted or..."
        "存在しない作品idです",             # "...is a nonexistent work ID"
        "work has been deleted or the id does not exist",  # English locale
    ),
    # Moebooru sites keep a deleted post's URL alive and answer HTTP 200,
    # serving the page with a notice where the image should be - so the
    # status code says nothing and only the body reveals it.
    #
    # Confirmed wording from a live deleted post
    # (yande.re/post/show/516284): "This post was deleted."
    #
    # Anchored to the leading "this" rather than the bare phrase: a
    # comment or a wiki excerpt could easily discuss a post being
    # deleted, whereas the site's own notice starts this way.
    # A deleted post also swaps the real image for a placeholder, which
    # the live post beside it does not have - an independent signal, so
    # a change of wording doesn't take detection with it.
    #
    # Verified against a live post specifically, because every Moebooru
    # page renders a block of hidden template strings ("This user name
    # doesn't exist...") whether or not they apply: a marker drawn from
    # that block would report every Moebooru match as deleted.
    "yande.re": ("this post was deleted", "/assets/stubs/"),
    "konachan.com": ("this post was deleted", "/assets/stubs/"),
    "konachan.net": ("this post was deleted", "/assets/stubs/"),
}

# Some sites answer a missing post by REDIRECTING to a generic "nothing
# here" page rather than returning 404 - so the request succeeds, the
# final page is valid HTML, and a status-code check sees HTTP 200.
# Sankaku does this: a post that's gone (or not visible without an
# account) lands on /posts/show_empty.
#
# Detected on the FINAL URL after redirects rather than by scanning the
# page text, which is a much stronger signal: it doesn't depend on the
# site's wording, its language, or its markup staying put. Keyed by
# hostname fragment, with path fragments that mean "there's nothing here".
SOFT_404_REDIRECTS = {
    # CONFIRMED from a live check: a post that shows nothing redirects to
    # /errors/not_found - and onto a DIFFERENT subdomain, www rather than
    # the chan host the request went to. That page carries no title and
    # none of the "No Content" wording the logged-in browser view shows,
    # so the body markers below can't see it; the landing path is the
    # only reliable signal.
    #
    # /posts/show_empty is kept alongside it because the logged-in
    # browser view does use that form - the two appear to be different
    # routes to the same "nothing here" outcome.
    "sankakucomplex.com": ("/errors/not_found", "/posts/show_empty"),
    # VERIFIED: requesting a post that no longer exists, e.g.
    # index.php?page=post&s=view&id=4154084, is answered with a
    # server-side redirect to the post LIST (page=post&s=list&tags=all).
    # HTTP 200, a completely valid page, and full of thumbnails - so
    # both a status check and any "does it look like a real page" check
    # call it alive. The giveaway is that it stopped being a post view.
    "gelbooru.com": ("s=list",),
    # Safebooru runs the same software, so it behaves the same way.
    "safebooru.org": ("s=list",),
    "rule34.xxx": ("s=list",),
    "xbooru.com": ("s=list",),
}

# Only read this much of the body when checking for a soft-404 marker -
# the marker appears in the page's own error text near the top, and this
# avoids downloading a full-size page (or worse, an image) just to check.
SOFT_404_SCAN_BYTES = 60_000


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _page_title(body: str) -> str:
    """The <title> of a fetched page, for logging. Identifies at a glance
    whether a marker miss means "this really is a live post" or "we were
    handed a bot check / age gate / login wall instead"."""
    if not body:
        return ""
    match = TITLE_RE.search(body)
    if not match:
        # No title at all is itself informative - it usually means the
        # bytes aren't HTML (a compressed or binary response read raw).
        return f"<no title; body starts {body[:60]!r}>"
    return " ".join(match.group(1).split())[:120]


def _soft_404_markers_for(url: str) -> tuple:
    lowered = url.lower()
    for host_fragment, markers in SOFT_404_MARKERS.items():
        if host_fragment in lowered:
            return markers
    return ()


def has_soft_404_detection(url: str) -> bool:
    """Whether this site needs its POST PAGE checked rather than its
    image file, because that's the only place its "gone" signal appears.

    Covers both flavours: sites that serve error TEXT with HTTP 200
    (Pixiv) and sites that REDIRECT to a generic empty page (Sankaku).
    A file URL would miss both - it just fails ambiguously, which
    correctly-but-uselessly reports "unknown".
    """
    lowered = url.lower()
    if any(fragment in lowered for fragment in SOFT_404_MARKERS):
        return True
    return any(fragment in lowered for fragment in SOFT_404_REDIRECTS)


# Landing on a site's BROWSE INDEX means the request never reached a
# post. Sankaku does this for its deprecated /post/show/{id} route, which
# is exactly the form SauceNAO still hands out.
#
# Matched on the final URL's PATH, compared exactly - never as a
# substring. The index is "/en/posts" and a real post is
# "/en/posts/4100422", so a substring test would report every successful
# post load as gone. Getting this wrong fails in the most destructive
# possible direction: silently deleting good matches.
INDEX_REDIRECT_PATHS = {
    "sankakucomplex.com": {"/posts", "/en/posts", "/", ""},
}


def _redirect_to_index(original_url: str, final_url: Optional[str]) -> Optional[str]:
    """The index path this request was bounced to, or None."""
    if not final_url or not isinstance(final_url, str):
        return None
    lowered_original = original_url.lower()
    for host_fragment, index_paths in INDEX_REDIRECT_PATHS.items():
        if host_fragment not in lowered_original:
            continue
        try:
            path = urlparse(final_url).path.rstrip("/").lower()
        except ValueError:
            return None
        # An exact match only. "/en/posts/4100422".rstrip("/") is not
        # "/en/posts", so a real post can never trip this.
        if path in index_paths:
            return path or "/"
    return None


def _redirect_means_gone(original_url: str, final_url: Optional[str]) -> Optional[str]:
    """The path fragment that proves this URL landed on a site's generic
    "nothing here" page, or None.

    Matched against the ORIGINAL url's host so a site can't be judged by
    rules belonging to whatever it redirected to.
    """
    if not final_url or not isinstance(final_url, str):
        # requests normally gives a string, but a stubbed or unusual
        # response might not - and guessing at a non-string here would
        # mean judging a live post on nonsense.
        return None
    lowered_original = original_url.lower()
    lowered_final = final_url.lower()
    for host_fragment, path_fragments in SOFT_404_REDIRECTS.items():
        if host_fragment not in lowered_original:
            continue
        for path_fragment in path_fragments:
            # The fragment must appear in where we LANDED but not in
            # where we started. Without that second half, asking for a
            # Gelbooru list page directly would report itself as gone,
            # since it legitimately contains "s=list" all along.
            if path_fragment in lowered_final and path_fragment not in lowered_original:
                return path_fragment
    return None


# Sankaku's post ids changed shape. The new ones are short alphanumeric
# strings ("y0abg167r2o"); the old ones were numbers, and every search
# engine's index is still full of those.
SANKAKU_POST_ID_RE = re.compile(
    r"sankakucomplex\.com/(?:[a-z]{2}/)?posts?/(?:show/)?([A-Za-z0-9]+)", re.IGNORECASE)
SANKAKU_API_POST = "https://sankakuapi.com/posts/{}"
# A post the API describes as anything else - deleted, or held back - is
# not something the user can open.
SANKAKU_LIVE_STATUS = "active"


def url_is_structurally_dead(url: str) -> bool:
    """Whether this URL cannot resolve for anyone, without asking anything.

    Sankaku changed the shape of its post ids. The numeric ones every
    search engine still indexes stopped resolving entirely - CONFIRMED
    against its API, which answers "invalid id" for each of them - so
    such a URL is a dead link no matter what became of the post behind
    it. That is knowable from the URL alone, which matters in two places
    a network check does not reach: a candidate can be dropped the
    moment an engine offers it, and a result cached before this was
    understood can be cleaned up when it is replayed.
    """
    found = SANKAKU_POST_ID_RE.search(url or "")
    return found is not None and found.group(1).isdigit()


def drop_structurally_dead(candidates: List[MatchCandidate], filename: str = "") -> List[MatchCandidate]:
    """Candidates whose URL could not be opened by anyone, removed."""
    live = [c for c in candidates if not url_is_structurally_dead(c.url)]
    if len(live) != len(candidates):
        log.info("%s: dropped %d match(es) whose URL can no longer resolve",
                 filename or "?", len(candidates) - len(live))
    return live


def _recheck_cached_candidates(
    candidates: List[MatchCandidate], settings: Settings, filename: str,
    on_tick: Optional[OnTick] = None,
) -> List[MatchCandidate]:
    """Re-verify a cached result's candidates before showing them.

    A cached result carries the candidates that were alive WHEN IT WAS
    SAVED, and nothing re-checks them on the way back out - so a post
    deleted since is served up again every time, however often the user
    reports it. Observed with both Sankaku and Gelbooru, neither of which
    404s a deleted post: Sankaku now answers with a login screen, and
    Gelbooru redirects to its post list, so both looked alive at save
    time and stayed cached that way.

    Uses the same sweep a fresh search does, so there is one definition
    of "gone" rather than two, and it runs concurrently. Only when the
    user has asked for dead matches to be dropped: it costs a request
    per candidate, which is the price of not being handed dead links.
    """
    _sweep_candidate_availability(candidates, settings, filename, on_tick)
    live = [c for c in candidates if c.remote_available is not False]
    if len(live) != len(candidates):
        log.info("%s: dropped %d cached match(es) that have since gone",
                 filename, len(candidates) - len(live))
    return live


def sankaku_post_exists(url: str, timeout: float,
                        cookies: Optional[dict] = None) -> Optional[bool]:
    """Whether a Sankaku post is really there, asked of its API.

    None for anything that is not a Sankaku post URL, so the caller
    carries on with the ordinary check.

    The page itself can no longer answer this. CONFIRMED against the live
    site: /en/posts/<id> now redirects anonymous visitors to
    login.sankakucomplex.com with HTTP 200, and www.sankakucomplex.com
    serves a JavaScript shell with no title - so a dead post and a live
    one look exactly alike, and every one of them was being reported as
    available.

    The API answers anonymously and says outright:

      * a NUMERIC id gets HTTP 400 "invalid id". Those ids stopped
        resolving when Sankaku changed their format, so such a URL cannot
        be opened by anyone - it is dead regardless of the post's fate.
      * a modern id gets the post, with a "status" field.

    A post can also be there and still be unusable. CONFIRMED on
    /posts/y0abg167r2o: status "active", but file_url, sample_url and
    preview_url all null and "redirect_to_signup" true - the post exists
    and its picture is withheld from anyone not signed in. There is
    nothing for this app to fetch and nothing for the user to look at, so
    it counts as gone. Configured Sankaku cookies are sent with the
    request, so a logged-in user gets their own answer rather than the
    anonymous one.
    """
    found = SANKAKU_POST_ID_RE.search(url or "")
    if not found:
        return None
    post_id = found.group(1)

    try:
        response = net.get(
            SANKAKU_API_POST.format(post_id),
            headers={"User-Agent": USER_AGENT}, timeout=timeout,
            cookies=cookies or None,
        )
    except requests.RequestException as exc:
        log.debug("Sankaku API check failed for %s (%s) - treating as unknown", url, exc)
        return None

    if response.status_code == 400 and post_id.isdigit():
        log.debug("Sankaku: %s uses a numeric id, which no longer resolves", url)
        return False
    if response.status_code in (404, 410):
        return False
    if response.status_code != 200:
        return None                     # rate limited, blocked, or down

    try:
        post = response.json()
    except ValueError:
        return None
    if not isinstance(post, dict) or not post.get("id"):
        return False
    status = str(post.get("status") or "").lower()
    if status and status != SANKAKU_LIVE_STATUS:
        log.debug("Sankaku: %s is %r rather than active", url, status)
        return False
    if not any(post.get(field) for field in ("file_url", "sample_url", "preview_url")):
        log.info(
            "Sankaku: %s exists but its picture is withheld (redirect_to_signup=%s) - "
            "nothing to fetch and nothing to look at, so it counts as gone",
            url, post.get("redirect_to_signup"),
        )
        return False
    return True


def check_url_available(
    url: Optional[str], timeout: float, referer: Optional[str] = None,
    on_tick: Optional[OnTick] = None, cookies: Optional[dict] = None,
    settings=None,
) -> Optional[bool]:
    """Whether a match's source URL still exists. See _page_says_available.

    Sankaku gets a second opinion, and only ever a harsher one. Its page
    can no longer prove a post is THERE - an anonymous visitor is
    redirected to a login screen with HTTP 200 - so a "yes" or a "don't
    know" from the page means very little, and its API is asked instead.
    A page that already proved the post gone is believed as it is: the
    API can add a "gone" verdict, never overturn one, and never costs a
    request when the page has already settled it.
    """
    verdict = _page_says_available(url, timeout, referer, on_tick, cookies, settings)
    if verdict is False or not url:
        return verdict
    second = sankaku_post_exists(
        url, timeout, cookies or (cookies_for_url(url, settings) if settings else None))
    return verdict if second is None else second


def _page_says_available(
    url: Optional[str], timeout: float, referer: Optional[str] = None,
    on_tick: Optional[OnTick] = None, cookies: Optional[dict] = None,
    settings=None,
) -> Optional[bool]:
    """Checks whether a match's source URL still exists.

    Returns True if it responded OK, False for a definite "gone" result,
    and None when the answer is genuinely unknown - a network error, a
    timeout, a 403 (very often just hotlink protection or a bot check
    rather than a deleted post), or a server error. This distinction
    matters: treating "couldn't reach it" as "it's gone" would throw
    away perfectly good matches whenever the network hiccups, so
    anything ambiguous stays None and is never reported as unavailable.

    "Gone" covers both a proper 404/410 AND a soft-404: some sites
    (Pixiv confirmed) serve a "this post was deleted" error page with
    HTTP 200, which a status-code-only check would wrongly call
    available. For those sites specifically, a bounded chunk of the page
    body is scanned for their known error text.

    Uses GET with stream=True rather than HEAD - some booru CDNs don't
    answer HEAD at all (or answer it misleadingly), and streaming lets
    us read only as much of the body as the soft-404 check needs."""
    if not url:
        return None
    # Use the site's own headers where it has them. Sankaku in particular
    # hides its "No Content" answer from this project's User-Agent and
    # bounces to the browse index instead, so checking with the default UA
    # cannot tell a dead post from a live one.
    headers = dict(headers_for_url(url, settings) or {"User-Agent": USER_AGENT})
    if referer:
        headers["Referer"] = referer

    soft_404_markers = _soft_404_markers_for(url)

    # The response, once there is one, so a timed-out check can close it
    # from this thread - which aborts the read the worker is blocked in,
    # rather than leaving it to trickle on after its result is discarded.
    in_flight: dict = {}

    def _do_request():
        resp = net.get(
            url, headers=headers, timeout=timeout, allow_redirects=True, stream=True,
            cookies=cookies,
        )
        in_flight["resp"] = resp
        try:
            body = ""
            if resp.status_code == 200 and soft_404_markers:
                # Only pull the bounded prefix needed for the marker
                # check - never the whole page, and never at all for
                # sites with no soft-404 quirk to look for.
                raw = resp.raw.read(SOFT_404_SCAN_BYTES, decode_content=True) or b""
                body = raw.decode("utf-8", errors="ignore")
            return resp.status_code, body, resp.url, resp.headers.get("Content-Type", "")
        finally:
            resp.close()

    try:
        with ProgressTicker("Checking availability", timeout, on_tick):
            status, body, final_url, content_type = run_with_hard_timeout(_do_request, timeout)
    except (requests.RequestException, HardTimeoutError) as exc:
        if "resp" in in_flight:
            in_flight["resp"].close()
        log.debug("Availability check for %s was inconclusive: %s", url, exc)
        return None

    if settings is not None and "resp" in in_flight:
        # Closed already, but a closed Response keeps the cookies it
        # already received - see capture_rotated_deviantart_cookies for
        # why this only ever does anything for deviantart.com/fav.me.
        capture_rotated_deviantart_cookies(url, in_flight["resp"], settings)

    if status in DEFINITELY_GONE_STATUSES:
        log.info("Source URL is gone (HTTP %d): %s", status, url)
        return False

    # Checked before the status range: a redirect to a site's generic
    # "nothing here" page ends in a perfectly healthy HTTP 200, so this
    # has to be judged on where the request LANDED, not on how it
    # finished.
    bounced_to_index = _redirect_to_index(url, final_url)
    if bounced_to_index:
        # NOT proof of deletion. Sankaku bounces every post URL - id 100
        # and id 38,000,000 alike - to a login page or its index now that
        # the legacy site is behind SSO, so reading this as "gone" deleted
        # every Sankaku match regardless of whether it still existed.
        # Unverifiable is the honest answer, and the caller keeps the
        # match rather than silently discarding it.
        log.info(
            "Source URL was bounced to the site's browse index (%s) rather than a post - "
            "cannot verify it either way, so leaving the match alone: %s", bounced_to_index, url,
        )
        return None

    landed_on = _redirect_means_gone(url, final_url)
    if landed_on:
        log.info(
            "Source URL redirected to %s, the site's \"no such post\" page - treating as gone: %s",
            landed_on, url,
        )
        return False

    if 200 <= status < 400:
        if soft_404_markers and body:
            lowered = body.lower()
            for marker in soft_404_markers:
                if marker.lower() in lowered:
                    log.info(
                        "Source URL returned HTTP %d but its page says the content was "
                        "deleted (matched %r): %s", status, marker, url,
                    )
                    return False
        # Logged even on success: without this, a check that passed and a
        # check that never really ran look identical in the log, and
        # "why wasn't this dropped?" becomes unanswerable. Says whether
        # the body was actually scanned, since for sites whose only
        # "gone" signal is in the page text that's the part that matters.
        if soft_404_markers:
            # Say WHAT came back, not just that nothing matched. A marker
            # miss has two very different causes - the page really is a
            # live post, or we were served something else entirely (a bot
            # check, an age gate, a login wall) - and they're
            # indistinguishable without seeing the page. The title is the
            # cheapest thing that identifies it.
            log.debug(
                "Availability check for %s: HTTP %d, scanned %d bytes against %d marker(s), "
                "none matched - treating as available. title=%r content_type=%r%s%s",
                url, status, len(body), len(soft_404_markers), _page_title(body), content_type,
                f" landed_on={final_url}" if final_url and final_url != url else "",
                "" if cookies else " (no cookies sent for this host)",
            )
        else:
            log.debug(
                "Availability check for %s: HTTP %d, no body markers defined for this host "
                "- treating as available", url, status,
            )
        return True

    log.debug("Availability check for %s inconclusive (HTTP %d) - not treating as gone", url, status)
    return None
