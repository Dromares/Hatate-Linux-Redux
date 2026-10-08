"""DAN-83 live-source check: does anime-pictures.net still answer this
runner with real post data, or with the 403 that escaped to the board?

Not part of `run_tests.sh` or CI - see README.md in this directory for
why. Run through the shared entry point:

    python3 -m tests.live_sources.run_checks anime_pictures

This calls core.boorus.fetch_page_info exactly as a real search result
would: no mocked HTTP, no stubbed parser. A SOURCE_UNAVAILABLE outcome
means the site (or something between this runner and it) is down or
blocking this runner - DAN-83 itself was a 403, exactly this shape.
BELIEF_BROKEN means it answered cleanly and the shape changed under the
parser. See core/boorus/animepictures.py and core/site_access.py.
"""
from __future__ import annotations

import sys

from .. import _path  # noqa: F401

from core import boorus
from .harness import Check, SourceUnavailable, exit_code, memoize_once

# A long-lived, non-account-gated post (see tests/test_search_quality.py,
# which fixtures this same id from a captured API response) with known
# dimensions to check the parse actually extracted something real rather
# than an empty-but-200 body.
POST_URL = "https://anime-pictures.net/posts/596771"
EXPECTED_WIDTH = 5728
EXPECTED_HEIGHT = 4000

CHECK = Check("anime_pictures")


@memoize_once
def _fetch():
    print(f"GET (via fetch_page_info) {POST_URL}")
    try:
        return boorus.fetch_page_info(POST_URL, timeout=20.0)
    except Exception as exc:  # noqa: BLE001 - classified as unavailable, not swallowed
        # fetch_page_info raises BooruError for both a transport failure
        # (DNS, connection refused, timeout) and a non-200 response, and
        # BooruContentGoneError for 404/410 - none of those say our
        # parser is wrong, they say the subject could not be reached or
        # has nothing to reach.
        raise SourceUnavailable(
            f"{POST_URL} could not be fetched: {type(exc).__name__}: {exc}",
            evidence=str(exc),
        ) from exc


@CHECK.belief("the post answers with tags")
def _tags_present() -> str:
    info = _fetch()
    assert info.tags, (
        "fetch_page_info returned HTTP 200 but zero tags - the site answered, so this "
        "is not a SOURCE_UNAVAILABLE case, but its markup/API shape no longer parses "
        "under core/boorus/animepictures.py."
    )
    return f"{len(info.tags)} tag(s)"


@CHECK.belief("dimensions match the known original")
def _dimensions_match() -> str:
    info = _fetch()
    actual = (info.width, info.height)
    expected = (EXPECTED_WIDTH, EXPECTED_HEIGHT)
    assert actual == expected, (
        f"expected {expected[0]}x{expected[1]}, got {actual[0]}x{actual[1]} for {POST_URL} - "
        "the API response shape for width/height probably changed."
    )
    return f"{actual[0]}x{actual[1]}"


if __name__ == "__main__":
    sys.exit(exit_code(CHECK.run()))
