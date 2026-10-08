"""Keyboard bindings for the review pass, and the user's overrides.

The search half of this app is unattended: you start it and walk away
for a day or more. The review half is not - deciding what to do with
each result is a person sitting there doing it a few thousand times -
and until now it was the half with no keyboard at all. Two shortcuts
existed in the whole program, Ctrl+S and Ctrl+O, so every review was
click the row, click the candidate dropdown, right-click, find Compare,
close, right-click again, find the send entry.

This is the registry of what can be bound, what it is bound to by
default, and how a saved override is merged over that. No Qt here: the
key sequences are strings, which is what QKeySequence takes and what the
config file stores, so all of it is testable without a display.

Two rules the rest of the code depends on.

An override of "" is not the same as no override. Absent means "use the
default"; empty means the user deliberately cleared the binding and
wants that key back for something else. Collapsing the two would make a
cleared binding reappear on the next launch.

An id that is no longer in the registry is dropped on resolve rather
than carried along. Bindings are saved as a plain id -> key map, so a
config written by a build that had an action this one doesn't would
otherwise keep claiming a key that nothing can ever fire.
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional


class ReviewAction(NamedTuple):
    """One bindable thing.

    `needs_single_row` marks the ones that only make sense on exactly one
    image - comparing two pictures side by side has no meaning for twelve
    selected rows - so the handler can say so instead of acting on an
    arbitrary one of them.
    """
    id: str
    label: str
    default: str
    description: str = ""
    needs_single_row: bool = False


# Ordered as they appear in the Shortcuts tab, grouped by what they do.
#
# Every default is a bare key rather than a Ctrl/Alt combination, which
# is only safe because these are bound to the image table rather than to
# the window: see install() in gui/review_shortcuts.py. A bare "C" as a
# window-wide shortcut would make the filter box and the tag editor
# impossible to type in.
REVIEW_ACTIONS: tuple = (
    ReviewAction(
        "next_row", "Next image", "J",
        "Moves the selection down one row. The arrow keys still work too.",
    ),
    ReviewAction(
        "prev_row", "Previous image", "K",
        "Moves the selection up one row.",
    ),
    ReviewAction(
        "next_unreviewed", "Next image needing review", "N",
        "Skips ahead to the next row you haven't decided about - neither sent to "
        "Hydrus nor marked reviewed. This is the one that makes a long review pass "
        "resumable: you never have to find your place again by eye.",
    ),
    ReviewAction(
        "prev_unreviewed", "Previous image needing review", "Shift+N",
        "The same search, backwards.",
    ),
    ReviewAction(
        "next_candidate", "Next match candidate", "]",
        "Steps forward through the match dropdown, so alternatives can be read "
        "without reaching for the mouse.",
    ),
    ReviewAction(
        "prev_candidate", "Previous match candidate", "[",
        "Steps back through the match dropdown.",
    ),
    ReviewAction(
        "compare", "Compare with match", "C",
        "Opens the compare window on the selected image.",
        needs_single_row=True,
    ),
    ReviewAction(
        "open_match", "Open match in browser", "O",
        "Opens the matched page in your browser.",
        needs_single_row=True,
    ),
    ReviewAction(
        "show_in_hydrus", "Show in Hydrus", "H",
        "Opens a Hydrus page holding the selected files and switches Hydrus to it. "
        "Pairs with O: that opens the match in a browser, this opens your own copy "
        "in Hydrus. Needs the \"manage pages\" permission on your access key.",
    ),
    ReviewAction(
        "send_upload", "Send file + URL + tags to Hydrus", "Return",
        "The main send: uploads your local file with the matched URL and the tag "
        "list attached. Bound to Return because on most images it is the whole "
        "decision.",
    ),
    ReviewAction(
        "send_url", "Send URL to Hydrus's URL importer", "U",
        "Hands the matched URL to Hydrus's own downloader instead of uploading "
        "your local copy.",
    ),
    ReviewAction(
        "download_send", "Download match + send to Hydrus", "D",
        "Downloads the full-resolution match and sends that, rather than your "
        "local file.",
    ),
    ReviewAction(
        "research", "Re-search", "F5",
        "Searches the selected images again, ignoring the cached result.",
    ),
    ReviewAction(
        "reset_result", "Reset result", "R",
        "Puts the selected images back to Not searched. Asks first, as the "
        "menu entry does.",
    ),
    ReviewAction(
        "toggle_reviewed", "Mark / unmark as reviewed", "Space",
        "Records that you looked at this one and decided against sending it. "
        "Nothing is sent or changed on disk - it just takes the row out of "
        "\"needs review\", so N skips past it from now on. Press again to undo.",
    ),
    ReviewAction(
        "remove_row", "Remove from list", "Del",
        "Takes the selected images out of this list. The files, and Hydrus, "
        "are untouched.",
    ),
)

ACTIONS_BY_ID: Dict[str, ReviewAction] = {a.id: a for a in REVIEW_ACTIONS}


def default_bindings() -> Dict[str, str]:
    """The out-of-the-box key for every action."""
    return {a.id: a.default for a in REVIEW_ACTIONS}


def resolve(overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The effective binding for every action: the user's override where
    there is one, the default otherwise.

    Overrides for ids this build doesn't have are dropped - see the
    module docstring. The result always has exactly one entry per
    registered action, so callers never need to guess at a missing key.
    """
    bindings = default_bindings()
    for action_id, key in (overrides or {}).items():
        if action_id not in ACTIONS_BY_ID:
            continue
        if not isinstance(key, str):
            # A hand-edited config can put anything here, and a non-string
            # would blow up inside QKeySequence rather than in the config
            # layer where it could be understood.
            continue
        bindings[action_id] = key.strip()
    return bindings


def prune(bindings: Dict[str, str]) -> Dict[str, str]:
    """What is worth writing to the config: only the ones that actually
    differ from the default.

    Saving all fourteen every time would freeze today's defaults into
    every existing config, so a later build that improves one could never
    hand it to anyone who had opened the Shortcuts tab once.
    """
    defaults = default_bindings()
    return {
        action_id: key for action_id, key in bindings.items()
        if action_id in defaults and key != defaults[action_id]
    }


def normalize(key: str) -> str:
    """A key in the form used for comparing two bindings.

    Qt renders sequences canonically, so this only has to handle the
    slop a config file can contain - surrounding space, and case, since
    "j" and "J" are one key on every keyboard this runs on.
    """
    return (key or "").strip().casefold()


def conflicts(bindings: Dict[str, str]) -> Dict[str, List[str]]:
    """Keys bound to more than one action, as key -> the action ids on it.

    Worth blocking rather than warning about. Qt's response to an
    ambiguous shortcut is to fire neither one, so a duplicate doesn't
    produce a wrong action - it produces a key that silently does
    nothing, which is the hardest kind of broken to work out from the
    outside. Unbound ("") is never a conflict; any number of actions can
    have no key.
    """
    seen: Dict[str, List[str]] = {}
    for action_id, key in bindings.items():
        canonical = normalize(key)
        if not canonical:
            continue
        seen.setdefault(canonical, []).append(action_id)
    return {
        key: ids for key, ids in seen.items() if len(ids) > 1
    }
