"""What the preview panel says.

Every function here turns an entry or a candidate into a piece of text (or
a colour) and touches no widget. They were module-level helpers in
main_window.py already, plus two decisions still buried inside
_update_preview - and none of them had a test, because reaching them meant
importing a 3,000-line GUI module.

The placeholder wording in particular is worth pinning: "No match found"
and "Not searched yet" mean opposite things to someone deciding whether to
re-run a search, and they are one `elif` apart.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from core.image_compare import compare_file_sizes, compare_sizes
from core.models import MatchStatus
from core.similarity_display import similarity_label
from gui import theme

# Wording for a row with no candidates at all, by why it has none.
NO_CANDIDATE_TEXT = {
    MatchStatus.SEARCHING: "Searching…",
    MatchStatus.NOT_FOUND: "No match found",
}
NOT_SEARCHED_TEXT = "Not searched yet"

# The banner used to be green/amber/grey. These are the same three
# ink-ramp tiers Status's own chips draw at (gui/theme.py
# STATUS_WEIGHTS): ink_65 for a confirmed upgrade (the "good"/"sent"
# tier), ink_100 for "worth a second look" (the match isn't clearly
# bigger, or the proportions differ - the "poor"/"not_found" tier), and
# ink_45 for "nothing to compare" (no candidate at all - the inert
# "not_searched"/"estimated" tier).
BANNER_GOOD = "ink_65"
BANNER_UNCERTAIN = "ink_100"
BANNER_NEUTRAL = "ink_45"


def human_size(num_bytes: Optional[int]) -> Optional[str]:
    if num_bytes is None:
        return None
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def matched_caption(entry) -> str:
    """"Matched image: Danbooru (95% similar)".

    The site and the score are both omitted when absent rather than shown
    empty, so a match with neither still reads as a sentence.

    The panel has room for words, so an unmeasured score says so in words
    - "(~76% similar - ranking, not measured)" - rather than leaving the
    table's "~" to be decoded.
    """
    caption = "Matched image:"
    if entry.booru_name:
        caption += f" {entry.booru_name}"
    if entry.similarity is not None:
        score = similarity_label(entry.similarity, entry.similarity_measured)
        qualifier = "" if entry.similarity_measured else " - ranking, not measured"
        caption += f" ({score} similar{qualifier})"
    return caption


def no_candidate_text(entry) -> str:
    """Why this row shows no match.

    "No match found" and "Not searched yet" are one elif apart and mean
    opposite things to someone deciding whether to search again, so the
    mapping is data rather than a chain.
    """
    if entry.status == MatchStatus.ERROR:
        return f"Search error: {entry.error_message or ''}"
    if entry.status == MatchStatus.NOT_FOUND and entry.error_message:
        # Some engines answered and found nothing; others couldn't answer.
        return f"No match found (not every engine could search: {entry.error_message})"
    return NO_CANDIDATE_TEXT.get(entry.status, NOT_SEARCHED_TEXT)


def local_image_info_text(path: str, image_opener) -> str:
    """Dimensions, file size, and format for the local file - all read
    straight from disk, no network needed.

    `image_opener` is PIL's Image.open, passed in so this stays a function
    about text rather than about imaging.
    """
    parts = []
    try:
        with image_opener(path) as im:
            parts.append(f"{im.width}×{im.height}")
            if im.format:
                parts.append(im.format)
    except (OSError, ValueError):
        pass
    try:
        size_str = human_size(os.path.getsize(path))
        if size_str:
            parts.append(size_str)
    except OSError:
        pass
    return " • ".join(parts) if parts else "Could not read image info"


def matched_image_info_text(candidate) -> str:
    """Dimensions (from IQDB when available), file size and format (from a
    lightweight HEAD request to the source URL) for the currently selected
    match. Any piece that isn't available yet or couldn't be determined is
    simply omitted rather than shown as an error - format/size in
    particular depend on the source site supporting HEAD requests."""
    if candidate is None:
        return ""
    parts = []
    if candidate.width and candidate.height:
        parts.append(f"{candidate.width}×{candidate.height}")
    if candidate.remote_format:
        parts.append(candidate.remote_format)
    size_str = human_size(candidate.remote_size_bytes)
    if size_str:
        parts.append(size_str)
    info = " • ".join(parts) if parts else "Image info not available"
    # Borrowed tags say so. They belong to another match of the same
    # picture, and letting them pass as this one's would misreport where
    # the tags in the list came from.
    lender = getattr(candidate, "tags_borrowed_from", None)
    if lender:
        info += f"\ntags from another match of the same image: {lender}"
    # Why this match has no tags, when the parser knows. Without it a
    # hash-keyed miss shows as a match with an empty tag list and no
    # explanation - indistinguishable from a parser that has silently
    # stopped working, which is how it read from the outside.
    reason = getattr(candidate, "incomplete_reason", None)
    if reason:
        return f"{info}\n{reason}"
    return info


def comparison_banner_text(entry, candidate) -> str:
    """The one line that answers the question the preview panel is really
    for: is this match better than what I already have?

    The raw numbers were already on screen, but as two separate readouts
    - leaving the reader to divide 2000 by 1000 to notice the match is
    twice the size. This states the relationship directly, and says so
    prominently when the match is SMALLER, which is usually a reason not
    to bother with it.
    """
    if candidate is None:
        return ""

    parts = []
    if entry.similarity is not None:
        # The banner is one line of dot-separated fragments, so the long
        # wording the caption above uses would not fit; the "~" plus a
        # one-word qualifier does the same job at banner length.
        score = similarity_label(entry.similarity, entry.similarity_measured)
        parts.append(f"{score} similar"
                     if entry.similarity_measured else f"{score} similar (ranking)")

    comparison = compare_sizes(
        entry.local_width or 0, entry.local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )
    if comparison.verdict:
        parts.append(f"match is {comparison.verdict}")

    try:
        local_bytes = os.path.getsize(entry.path)
    except OSError:
        local_bytes = None
    file_text = compare_file_sizes(local_bytes, candidate.remote_size_bytes)
    if file_text:
        parts.append(file_text)

    return "  ·  ".join(parts)


def comparison_banner_tier(entry, candidate) -> str:
    """ink_65 when the match is worth taking, ink_100 when it isn't
    clearly an upgrade, ink_45 with no candidate to compare against -
    the same reading the Status column's weight channel already uses,
    so the two don't have to be learned separately."""
    if candidate is None:
        return BANNER_NEUTRAL
    comparison = compare_sizes(
        entry.local_width or 0, entry.local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )
    if comparison.aspect_differs:
        return BANNER_UNCERTAIN
    return BANNER_GOOD if comparison.remote_is_bigger else BANNER_UNCERTAIN


