"""DAN-59/61/67/68 live-source check: for rule34.us, does the URL this
app rebuilds from a Google Lens result title still land on a real post,
and is the "Original" file link that page hands back still fetchable?

Not part of `run_tests.sh` or CI - see README.md in this directory. Run
through the shared entry point:

    python3 -m tests.live_sources.run_checks rule34us

All four escapes that reached the board for this site were URL and
download-SHAPE bugs (encoded slashes, a mis-rebuilt link) - never an
empty tag parse. A check that only asked "did we get tags back" would
have missed every one of them, so tag presence is only the third belief
here, not the headline one. The headline beliefs are the two the ticket
actually keeps breaking on: does the Lens-title-rebuilt URL resolve, and
is the "Original" link this app extracts from that page's own markup
actually fetchable - not merely present as a string.
"""
from __future__ import annotations

import sys

import requests

from .. import _path  # noqa: F401

from core import boorus, net
from core.google_lens import parse_exact_matches
from .harness import Check, SourceUnavailable, exit_code, memoize_once

# Pinned 2026-10-02, CONFIRMED still live and matching the values below.
#
# Post 6608448, reached through Google Lens's "Exact matches" tab - the
# only route to rule34.us at all, since neither IQDB nor SauceNAO indexes
# it (see core/boorus/rule34us.py's module docstring). LENS_TITLE is the
# real title shape Lens renders for this post (same text already pinned
# as a fixture in tests/test_search_quality.py's lazy-load-placeholder
# test), reused here rather than re-derived so a mismatch is debuggable
# against that test's own documented reasoning. FILE_URL and the
# dimensions are pinned the same way in
# tests/test_parsers_and_tags.py's TestRule34UsParser, captured from this
# post's real markup.
LENS_TITLE = "If it exists, there is porn of it / gattles / 6608448 - Rule34.us"
POST_URL = "https://rule34.us/index.php?r=posts/view&id=6608448"
FILE_URL = "https://img2.rule34.us/images/cb/6f/cb6ffdde43334831d14e3dc0bf51e132.png"
EXPECTED_WIDTH = 2039
EXPECTED_HEIGHT = 2894

CHECK = Check("rule34us")


@CHECK.belief("the Lens-title URL rebuild still resolves to a real page")
def _lens_rebuild_resolves() -> str:
    """core/google_lens.py rebuilds this URL purely from a title string -
    no fetch involved - so building it is pure string logic and cannot
    itself go SOURCE_UNAVAILABLE. Confirming it actually RESOLVES is a
    live HEAD against the built URL, independent of fetch_page_info's own
    GET below, because a %2F-shaped rebuild bug (DAN-67/68's actual
    class) produces a URL that looks fine as a string but 404s or never
    matches on the receiving end."""
    matches = parse_exact_matches(LENS_TITLE)
    urls = [m.url for m in matches]
    assert urls == [POST_URL], (
        f"Lens title {LENS_TITLE!r} rebuilt {urls!r}, expected [{POST_URL!r}] - "
        "core/google_lens.py's rule34.us EXACT_MATCH_SITES entry no longer matches "
        "this title shape or built the wrong URL from it."
    )
    try:
        resp = net.head(POST_URL, timeout=20.0, deadline=20.0, allow_redirects=True)
    except requests.RequestException as exc:
        raise SourceUnavailable(
            f"{POST_URL} could not be reached: {type(exc).__name__}: {exc}",
            evidence=str(exc),
        ) from exc
    assert resp.status_code == 200, (
        f"the rebuilt URL {POST_URL} answered HTTP {resp.status_code}, not 200 - "
        "the id is right but the URL template (index.php?r=posts/view&id=) no "
        "longer resolves on the real site."
    )
    return f"{POST_URL} -> HTTP {resp.status_code}"


@memoize_once
def _fetch():
    print(f"GET (via fetch_page_info) {POST_URL}")
    try:
        return boorus.fetch_page_info(POST_URL, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 - classified as unavailable, not swallowed
        # fetch_page_info raises BooruError for a transport failure or a
        # non-200 response, and BooruContentGoneError for 404/410 - none
        # of those say our parser is wrong, they say the subject could
        # not be reached or has nothing left to reach.
        raise SourceUnavailable(
            f"{POST_URL} could not be fetched: {type(exc).__name__}: {exc}",
            evidence=str(exc),
        ) from exc


@CHECK.belief("the post answers with tags")
def _tags_present() -> str:
    info = _fetch()
    assert info.tags, (
        "fetch_page_info returned HTTP 200 but zero tags - the site answered, so "
        "this is not a SOURCE_UNAVAILABLE case, but its markup no longer parses "
        "under core/boorus/rule34us.py (the tag <li> classes or their nested "
        "tag-listing links may have changed)."
    )
    return f"{len(info.tags)} tag(s)"


@CHECK.belief("dimensions match the known original")
def _dimensions_match() -> str:
    info = _fetch()
    actual = (info.width, info.height)
    expected = (EXPECTED_WIDTH, EXPECTED_HEIGHT)
    assert actual == expected, (
        f"expected {expected[0]}x{expected[1]}, got {actual[0]}x{actual[1]} for "
        f"{POST_URL} - the page's 'Size: ...w x ...h' line probably changed shape."
    )
    return f"{actual[0]}x{actual[1]}"


@CHECK.belief("the extracted \"Original\" file link is actually fetchable")
def _original_link_is_fetchable() -> str:
    """This is the belief that actually keeps breaking for this site
    (DAN-59/61/67/68): not whether a file_url string comes out of the
    parser, but whether it is a real, fetchable link on the real file
    host - rule34.us serves its originals from a separate img*.rule34.us
    host, and an encoding or host-rebuild quirk there is exactly the
    download-shape bug class that escaped four times without any test
    noticing, because every prior test only asserted the string's
    VALUE, never that pointing a client at it actually works."""
    info = _fetch()
    assert info.file_url, (
        "fetch_page_info returned HTTP 200 but no file_url - rule34us.parse_file_url "
        "found no 'Original' link and no fallback <img>/<video> src on the page."
    )
    assert info.file_url == FILE_URL, (
        f"expected the pinned Original link {FILE_URL!r}, got {info.file_url!r} - "
        "the post's file changed, or parse_file_url picked a different element."
    )
    try:
        resp = net.head(info.file_url, timeout=20.0, deadline=20.0, allow_redirects=True)
    except requests.RequestException as exc:
        raise SourceUnavailable(
            f"the extracted Original link {info.file_url} could not be reached: "
            f"{type(exc).__name__}: {exc}",
            evidence=str(exc),
        ) from exc
    assert resp.status_code == 200, (
        f"the extracted Original link {info.file_url} answered HTTP "
        f"{resp.status_code}, not 200 - this is the literal download-shape failure "
        "DAN-59/61/67/68 reported: a link that parses out of the page cleanly but "
        "does not actually fetch."
    )
    content_type = resp.headers.get("Content-Type", "")
    assert content_type.startswith("image/") or content_type.startswith("video/"), (
        f"the extracted Original link {info.file_url} answered HTTP 200 but with "
        f"Content-Type {content_type!r}, not an image/video - likely an error page "
        "or redirect-to-HTML served with a 200 status."
    )
    return f"{info.file_url} -> HTTP {resp.status_code} ({content_type})"


if __name__ == "__main__":
    sys.exit(exit_code(CHECK.run()))
