"""Keeps track of whether each site's parser is actually working.

Safebooru, rule34 and Xbooru extracted zero tags - possibly for as long
as they had been supported - and nothing said so. The parser logged "No
tags extracted" to a file nobody opens, the match still appeared with its
URL, and the only way it ever came to light was a person noticing that
one site's results looked thin.

That is the failure worth designing against: a parser that stops working
is silent, and it stays silent. This module records what every fetch
produced so the app can say "e621 has returned nothing 14 times this
session" instead of leaving it to be spotted by eye.

Deliberately in-memory and per-session. This is a health signal, not an
audit trail - a stale count from last week would be worse than none,
because it would describe markup that has since changed.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .applog import get_logger

log = get_logger("parser_health")

# How many fetches in a row a parser may return nothing for before it is
# called broken. One empty result is ordinary - a post genuinely can have
# no tags, and a hash-keyed lookup like Sankaku's misses far more often
# than it hits. A run of them with no successes at all is not.
CONSECUTIVE_EMPTIES_BEFORE_SUSPECT = 3


@dataclass
class SiteHealth:
    site: str
    ok: int = 0                       # fetches that produced tags
    empty: int = 0                    # fetched fine, parsed to nothing
    failed: int = 0                   # the fetch itself failed
    consecutive_empty: int = 0
    last_ok_at: Optional[float] = None
    last_problem: Optional[str] = None
    last_problem_at: Optional[float] = None
    reported: bool = False            # already surfaced to the user once

    @property
    def attempts(self) -> int:
        return self.ok + self.empty + self.failed

    @property
    def suspect(self) -> bool:
        """Looks broken, as opposed to merely having missed.

        Requires never having succeeded this session: a parser that works
        and then hits a run of tagless posts is not broken, and saying so
        would train the user to ignore the warning.
        """
        return self.ok == 0 and self.consecutive_empty >= CONSECUTIVE_EMPTIES_BEFORE_SUSPECT

    def summary(self) -> str:
        if self.suspect:
            return f"no tags in {self.consecutive_empty} attempts"
        if self.ok:
            return f"{self.ok} ok" + (f", {self.empty} empty" if self.empty else "")
        if self.failed:
            return f"{self.failed} failed"
        return "not used yet"


_lock = threading.Lock()
_sites: Dict[str, SiteHealth] = {}


def _entry(site: str) -> SiteHealth:
    health = _sites.get(site)
    if health is None:
        health = SiteHealth(site=site)
        _sites[site] = health
    return health


def record_success(site: str, tag_count: int) -> None:
    with _lock:
        health = _entry(site)
        health.ok += 1
        health.consecutive_empty = 0
        health.last_ok_at = time.time()


def record_empty(site: str, url: str) -> None:
    """Fetched fine but parsed to nothing - the shape of a broken parser."""
    with _lock:
        health = _entry(site)
        health.empty += 1
        health.consecutive_empty += 1
        health.last_problem = "returned no tags"
        health.last_problem_at = time.time()


def record_failure(site: str, url: str, reason: str) -> None:
    """The fetch itself didn't work. Kept apart from an empty parse: a
    site being down says nothing about whether its parser still fits."""
    with _lock:
        health = _entry(site)
        health.failed += 1
        health.last_problem = reason
        health.last_problem_at = time.time()


def newly_suspect() -> List[SiteHealth]:
    """Sites that have just crossed into looking broken, reported once
    each so a warning doesn't repeat on every image of a batch."""
    with _lock:
        found = []
        for health in _sites.values():
            if health.suspect and not health.reported:
                health.reported = True
                found.append(health)
        return found


def snapshot() -> List[SiteHealth]:
    """Every site touched this session, worst first."""
    with _lock:
        return sorted(
            _sites.values(),
            key=lambda h: (not h.suspect, h.ok > 0, h.site.lower()),
        )


def reset() -> None:
    with _lock:
        _sites.clear()
