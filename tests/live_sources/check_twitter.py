"""DAN-56/57 live-source check: for a multi-photo tweet, does this app
still resolve the photo that actually matched, or silently go back to
always showing photo 1 - a successful, non-empty, WRONG result that no
existing test or safety net would catch?

Not part of `run_tests.sh` or CI - see README.md in this directory. Run
through the shared entry point:

    python3 -m tests.live_sources.run_checks twitter

api.fxtwitter.com is a third-party proxy, not Twitter itself, so two
independent things can break this: the proxy's own JSON envelope, or
Twitter's media layout underneath it. The beliefs below are split so a
failure names which one - see each belief's docstring.
"""
from __future__ import annotations

import sys

from .. import _path  # noqa: F401

from core import boorus
from core.boorus import twitter
from .harness import Check, SourceUnavailable, exit_code, memoize_once

# Pinned 2026-10-02, CONFIRMED still live and matching the values below.
#
# This is the exact tweet that caused DAN-56/57: a four-photo tweet whose
# fourth photo was the local file the board reported as the match, shown
# downloaded and compared as photo 1 instead. tests/test_multipage.py's
# TestResolvingAMultiPhotoTweet pins the same id, author, and per-photo
# (media id, width, height) triples as its regression fixture - captured
# from this tweet's real api.fxtwitter.com response, and reused here
# rather than re-derived, so a future failure is debuggable against that
# test's own documented reasoning instead of needing to rediscover why
# this tweet was chosen. All four photos have distinct (width, height)
# pairs, which is what makes "photo 3" a checkable claim rather than one
# any photo could satisfy by accident.
#
# If this tweet is ever deleted, made private, or edited, that is
# SOURCE_UNAVAILABLE (see _fetch below) or a belief going BELIEF_BROKEN
# for a reason that says so explicitly - not a silent pass. Replacing it
# means picking another long-lived public multi-photo tweet, updating
# EXPECTED_AUTHOR/EXPECTED_PHOTOS below, and updating this comment with
# the new reasoning.
TWEET_URL = "https://x.com/DARKMETAKNIGH12/status/2011168979038142698"
EXPECTED_AUTHOR = "DARKMETAKNIGH12"
# (media id substring, width, height), in the tweet's real photo order.
EXPECTED_PHOTOS = [
    ("G-kbgPfa0AAXsZh", 1216, 832),
    ("G-kbgizbQAAakdJ", 1365, 2048),
    ("G-kbhHJaoAAgMek", 2048, 2048),
    ("G-kbhl1bAAAxpzn", 1171, 2048),
]

CHECK = Check("twitter")


