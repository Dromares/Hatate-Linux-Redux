"""Which entries the list shows.

Deliberately a plain predicate over ImageEntry, with no Qt in it: what
counts as a match is the part worth testing, and it can be decided
without a window.

A filter narrows what is DISPLAYED and nothing else. It never removes an
entry, never changes one, and every action still applies to whatever is
selected - so hiding a row cannot lose work. The one thing to be careful
of is the opposite: an action reaching a row the user cannot see. That is
handled where the list is displayed, by never mapping a row number to an
entry except through the filtered view.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set

from .models import ImageEntry, MatchStatus
from .sites import classify_site

# The site shown for an entry that has no match yet. Kept distinct from
# the "Other" catch-all, which means "matched something we don't have a
# name for" - a real result, and a different thing to look for.
NO_SITE = "(no match)"

# The label shown for an entry the upscale check has never run on.
# entry.upscale_verdict itself is None for this case, but None also means
# "not filtering" on EntryFilter.upscale_verdicts, so filtering needs its
# own label to tell "value is unset" apart from "no filter chosen".
UPSCALE_NOT_CHECKED = "Not checked"
_UPSCALE_LABELS = {"flagged": "Flagged", "clear": "Clear"}
_UPSCALE_ORDER = ("Flagged", "Clear", UPSCALE_NOT_CHECKED)


@dataclass
class EntryFilter:
    """An empty filter matches everything, which is the default state.

    `None` for statuses or sites means "not filtering on this", which is
    deliberately different from an empty set - that would mean "nothing
    is allowed" and match no rows at all. The distinction matters: the UI
    can offer "none selected" as a genuine choice without it being
    confused with "no filter".
    """
    statuses: Optional[Set[MatchStatus]] = None
    sites: Optional[Set[str]] = None
    upscale_verdicts: Optional[Set[str]] = None
    text: str = ""

    def is_active(self) -> bool:
        """Whether this filter hides anything. Drives the "showing N of M"
        readout, so the user is never left wondering where rows went."""
        return (
            self.statuses is not None or self.sites is not None
            or self.upscale_verdicts is not None or bool(self.text.strip())
        )

    def matches(self, entry: ImageEntry) -> bool:
        if self.statuses is not None and entry.status not in self.statuses:
            return False
        if self.sites is not None and site_of(entry) not in self.sites:
            return False
        if (self.upscale_verdicts is not None
                and upscale_verdict_label(entry) not in self.upscale_verdicts):
            return False
        needle = self.text.strip().lower()
        if needle and needle not in entry.filename.lower():
            return False
        return True

    def apply(self, entries) -> list:
        if not self.is_active():
            # The common case, and worth not walking the list for: at
            # tens of thousands of entries this runs on every refresh.
            return list(entries)
        return [e for e in entries if self.matches(e)]


def site_of(entry: ImageEntry) -> str:
    """Which site an entry's current match came from.

    Uses the selected candidate rather than the whole set: the list shows
    one match per row, and filtering on a site the user cannot see in the
    Booru column would be baffling.
    """
    candidate = entry.selected_candidate
    if candidate is None or not candidate.url:
        return NO_SITE
    return classify_site(candidate.source_name, candidate.url)


def sites_present(entries) -> list:
    """The sites actually represented in this list, in display order, so
    the site menu can offer only what is there rather than every site the
    app knows about. NO_SITE sorts last - it is the absence of a result,
    not a site."""
    seen = {site_of(e) for e in entries}
    named = sorted(s for s in seen if s != NO_SITE)
    return named + ([NO_SITE] if NO_SITE in seen else [])


def statuses_present(entries) -> list:
    """The statuses actually represented, in the enum's own order so the
    menu reads the same way every time regardless of what is loaded."""
    seen = {e.status for e in entries}
    return [s for s in MatchStatus if s in seen]


def upscale_verdict_label(entry: ImageEntry) -> str:
    """The label an entry's upscale-check verdict shows in the filter and
    table - "Not checked" for the never-run case, which is what
    entry.upscale_verdict of None actually means."""
    if entry.upscale_verdict is None:
        return UPSCALE_NOT_CHECKED
    return _UPSCALE_LABELS.get(entry.upscale_verdict, UPSCALE_NOT_CHECKED)


def upscale_verdicts_present(entries) -> list:
    """The verdict labels actually represented, in a fixed order (worst
    news first) so the menu reads the same way every time."""
    seen = {upscale_verdict_label(e) for e in entries}
    return [label for label in _UPSCALE_ORDER if label in seen]
