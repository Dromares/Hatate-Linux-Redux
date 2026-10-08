"""Ranking matches so the most USEFUL one is recommended first.

Sorting purely by similarity answers "which is most likely the same
image?" and stops there. But two matches of the same picture are not
equally useful: one may be four times the resolution and carry sixty
tags, the other may be on a site this app can't read at all and
contribute nothing but a link. The dropdown's first entry is the one
that gets selected by default, so it should be the best match that is
actually usable.

Two constraints shape the design.

SIMILARITY MUST STAY DOMINANT. A quality bonus that could overturn a
meaningfully better similarity would start recommending the wrong
picture, which is far worse than recommending a correct one from a
mediocre site. The bonus is therefore capped (MAX_QUALITY_BONUS) well
below the similarity gaps that distinguish a real match from a
near-miss: it reorders candidates that are already about equally likely,
and nothing more.

RANKING MUST BE FREE. Tags, file URLs and true dimensions are only
fetched for the candidate that ends up selected - fetching them for
every candidate would mean an HTTP request per candidate per image,
which is untenable across a large library. So the score uses only what
is already known by the time candidates are merged:

  - similarity, from the search engine
  - whether the site has a parser at all, from the URL alone
  - dimensions, when the engine supplied them (IQDB does; SauceNAO
    doesn't)
  - availability, from the sweep that has already run

Anything requiring a fetch is deliberately absent. A candidate is never
penalised for information that simply hasn't been retrieved yet -
otherwise SauceNAO results, which never carry dimensions, would be
systematically ranked below IQDB ones for no real reason.
"""
from __future__ import annotations

import math
from typing import List, Optional

from .applog import get_logger
from .boorus import find_parser

log = get_logger("ranking")

# Ceiling on everything quality can contribute. Deliberately small: a
# 98% match must always outrank a 94% one, however good the latter's
# source is.
MAX_QUALITY_BONUS = 3.0

# A site with a parser can supply tags, a full-resolution file URL and
# real dimensions; one without contributes a link and nothing else. This
# is the single most useful thing knowable for free, so it carries the
# largest share.
PARSER_BONUS = 1.5

# Resolution advantage over the local file, awarded on a log scale so a
# genuinely enormous match doesn't dominate: 2x earns half, 4x earns all
# of it, beyond that flattens out.
MAX_RESOLUTION_BONUS = 1.5
RESOLUTION_REFERENCE_RATIO = 4.0

# Confirmed-dead matches sink below everything else. They're normally
# dropped before this point; this is for when dropping is turned off.
UNAVAILABLE_PENALTY = 1000.0


def _resolution_bonus(local_w, local_h, remote_w, remote_h) -> float:
    """Reward for a match being larger than the local file.

    Returns 0 when either size is unknown - NOT a penalty. Only some
    engines report dimensions, and marking a candidate down for its
    engine's reporting habits would rank by provenance rather than by
    quality.
    """
    if not all(isinstance(v, int) and v > 0 for v in (local_w, local_h, remote_w, remote_h)):
        return 0.0
    ratio = max(remote_w, remote_h) / max(local_w, local_h)
    if ratio <= 1.0:
        return 0.0
    # log-scaled against the reference ratio, clamped to the maximum.
    scaled = math.log(ratio) / math.log(RESOLUTION_REFERENCE_RATIO)
    return min(MAX_RESOLUTION_BONUS, MAX_RESOLUTION_BONUS * scaled)


def quality_bonus(candidate, local_width: Optional[int], local_height: Optional[int]) -> float:
    """How much better than its similarity alone this candidate looks,
    from information already in hand."""
    bonus = 0.0

    if candidate.url and find_parser(candidate.url) is not None:
        bonus += PARSER_BONUS

    bonus += _resolution_bonus(
        local_width or 0, local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )

    return min(MAX_QUALITY_BONUS, bonus)


def candidate_score(candidate, local_width: Optional[int], local_height: Optional[int]) -> float:
    """The value the dropdown is ordered by, highest first."""
    score = float(candidate.similarity or 0.0)
    if candidate.remote_available is False:
        score -= UNAVAILABLE_PENALTY
    score += quality_bonus(candidate, local_width, local_height)
    return score


def score_breakdown(candidate, local_width: Optional[int], local_height: Optional[int]) -> dict:
    """Why this candidate scored where it did, as the individual terms
    candidate_score() adds together - for callers that need to SAY the
    reason rather than just sort by it (e.g. the MCP get_candidates tool,
    where the whole point is letting something that cannot read this
    module's comments see why one match outranks another)."""
    has_parser = bool(candidate.url and find_parser(candidate.url) is not None)
    parser_bonus = PARSER_BONUS if has_parser else 0.0
    resolution_bonus = _resolution_bonus(
        local_width or 0, local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )
    unavailable = candidate.remote_available is False
    return {
        "similarity": float(candidate.similarity or 0.0),
        "has_parser": has_parser,
        "parser_bonus": parser_bonus,
        "resolution_bonus": resolution_bonus,
        "quality_bonus": quality_bonus(candidate, local_width, local_height),
        "quality_bonus_cap": MAX_QUALITY_BONUS,
        "unavailable_penalty_applied": unavailable,
        "unavailable_penalty": UNAVAILABLE_PENALTY if unavailable else 0.0,
        "score": candidate_score(candidate, local_width, local_height),
    }


def rank_candidates(
    candidates: List, local_width: Optional[int] = None, local_height: Optional[int] = None,
    filename: str = "",
) -> List:
    """Returns the candidates ordered best-first.

    Stable within equal scores, so candidates that genuinely tie keep the
    order the engines returned them in rather than shuffling between runs.
    """
    if len(candidates) < 2:
        return candidates

    ranked = sorted(
        candidates,
        key=lambda c: -candidate_score(c, local_width, local_height),
    )

    if ranked and ranked[0] is not candidates[0]:
        log.info(
            "%s: recommending %s over the most similar match %s - "
            "score %.1f (similarity %.0f%%) vs %.1f (similarity %.0f%%)",
            filename or "match", ranked[0].url, candidates[0].url,
            candidate_score(ranked[0], local_width, local_height), ranked[0].similarity or 0,
            candidate_score(candidates[0], local_width, local_height),
            candidates[0].similarity or 0,
        )
    return ranked