@memoize_once
def _fetch():
    print(f"GET (via fetch_page_info, proxied through api.fxtwitter.com) {TWEET_URL}")
    try:
        return boorus.fetch_page_info(TWEET_URL, timeout=20.0)
    except boorus.BooruContentGoneError as exc:
        # CONFIRMED live, 2026-10-02: fxtwitter answers a gone/never-
        # existed tweet with HTTP 404 and a clean {"code":404,"tweet":
        # null} body. fetch_page_info turns 404/410 into this specific
        # exception. The pinned subject disappearing is not our bug.
        raise SourceUnavailable(
            f"{TWEET_URL} is gone (HTTP 404/410): {exc}", evidence=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 - classified as unavailable, not swallowed
        # Anything else fetch_page_info raises here is a transport
        # failure (DNS, connection refused, timeout) or a non-200 from
        # the PROXY itself (rate limit, 5xx) - api.fxtwitter.com being
        # unreachable or unhappy, not Twitter's media layout or the
        # proxy's own JSON shape changing underneath a 200 response.
        raise SourceUnavailable(
            f"api.fxtwitter.com could not be reached for {TWEET_URL}: "
            f"{type(exc).__name__}: {exc}",
            evidence=str(exc),
        ) from exc


@CHECK.belief("the proxy resolves the tweet and names its author")
def _author_resolves() -> str:
    """The proxy's own envelope: valid JSON, a 'tweet' object, an
    'author.screen_name' field. If this breaks while the other two
    beliefs still pass, it is specifically the envelope - the finest
    grain this check can isolate the proxy's own shape to."""
    info = _fetch()
    assert info.tags, (
        "fetch_page_info returned HTTP 200 for the pinned tweet but parsed no tags - "
        "api.fxtwitter.com answered, so the tweet is not gone, but its JSON envelope "
        "(the 'tweet'/'author' shape core/boorus/twitter.py reads) has changed."
    )
    names = {tag.name for tag in info.tags}
    assert EXPECTED_AUTHOR in names, (
        f"expected the author tag {EXPECTED_AUTHOR!r}, got {sorted(names)!r} - either "
        "the pinned tweet's author changed (shouldn't happen for this id) or the "
        "author field moved in the proxy's JSON shape."
    )
    return f"author={EXPECTED_AUTHOR!r}"


@CHECK.belief("the tweet still has its known photos, in known order")
def _media_layout_matches() -> str:
    """Twitter's media layout, as surfaced through the proxy: the number
    of photos, and each one's real dimensions, in order. If the envelope
    belief above still passes but this one breaks, the proxy is fine and
    it is Twitter's own media data underneath it that changed shape."""
    info = _fetch()
    assert info.body, "fetch_page_info did not keep the response body to parse pages from"
    pages = twitter.parse_pages(info.body, TWEET_URL)
    assert len(pages) == len(EXPECTED_PHOTOS), (
        f"expected {len(EXPECTED_PHOTOS)} photos, got {len(pages)} - Twitter's media "
        "layout for this tweet, as surfaced through the fxtwitter proxy, has changed. "
        f"Evidence: {[(p.get('width'), p.get('height')) for p in pages]!r}"
    )
    for index, (media_id, width, height) in enumerate(EXPECTED_PHOTOS):
        photo = pages[index]
        actual = (photo.get("width"), photo.get("height"))
        url = (photo.get("urls") or {}).get("original") or ""
        assert actual == (width, height) and media_id in url, (
            f"photo index {index}: expected {media_id} at {width}x{height}, got "
            f"{url!r} at {actual[0]}x{actual[1]} - the tweet's photo order or sizing "
            "changed under the proxy."
        )
    return f"{len(pages)} photo(s) in the pinned order"


@CHECK.belief("a /photo/N URL resolves to that photo, not always photo 1")
def _index_resolves_to_the_right_photo() -> str:
    """The DAN-56/57 belief itself, pinned: non-emptiness is not the
    assertion - a search engine that names which photo of a multi-photo
    tweet matched (the /photo/N suffix) must get THAT photo back, not
    unconditionally the first. This is the exact shape that escaped:
    successful, non-empty, and quietly wrong.

    Reuses the already-fetched body (see memoize_once on _fetch) instead
    of issuing a second live request for a URL that differs only by its
    /photo/N suffix - core/boorus/twitter.py's parse_dimensions and
    parse_file_url read the same JSON body regardless of which URL
    variant is passed, so this is the real production parsing code,
    without re-spending a request the two beliefs above already made.
    """
    info = _fetch()
    probe_index = 2  # third photo (0-based) - distinct (width, height) from every other photo
    probe_url = f"{TWEET_URL}/photo/{probe_index + 1}"
    width, height = twitter.parse_dimensions(info.body, probe_url)
    file_url = twitter.parse_file_url(info.body, probe_url) or ""
    expected_id, expected_w, expected_h = EXPECTED_PHOTOS[probe_index]
    assert (width, height) == (expected_w, expected_h) and expected_id in file_url, (
        f"{probe_url} resolved to {file_url!r} at {width}x{height}, expected photo "
        f"{probe_index + 1} ({expected_id}) at {expected_w}x{expected_h} - this is "
        "exactly the DAN-56/57 shape: a successful, non-empty, WRONG photo."
    )
    return f"photo {probe_index + 1}/{len(EXPECTED_PHOTOS)} resolved correctly ({expected_id})"


if __name__ == "__main__":
    sys.exit(exit_code(CHECK.run()))
