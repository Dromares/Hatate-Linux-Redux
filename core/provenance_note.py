"""What this app knows about where a file came from, as text for a note.

Every fact here is already on the screen and already in the export
(core/export.py's FIELDS) - engine, site, similarity, matched URL. What
the library itself keeps is none of it: Hydrus ends up holding the file
and its tags, and nothing that says an automated tool put them there, let
alone which search engine found the match or how good it was. Six months
later the only record of that is the app's own session file, if it still
exists.

So this renders the same handful of facts into a note Hydrus can store on
the file, and it is the ONE place that decides their wording - the same
reason core/similarity_display.py exists for the similarity marker alone.

Three rules the text follows, all of them about not overstating what is
known:

  * The similarity carries its honesty marker. An ascii2d or Google
    result's number is that result's POSITION in a list, not a
    measurement, and similarity_display is what says so - a bare "80%"
    written into somebody's library would be a claim this app cannot
    support. See core/similarity_display.py.
  * A fact that is missing is left out, not written as "unknown". A note
    listing empty fields reads as a failure; a shorter note reads as a
    shorter note.
  * With no match at all there is no note. An uploaded file with no
    matched URL and no engine has no provenance to record, and "imported
    by Hatate" alone is noise in a library the user has to live with.
"""
from __future__ import annotations

import time
from typing import List, Optional

from .models import ImageEntry
from .similarity_display import MEASURED_NOTE, UNMEASURED_NOTE, similarity_label

# The note's name in Hydrus, and therefore the note this app owns and
# overwrites. Namespaced with the app's name on purpose: it has to be
# obvious in Hydrus's own notes panel which tool wrote it, and it must
# not collide with a name a user would pick for their own note.
DEFAULT_NOTE_NAME = "hatate: match source"

# How the date is written. Local time, to the minute: this is a record for
# a person reading their own library, not a timestamp to compute with, and
# seconds add noise to something whose useful resolution is "that
# afternoon". Ordered biggest-unit-first so a notes panel sorts and reads
# sensibly whatever the reader's locale.
DATE_FORMAT = "%Y-%m-%d %H:%M"

FIRST_LINE = "Match found by Hatate-Linux-Redux."


def note_name(configured: Optional[str]) -> str:
    """The configured note name, or the default for a blank one.

    A blank setting means "I did not choose", not "write the note with no
    name" - Hydrus needs a name, and an empty one is not something to
    push into somebody's library for them to find later.
    """
    return (configured or "").strip() or DEFAULT_NOTE_NAME


def _similarity_line(entry: ImageEntry) -> Optional[str]:
    if entry.similarity is None:
        return None
    label = similarity_label(entry.similarity, entry.similarity_measured)
    note = MEASURED_NOTE if entry.similarity_measured else UNMEASURED_NOTE
    return f"Similarity: {label} - {note}"


def provenance_note(entry: ImageEntry, sent_at: Optional[float] = None) -> Optional[str]:
    """The note's text for this entry, or None when there is nothing worth
    writing.

    `sent_at` is the moment being recorded, defaulting to now. It is the
    IMPORT's date rather than the search's: the note describes a thing
    that was done to the library, and `last_searched` is already the
    search's own record. Passed in rather than read from the clock inside
    so the text is testable and so a batch's notes all agree.
    """
    engine = entry.selected_candidate.engine if entry.selected_candidate else ""
    if not entry.matched_url and not engine:
        # Nothing was matched - see the module docstring.
        return None

    lines: List[str] = [FIRST_LINE]
    if engine:
        lines.append(f"Engine: {engine}")
    if entry.booru_name:
        lines.append(f"Site: {entry.booru_name}")
    similarity = _similarity_line(entry)
    if similarity:
        lines.append(similarity)
    if entry.matched_url:
        lines.append(f"Match: {entry.matched_url}")
    moment = time.time() if sent_at is None else sent_at
    lines.append(f"Sent to Hydrus: {time.strftime(DATE_FORMAT, time.localtime(moment))}")
    return "\n".join(lines)
