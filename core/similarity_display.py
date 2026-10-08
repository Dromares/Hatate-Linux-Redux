"""How a similarity number says whether it is actually a measurement.

IQDB and SauceNAO compare the two pictures and report a real score.
ascii2d and the Google engines report no score at all - what the app
shows for one of their results is that result's POSITION in the list,
turned into a number (80, 78, 76, ...). Until now both arrived in the
Similarity column in the same "80%" shape, with nothing to tell them
apart, so an ordinal 80 read as a strong match when it means nothing of
the kind.

core/similarity_check.py measures those ordinal results for real where it
can, and sets MatchCandidate.similarity_measured when it succeeds. This
module is the one place that decides how that flag is SAID, so the table,
the preview panel, the candidate dropdown and the export cannot drift
into three different vocabularies for the same fact.

The marker deliberately goes on the unmeasured number rather than the
measured one: a real score is the normal case and should read as an
ordinary number, and an unmarked "~" is the thing worth noticing.
"""
from __future__ import annotations

from typing import Optional

# Prefixed, not suffixed: it has to be visible even when a narrow column
# elides the text, and the "%" end is what gets cut.
ESTIMATE_MARK = "~"

# The chip key the table paints an unmeasured cell with. Deliberately the
# same idiom as the Status and Sent columns (gui/table_delegates.py)
# rather than a third kind of marker.
ESTIMATE_CHIP = "estimated"

MEASURED_NOTE = "measured by comparing this file with the match"
UNMEASURED_NOTE = ("the engine's ranking of its own results, not a measurement - "
                   "ascii2d and the Google engines report no similarity at all")


def similarity_label(similarity: Optional[float], measured: bool) -> str:
    """"92%" for a real score, "~92%" for a ranking. "" for no match."""
    if similarity is None:
        return ""
    return f"{'' if measured else ESTIMATE_MARK}{similarity:.0f}%"


def similarity_tooltip(similarity: Optional[float], measured: bool) -> str:
    """The long form, for wherever there is room to explain."""
    if similarity is None:
        return ""
    return f"{similarity:.0f}% - {MEASURED_NOTE if measured else UNMEASURED_NOTE}"