def comparison_banner_colour(entry, candidate, mode: str) -> str:
    """The banner tier above, resolved to the real colour for `mode`
    ('dark' or 'light') - a QSS-ready value, for the one caller
    (main_window's `setStyleSheet`) that cannot use a tier name
    directly."""
    return theme.palette(mode)[comparison_banner_tier(entry, candidate)]


@dataclass(frozen=True)
class ComparisonReadout:
    """The compare header's readout, as parts the widget can style apart:
    `● 96% SIMILARITY ▲ 2.5× larger`. Every part is "" when it has
    nothing to say, so the widget hides the part rather than showing a
    dash."""
    glyph: str = ""
    value: str = ""
    label: str = ""
    diff: str = ""
    tier: str = BANNER_NEUTRAL
    tooltip: str = ""


def comparison_readout(entry, candidate) -> ComparisonReadout:
    """The number a reviewer decides on, pulled out of the old one-line
    banner. The measured/ranking distinction (B-5) is kept in two
    channels: the label says RANKING instead of SIMILARITY, and the
    value keeps similarity_label's "~". The banner's file-size and
    "different shape" remarks, which a 28px readout has no room for, move
    to the tooltip so nothing the banner said is lost."""
    if candidate is None:
        return ComparisonReadout()

    glyph = value = label = ""
    if entry.similarity is not None:
        value = similarity_label(entry.similarity, entry.similarity_measured)
        label = "Similarity" if entry.similarity_measured else "Ranking"
        glyph = theme.status_glyph("good" if entry.similarity_measured else "estimated")

    comparison = compare_sizes(
        entry.local_width or 0, entry.local_height or 0,
        candidate.width or 0, candidate.height or 0,
    )
    diff = comparison.verdict
    if diff and comparison.remote_is_bigger:
        diff = f"\u25b2 {diff}"
    return ComparisonReadout(
        glyph=glyph, value=value, label=label, diff=diff,
        tier=comparison_banner_tier(entry, candidate),
        tooltip=comparison_banner_text(entry, candidate),
    )
