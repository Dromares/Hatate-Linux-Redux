"""Finding one setting among eight tabs of them.

The Settings dialog holds on the order of a hundred controls across eight
tabs. Every one of them is somewhere sensible, and none of them is
findable: knowing that the thing you want is called something like "pause
when the quota runs out" does not tell you whether it lives under General,
Engine, Import or SauceNAO, so finding it means opening tabs and reading.

This indexes what is already there rather than asking the tabs to declare
anything. Every label, checkbox and radio button in a tab is a setting's
name as far as a person is concerned, so walking the built widget tree
gets the whole list for free - and keeps working when a setting is added,
because nothing has to be registered anywhere.

Matching is on words, not on the whole phrase: "quota pause" finds "Pause
searching when the daily quota runs out", which a substring match would
not. That matters because nobody remembers the wording, only roughly what
it said.
"""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QCheckBox, QGroupBox, QLabel, QRadioButton

# A label longer than this is an explanation, not a name. The dialog uses
# plenty of them - whole paragraphs about what an engine does - and they
# would swamp the results with entries whose text nobody searched for.
MAX_NAME_LENGTH = 90

# How long a jumped-to setting stays marked. Long enough to find with the
# eye after the tab switches, short enough not to become part of the page.
HIGHLIGHT_MS = 2000

# Dynamic properties used to remember a highlighted widget's real role.
# These live outside objectName on purpose - see highlight().
_ORIGINAL_ROLE_PROPERTY = "_settingsSearchOriginalRole"
_HIGHLIGHT_TOKEN_PROPERTY = "_settingsSearchHighlightToken"


def index_tabs(pages):
    """[(tab name, page widget)] -> [(tab index, tab name, name, widget)]."""
    found = []
    for tab_index, (tab_name, page) in enumerate(pages):
        # The tab itself, so every tab is reachable by name even when its
        # contents index to nothing - Shortcuts holds its actions in a
        # table rather than in labels, so it otherwise has no entries at
        # all and searching "shortcuts" answers nothing.
        found.append((tab_index, tab_name, tab_name, page))
        seen = set()
        for widget in page.findChildren((QLabel, QCheckBox, QRadioButton, QGroupBox)):
            text = _name_of(widget)
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append((tab_index, tab_name, text, widget))
    return found


def _name_of(widget):
    try:
        text = widget.title() if isinstance(widget, QGroupBox) else widget.text()
    except (AttributeError, RuntimeError):
        return ""
    text = (text or "").replace("&", "").strip()
    if not text or len(text) > MAX_NAME_LENGTH:
        return ""
    # A label that is only punctuation ("min", ":", "…") names nothing on
    # its own and is noise in a result list.
    if len(text.strip(" :.…")) < 3:
        return ""
    return text.rstrip(":")


def matches(entries, query):
    """Entries matching every word of the query, in any order.

    The tab's name counts as part of what an entry is called. People
    search with half a location and half a name - "hydrus key" - and the
    Hydrus tab's field is labelled just "Access key", so matching the
    label alone finds nothing for a query that is completely clear about
    what it wants.
    """
    words = [w for w in query.casefold().split() if w]
    if not words:
        return []
    out = []
    for entry in entries:
        haystack = f"{entry[1]} {entry[2]}".casefold()
        if all(word in haystack for word in words):
            out.append(entry)
    # Shorter names first: an exact-ish match is almost always a shorter
    # string than a sentence that happens to contain the same words.
    out.sort(key=lambda e: (len(e[2]), e[2]))
    return out


def highlight(widget):
    """Marks a widget briefly, then puts it back as it was.

    Uses the styled-role mechanism the rest of the app uses, which means
    it needs the repolish that goes with it - see gui.widgets.restyle.

    The role it had is read first rather than assumed empty: several of
    these labels are 'Hint's, and restoring them to nothing would leave
    them as ordinary text for the rest of the session.

    The real role is kept in a dynamic property, not in a local variable
    read from objectName, because objectName is also where restyle() puts
    "Found" while the highlight is up. A second highlight() on the same
    widget before the first revert fires (a double-click activates both
    itemClicked and itemActivated - see settings_dialog._jump_to_setting)
    would otherwise read "Found" as the value to restore to, and the
    widget would lose its real role for the rest of the session. Keeping
    the original elsewhere means a re-entrant call has nothing to corrupt.
    A token, bumped on every call, additionally makes sure only the most
    recent pending revert actually restores the widget - an earlier one
    firing after a newer highlight() would otherwise stomp on it.
    """
    from gui import widgets as house

    if widget.property(_ORIGINAL_ROLE_PROPERTY) is None:
        widget.setProperty(_ORIGINAL_ROLE_PROPERTY, widget.objectName())
    token = (widget.property(_HIGHLIGHT_TOKEN_PROPERTY) or 0) + 1
    widget.setProperty(_HIGHLIGHT_TOKEN_PROPERTY, token)
    house.restyle(widget, "Found")

    def _revert():
        try:
            if widget.property(_HIGHLIGHT_TOKEN_PROPERTY) != token:
                # A later highlight() has taken over this widget; that
                # call's own revert owns putting it back now.
                return
            restore_to = widget.property(_ORIGINAL_ROLE_PROPERTY) or ""
            widget.setProperty(_ORIGINAL_ROLE_PROPERTY, None)
            widget.setProperty(_HIGHLIGHT_TOKEN_PROPERTY, None)
            house.restyle(widget, restore_to)
        except RuntimeError:
            # The dialog was closed while the highlight was still up.
            pass

    QTimer.singleShot(HIGHLIGHT_MS, _revert)
