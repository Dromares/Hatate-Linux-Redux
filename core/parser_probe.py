"""Checks each parser against a real post on the live site.

The test suite is entirely fixture-based, which is fast and deterministic
and could not have caught the bugs that actually happened. Three of them
came from fixtures that did not match reality: an Anime-Pictures fixture
written from guesswork, an e621 one that never contained a null "sample",
and a Gelbooru one that used the tag container id its relatives do not.
A fixture can only test what its author already believed.

So this exists as the other half: fetch one known post per site and check
that the parser still gets tags, dimensions and a picture out of it. It
is the audit that found Safebooru, rule34 and Xbooru returning zero tags,
turned into something the app can run on demand.

Deliberately NOT part of the normal test run. It is slow, it needs the
network, and it will report a site being down as a failure - all fine for
something a person chooses to run, all useless in a suite meant to gate a
change.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .applog import get_logger

log = get_logger("parser_probe")

# One known post per site. Chosen to be long-standing and unremarkable -
# a post that is likely to still be there, and whose tags are ordinary.
# If one is deleted the probe reports that site as failing, which is the
# correct outcome: the reference needs replacing.
PROBES = [
    ("danbooru",       "https://danbooru.donmai.us/posts/1000000"),
    ("gelbooru",       "https://gelbooru.com/index.php?page=post&s=view&id=5053544"),
    ("safebooru",      "https://safebooru.org/index.php?page=post&s=view&id=7088660"),
    ("rule34",         "https://rule34.xxx/index.php?page=post&s=view&id=5000000"),
    ("xbooru",         "https://xbooru.com/index.php?page=post&s=view&id=1272540"),
    ("moebooru",       "https://yande.re/post/show/933082"),
    ("e621",           "https://e621.net/post/show/219935"),
    ("zerochan",       "https://www.zerochan.net/3793685"),
    ("animepictures",  "https://anime-pictures.net/pictures/view_post/596771"),
    ("eshuushuu",      "https://e-shuushuu.net/images/1118149"),
    ("deviantart",     "https://www.deviantart.com/billjersey/art/Mad-Moxxi-160109252"),
    ("pawchive",       "https://pawchive.pw/patreon/user/50093102/post/83421639"),
    ("twitter",        "https://twitter.com/i/web/status/960455946269945856"),
]

# Sites that legitimately produce no tags, so "no tags" must not be
# reported as a fault for them.
NO_TAGS_EXPECTED = {
    "deviantart",   # the artist only - oEmbed publishes no tag vocabulary
    "twitter",      # the author only - a tweet has no tags
    "pawchive",     # the creator, plus the post's own tags when it has any
}

# Sites that genuinely do not publish the original's dimensions, so their
# absence is not a fault. Everywhere else they matter: the app compares
# them against the local file to decide whether a match is an upgrade, and
# a site quietly ceasing to report them would silently disable that.
NO_DIMENSIONS_EXPECTED = {
    "pawchive",     # its API states neither width nor height
}

# Sankaku is not probed: it identifies posts by the local file's hash, so
# there is no URL that stands alone as a check.


@dataclass
class ProbeResult:
    site: str
    url: str
    ok: bool
    tags: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    has_preview: bool = False
    has_file_url: bool = False
    problem: Optional[str] = None
    seconds: float = 0.0

    restricted: Optional[str] = None

    def summary(self) -> str:
        if not self.ok:
            return self.problem or "failed"
        if self.restricted:
            return f"{self.tags} tags, held back by the site (not a parser fault)"
        parts = [f"{self.tags} tags"]
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        parts.append("preview" if self.has_preview else "NO preview")
        if self.has_file_url:
            parts.append("file")
        return ", ".join(parts)


def run(timeout: float = 30.0,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        only: Optional[List[str]] = None) -> List[ProbeResult]:
    """Probe every site (or just `only`), returning one result each.

    `on_progress(done, total, site)` fires before each fetch so a caller
    can show which site it is waiting on - these are real requests to
    thirteen different sites and the whole run takes a while.
    """
    from .boorus import fetch_page_info

    targets = [(s, u) for s, u in PROBES if not only or s in only]
    results: List[ProbeResult] = []
    for index, (site, url) in enumerate(targets):
        if on_progress:
            on_progress(index, len(targets), site)
        started = time.monotonic()
        try:
            info = fetch_page_info(url, timeout=timeout)
        except Exception as exc:
            results.append(ProbeResult(
                site=site, url=url, ok=False, problem=str(exc)[:160],
                seconds=time.monotonic() - started))
            continue

        elapsed = time.monotonic() - started
        tags = len(info.tags)
        problem = None
        if not info.fetched:
            problem = "no parser matched this URL"
        elif tags == 0 and site not in NO_TAGS_EXPECTED:
            problem = "fetched, but produced no tags"
        elif info.restricted:
            # The site is holding this post back rather than the parser
            # failing to read it. Calling that a fault would train the
            # reader to ignore this report.
            problem = None
        elif not info.preview_url and not info.file_url:
            problem = "no picture of any kind"
        elif (not info.width or not info.height) and site not in NO_DIMENSIONS_EXPECTED:
            problem = "no dimensions - the upgrade comparison needs these"

        results.append(ProbeResult(
            site=site, url=url, ok=problem is None, tags=tags,
            width=info.width, height=info.height,
            has_preview=bool(info.preview_url), has_file_url=bool(info.file_url),
            restricted=info.restricted, problem=problem, seconds=elapsed,
        ))
        log.info("Parser probe %s: %s", site, results[-1].summary())

    if on_progress:
        on_progress(len(targets), len(targets), "")
    return results
