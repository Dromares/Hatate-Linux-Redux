"""Picking the right page of a multi-page post.

A Pixiv artwork can hold dozens of images under one URL, and a search
engine reports the artwork, not the page. Pixiv's /ajax/illust/{id}
response describes only the first page, so a match on page 5 was shown -
and downloaded, and compared against - as page 1: the wrong picture,
presented with no indication anything was off.

Nothing in the match metadata reliably says which page it was. But the
question "which of these images is the one I have?" is exactly what a
perceptual hash answers, and the local file is right there. So each
page's small thumbnail is hashed and compared against the local image,
and the closest one wins.

Deliberate limits:

- Only for posts that actually have several pages. A single-page post
  costs nothing extra, which is the overwhelming majority.
- Only up to MAX_PAGES_TO_COMPARE. Some artworks run to hundreds of
  pages, and hashing all of them would cost more than the mistake does.
  A post past that size isn't hopeless, though: where something else has
  already named a page, check_page confirms or refutes that one page for
  a single download, no matter how many the post holds.
- Only when the closest page is clearly closer than the alternatives.
  If several pages look equally like the local image - a set of near
  duplicate variants, say - there is no evidence to choose between them,
  and picking one anyway would be a guess dressed up as a result.

SauceNAO, it turns out, already knows the answer: it names the page in
its own thumbnail URL (.../manga/89861006_p1.jpg). That's free evidence,
but it can't simply be believed - see page_index_from_thumbnail for what
it is and isn't good for, and for the measurements behind that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .applog import get_logger
from .image_compare import dhash, hamming_distance

log = get_logger("multipage")

# Beyond this, the comparison costs more than the error it prevents.
MAX_PAGES_TO_COMPARE = 30

# At or below this, a page isn't merely the closest - it's essentially
# the same image, which is evidence in its own right regardless of how
# close the runner-up is. This matters because pages of one artwork are
# often genuinely similar (same character, different pose), so demanding
# a wide margin would refuse to choose in exactly the common case and
# leave the wrong page selected - no better than not looking at all.
NEAR_EXACT_DISTANCE = 5

# When the best page ISN'T a near-exact match, it must beat the runner-up
# by this much to count as a decision rather than a coin toss.
MIN_DISTANCE_MARGIN = 4

# Past this, the "best" page doesn't actually resemble the local image,
# so the comparison is untrustworthy - the thumbnails may have failed to
# download, or the match itself may simply be wrong.
#
# Measured rather than guessed: a genuine page scores 0-5 against its own
# thumbnail (same image, different resize and re-encode) while unrelated
# artwork lands at 22+. An earlier value of 24 sat above that unrelated
# band and duly accepted images that were not in the post at all.
MAX_PLAUSIBLE_DISTANCE = 12

# A flat or near-flat image hashes to almost no set bits, which lands
# spuriously close to everything. Such an image carries no information to
# decide on, so it's refused rather than matched by accident.
MIN_INFORMATIVE_BITS = 6


@dataclass
class PageMatch:
    index: int
    distance: int
    runner_up_distance: Optional[int] = None
    confident: bool = False
    reason: str = ""
    # Set when the refusal was "these pages are the same picture as far
    # as I can tell", as opposed to "none of them look like this at all".
    # The distinction matters to callers holding a second opinion: a tie
    # is worth breaking with weaker evidence, whereas a post that doesn't
    # contain the local image at all shouldn't be given a page number on
    # the strength of a hint.
    indistinguishable: bool = False


def choose_matching_page(
    local_source, page_thumbnails: Sequence[Tuple[int, bytes]],
) -> Optional[PageMatch]:
    """Which page best matches the local image.

    `page_thumbnails` is (page_index, image_bytes) for each candidate
    page. Returns None when there's nothing to decide between, or when
    the evidence is too weak to prefer one page - in which case the
    caller should keep the post's default first page rather than act on
    a guess.
    """
    if len(page_thumbnails) < 2:
        return None

    local_hash = dhash(local_source)
    if local_hash is None:
        log.debug("Could not hash the local image; leaving the page choice alone")
        return None
    if bin(local_hash).count("1") < MIN_INFORMATIVE_BITS:
        log.debug(
            "The local image is nearly featureless (%d bits of detail) - not enough to tell "
            "the pages apart", bin(local_hash).count("1"),
        )
        return None

    scored: List[Tuple[int, int]] = []
    for index, data in page_thumbnails:
        distance = hamming_distance(local_hash, dhash(data))
        if distance is not None:
            scored.append((distance, index))

    if len(scored) < 2:
        return None

    scored.sort()
    best_distance, best_index = scored[0]
    runner_up_distance = scored[1][0]

    match = PageMatch(
        index=best_index, distance=best_distance, runner_up_distance=runner_up_distance,
    )

    if best_distance > MAX_PLAUSIBLE_DISTANCE:
        match.reason = (
            f"closest page {best_index} still differs by {best_distance}/64 - the thumbnails "
            "may not have downloaded, or this match is poor"
        )
        return match

    near_exact = best_distance <= NEAR_EXACT_DISTANCE
    if near_exact and best_distance == runner_up_distance:
        # Two pages equally indistinguishable from the local image. Either
        # would arguably be right, but "arguably" isn't a basis to pick.
        match.indistinguishable = True
        match.reason = (
            f"pages {best_index} and {scored[1][1]} are both {best_distance}/64 from the "
            "local image - indistinguishable, so not choosing between them"
        )
        return match

    if not near_exact and runner_up_distance - best_distance < MIN_DISTANCE_MARGIN:
        match.indistinguishable = True
        match.reason = (
            f"pages {best_index} and {scored[1][1]} are equally close "
            f"({best_distance} vs {runner_up_distance}/64) - no basis to choose"
        )
        return match

    match.confident = True
    match.reason = f"page {best_index} matches at {best_distance}/64 (next closest {runner_up_distance})"
    return match


# SauceNAO's thumbnail filename for a Pixiv result: the illust ID, then
# the page it matched. Multi-image posts sit under a /manga/ path segment
# and are named 89861006_p1.jpg; single-image ones are 12345678_p0_
# master1200.jpg. Older entries are 12345678_s.jpg with no page at all,
# which this deliberately doesn't match - no marker, no claim.
SAUCENAO_PIXIV_THUMB_RE = re.compile(r"^(\d+)_p(\d+)(?:_master\d+)?\.jpg$", re.IGNORECASE)

# How far the aspect ratio of a page may sit from the local file's before
# it counts as a different picture rather than a re-encode. Generous,
# because the local file is routinely a resize and integer dimensions
# round: a 5% band clears every real case measured where the page was
# independently confirmed (worst 4.4%, most under 1.5%) while rejecting
# the shape mismatches a stale or mistaken index produces (9% and up,
# one of them a landscape page claimed for a portrait file).
ASPECT_RATIO_TOLERANCE = 0.05


def page_index_from_thumbnail(thumb_url: Optional[str], illust_id: Optional[str]) -> Optional[int]:
    """The page SauceNAO says it matched, from its own thumbnail URL.

    SauceNAO indexes Pixiv per image, not per post, so its thumbnail for
    a multi-image result names the exact page:
    .../res/pixiv/8986/manga/89861006_p1.jpg. That is the answer this
    module otherwise spends a page's worth of downloads working out.

    It is evidence, not truth, and the caller must treat it as such.
    Measured over 1473 real Pixiv results from one session:

    - The illust ID in the thumbnail always agreed with the one in the
      result URL, so a disagreement means something unmodelled is going
      on and the index is refused rather than trusted.
    - The index can be STALE. An artwork whose pages were edited after
      SauceNAO crawled it reports a page that has since moved or gone -
      one result claimed page 4 of a post that now has four pages in
      total, where hashing correctly identified page 3. So the caller
      must bounds-check it against the live page list, and must never
      let it override a confident hash.

    Returns None for anything that isn't an unambiguous SauceNAO Pixiv
    thumbnail, including IQDB's thumbnails, which are a different host
    and carry no page information at all.
    """
    if not thumb_url or not illust_id:
        return None
    split = urlsplit(thumb_url)
    host = split.hostname or ""
    if host != "saucenao.com" and not host.endswith(".saucenao.com"):
        return None
    if "/res/pixiv" not in split.path:
        return None
    match = SAUCENAO_PIXIV_THUMB_RE.match(split.path.rsplit("/", 1)[-1])
    if not match:
        return None
    if match.group(1) != str(illust_id):
        log.debug(
            "SauceNAO thumbnail %s names illust %s but the result is %s - ignoring the page it "
            "claims", thumb_url, match.group(1), illust_id,
        )
        return None
    return int(match.group(2))


def dimensions_identify_page(
    local_size: Optional[Tuple[int, int]], page_sizes: Sequence[Optional[Tuple[int, int]]],
) -> Optional[int]:
    """The one page whose dimensions are exactly the local file's.

    None when no page matches, or when several do - in which case the
    dimensions have identified nothing and something else has to decide.

    This is the cheapest possible confirmation: Pixiv's page list already
    carries every page's width and height, so an exact, unique hit
    settles the question without downloading a single thumbnail.
    """
    if not local_size:
        return None
    hits = [i for i, size in enumerate(page_sizes) if size and tuple(size) == tuple(local_size)]
    return hits[0] if len(hits) == 1 else None


def dimensions_contradict_page(
    local_size: Optional[Tuple[int, int]], page_sizes: Sequence[Optional[Tuple[int, int]]],
    index: int,
) -> bool:
    """Whether the page list argues AGAINST the given page.

    Compares aspect ratios rather than exact sizes, because the local
    file is routinely a resized copy and exact sizes would then never
    match. A page whose shape is simply not the local image's shape is
    not the page the local image came from, whatever anything else says.

    Measured against 15 real results: this clears every case where the
    reported page was independently confirmed correct (all within 4.4%,
    most under 1.5%) and rejects seven where it was not - including one
    naming a landscape page for a portrait file.
    """
    if not local_size or index >= len(page_sizes):
        return False
    local_w, local_h = local_size
    if not local_w or not local_h:
        return False
    size = page_sizes[index]
    if not size or not size[0] or not size[1]:
        return False
    local_ratio = local_w / local_h
    return abs(size[0] / size[1] - local_ratio) / local_ratio > ASPECT_RATIO_TOLERANCE


@dataclass
class PageCheck:
    distance: Optional[int] = None
    confirmed: bool = False
    reason: str = ""


def check_page(local_source, thumbnail: bytes) -> PageCheck:
    """Whether one particular page IS the local image.

    "Which of these pages is it?" costs a download per page, and past
    MAX_PAGES_TO_COMPARE that's more than the mistake is worth - so a
    post of a hundred-odd images gets abandoned on page 1, which is
    exactly the failure this module exists to prevent, just at a size
    where it can't afford to look.

    "Is it THIS page?" costs one download whatever the post's size. It
    can only be asked when something else has already named a page -
    SauceNAO's thumbnail, or a size that only one page has - but that
    turns an unanswerable question into a cheap one.

    Confirmation demands a near-exact result, not merely a plausible one.
    There's no runner-up here to be better than, so the reading has to
    stand on its own: measured over 19 real results from a 111-page post,
    the named page scored 0-2 while its neighbours sat at 20-45, and a
    page that isn't the local image reads unmistakably as one.
    """
    local_hash = dhash(local_source)
    if local_hash is None:
        return PageCheck(reason="the local image couldn't be hashed")
    if bin(local_hash).count("1") < MIN_INFORMATIVE_BITS:
        # Same trap as choose_matching_page guards: a near-flat image sits
        # spuriously close to everything, so it would "confirm" any page
        # put in front of it.
        return PageCheck(reason="the local image is nearly featureless")

    distance = hamming_distance(local_hash, dhash(thumbnail))
    if distance is None:
        return PageCheck(reason="that page's thumbnail couldn't be hashed")
    if distance <= NEAR_EXACT_DISTANCE:
        return PageCheck(
            distance=distance, confirmed=True,
            reason=f"it matches the local image at {distance}/64",
        )
    return PageCheck(
        distance=distance,
        reason=f"it differs from the local image by {distance}/64, too much to call it the same "
               "picture",
    )


def page_url_for_index(first_page_url: str, index: int) -> Optional[str]:
    """Rewrites a Pixiv page-0 file URL to another page.

    Pixiv encodes the page in the filename - 12345678_p0.jpg is the
    first, _p4 the fifth - so the whole path is reusable. Returns None
    when the URL doesn't carry that marker, rather than constructing
    something that merely looks plausible.
    """
    if not first_page_url or "_p0" not in first_page_url:
        return None
    if index == 0:
        return first_page_url
    # Only the LAST occurrence: a directory in the path could contain
    # "_p0" too, and rewriting that would break the URL entirely.
    head, separator, tail = first_page_url.rpartition("_p0")
    if not separator:
        return None
    return f"{head}_p{index}{tail}"
