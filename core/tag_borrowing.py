"""Taking tags from another match of the same picture.

Some sites cannot supply tags at all, and no amount of parsing changes
that. MangaDex does not have booru-style tags in the first place, and
Sankaku can only be reached by file hash - so a local file that has been
re-encoded or resaved can never be traced back to its post.

The numbers, and which measurement they come from. This docstring
originally cited "435 and 115 out of 587", from an unrecorded measurement
made when the feature was written; those figures do NOT reproduce and
there is no saved run behind them, so they are gone. What is quoted below
is DAN-55, whose inputs, script and full tables are in
`profiling/DAN-55/` (report.md, summary.json) - 1300 entries sampled from
a 16869-entry session, bucketed on `len(chosen.booru_tags)`:

  * 408 entries had a chosen match that supplied NO tags at all.
  * 226 of those 408 (55.4%) had another match in the same result that
    was similar enough to be an eligible lender, sitting unfetched
    because only the chosen candidate's page is ever read.
  * Which sites land in that empty band is a longer tail than "MangaDex
    and Sankaku": MangaDex is 16 of them, Sankaku does not appear in the
    sample at all, and the band is led by Other (157), Twitter (47) and
    Reddit (25). So the two named above are a real cause of an empty tag
    list, but nowhere near 94% of it.
  * Fetching a sample of 25 eligible lenders live, 19 turned out to have
    tags - mean 9.7, median 3, up to 48. So the tags usually do exist;
    they are just on a different site's copy of the same picture, which
    is what this module goes and gets.

Re-measure before quoting any of this again: it is one session's library,
and the shape of somebody else's would differ.

The rule that keeps this honest is similarity. A candidate is only
allowed to lend its tags when it is about as good a match as the one that
was chosen, because at that point it IS the same picture on another site.
A weaker match might be a different picture entirely, and tagging an
image with some other image's tags is far worse than leaving it untagged.
"""
from __future__ import annotations

from typing import List, Optional

from .applog import get_logger

log = get_logger("tag_borrowing")


def candidates_that_may_lend(
    candidates: List, chosen_index: int, slack: float,
) -> List:
    """The other matches close enough to lend their tags, best first.

    `slack` is how far below the chosen match's similarity a candidate may
    be. Zero means only an equal or better match qualifies - which on the
    measured library is 14% of the cases, because ranking puts the
    strongest match first and the rest are typically a point or two
    behind. A few points of slack covers most of them while staying well
    inside "the same picture".

    A candidate with no similarity of its own is skipped rather than
    guessed at: some engines report none, and treating an unknown as good
    enough is how an unrelated picture's tags get attached.
    """
    if not candidates or not (0 <= chosen_index < len(candidates)):
        return []
    chosen = candidates[chosen_index]
    if chosen.similarity is None:
        # Nothing to measure against, so nothing may be compared to it.
        return []
    floor = chosen.similarity - max(0.0, slack)

    eligible = [
        c for i, c in enumerate(candidates)
        if i != chosen_index
        and c.similarity is not None
        and c.similarity >= floor
        # A match already known to be gone has nothing to lend, and
        # fetching it would just confirm the 404 again.
        and c.remote_available is not False
    ]
    # Best first, so the strongest match is asked before a weaker one.
    return sorted(eligible, key=lambda c: -(c.similarity or 0.0))


def borrow_tags(
    entry, candidates: List, chosen_index: int, settings, fetch_details,
) -> Optional[str]:
    """Fills a tagless chosen match from the best eligible alternative.

    `fetch_details` is the detail-fetching callable, passed in so this is
    testable without a network. Returns the URL the tags came from, or
    None if nothing was borrowed.

    The chosen match STAYS chosen. It was picked as the best picture and
    that has not changed - only its tags come from elsewhere, and the
    candidate they came from is recorded so the tag list can say so
    rather than implying the chosen match supplied them.

    At most one alternative is fetched. The whole point is to rescue a
    tagless match cheaply; walking every candidate would turn one search
    into several page fetches for diminishing returns.
    """
    if not getattr(settings, "borrow_tags_from_other_matches", False):
        return None
    if not candidates or not (0 <= chosen_index < len(candidates)):
        return None
    chosen = candidates[chosen_index]
    if chosen.booru_tags:
        return None  # it has its own tags; nothing to do

    slack = getattr(settings, "borrow_tags_similarity_slack", 0.0)
    for lender in candidates_that_may_lend(candidates, chosen_index, slack):
        if not lender.booru_tags_fetched:
            fetch_details(lender)
        if not lender.booru_tags:
            continue
        chosen.booru_tags = list(lender.booru_tags)
        chosen.tags_borrowed_from = lender.url
        log.info(
            "%s: the chosen match has no tags - borrowed %d from %s (%.0f%% vs %.0f%%)",
            getattr(entry, "filename", "?"), len(lender.booru_tags), lender.url,
            lender.similarity or 0.0, chosen.similarity or 0.0,
        )
        return lender.url
    return None
