"""Installs the review pass's keyboard bindings on the main window.

The registry, the defaults and the user's overrides live in
core/shortcuts.py, which has no Qt in it. This is the half that needs a
window: it turns each binding into a QShortcut and points it at the
MainWindow method the right-click menu already calls, so a key and its
menu entry can never drift apart.

Everything is bound to the image table with WidgetWithChildrenShortcut,
not to the window. That choice is what lets the defaults be bare letters.
A window-wide "C" would fire while the cursor sat in the filter box or
the tag editor, and typing "cat" into the filter would compare an image,
open a dialog and eat the keystrokes. Scoped to the table, the keys work
exactly where the review happens and nowhere else - the filter box and
the tag list keep every character for themselves.

The Review page gets a second copy of every binding, scoped the same way
to that page. The table is hidden while Review is up, so without these
the keys the page advertises ("J/K move") would do nothing there. A text
field on the page (the in-place tag editor) still keeps its characters:
Qt lets a focused line edit claim printable keys before any shortcut.
"""
from __future__ import annotations

from typing import Callable, Dict, List

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QKeySequence, QShortcut

from core.applog import get_logger
from core.shortcuts import ACTIONS_BY_ID, resolve

log = get_logger("gui.shortcuts")


def _selected_rows(window) -> List[int]:
    return sorted(i.row() for i in window.table.selectionModel().selectedRows())


def _selected_entries(window) -> List:
    """The selected rows, resolved to entries right away.

    A key press runs its handler synchronously with nothing else able to
    run in between, so this isn't closing a race the way the context menu
    is - but the handlers it feeds now take entries, not row numbers, to
    match what the menu passes them.
    """
    return window.table_model.entries_at(_selected_rows(window))


def _move_selection(window, delta: int) -> None:
    """Steps the selection by one visible row.

    Rows are addressed through the model's filtered view, so this walks
    what is on screen rather than the underlying list - stepping into a
    row the filter is hiding would move the selection somewhere the user
    cannot see.
    """
    total = window.table_model.rowCount()
    if total <= 0:
        return
    rows = _selected_rows(window)
    if not rows:
        target = 0 if delta > 0 else total - 1
    else:
        # From a multi-row selection, carry on from the end being moved
        # toward rather than collapsing to the top every time.
        anchor = rows[-1] if delta > 0 else rows[0]
        target = anchor + delta
    if not 0 <= target < total:
        return          # already at the end; stop rather than wrapping
    window.select_table_row(target)


def _move_to_unreviewed(window, delta: int) -> None:
    """Jumps to the next/previous row still wanting a decision.

    This is the one that makes a review pass over thousands of rows
    resumable: without it, finding where you got to means scrolling and
    reading columns by eye. "Wanting a decision" is neither sent nor
    marked reviewed - see ImageEntry.needs_review.
    """
    total = window.table_model.rowCount()
    if total <= 0:
        return
    rows = _selected_rows(window)
    start = (rows[-1] if delta > 0 else rows[0]) if rows else -1 if delta > 0 else total
    row = start + delta
    while 0 <= row < total:
        entry = window.table_model.entry_at(row)
        if entry is not None and entry.needs_review:
            window.select_table_row(row)
            return
        row += delta
    direction = "after" if delta > 0 else "before"
    window.status_label.setText(f"Nothing left to review {direction} this one")


def _toggle_reviewed(window) -> None:
    """Marks the selection reviewed, or unmarks it if it already is.

    A mixed selection marks: that is the common intent, and it is the
    direction that can be undone by pressing the same key again.
    """
    entries = _selected_entries(window)
    already = bool(entries) and all(e.reviewed for e in entries)
    window._set_rows_reviewed(entries, not already)


def _step_candidate(window, delta: int) -> None:
    """Steps through the match dropdown.

    Setting the index is enough: the combo's own currentIndexChanged
    already drives fetching and previewing the candidate, so this must
    not duplicate any of that.
    """
    combo = window.candidate_combo
    if not combo.isEnabled() or combo.count() <= 1:
        return
    target = combo.currentIndex() + delta
    if not 0 <= target < combo.count():
        return
    combo.setCurrentIndex(target)


def handlers(window) -> Dict[str, Callable[[], None]]:
    """Action id -> what it does, for every id in the registry.

    Each of these is the same entry point the right-click menu uses, so
    a key does exactly what the menu item of that name does - including
    its confirmation prompt, which a keyboard shortcut is a better reason
    to keep than to skip.
    """
    return {
        "next_row": lambda: _move_selection(window, +1),
        "prev_row": lambda: _move_selection(window, -1),
        "next_unreviewed": lambda: _move_to_unreviewed(window, +1),
        "prev_unreviewed": lambda: _move_to_unreviewed(window, -1),
        "next_candidate": lambda: _step_candidate(window, +1),
        "prev_candidate": lambda: _step_candidate(window, -1),
        "compare": lambda: window._open_compare_dialog(window._current_entry()),
        "open_match": window.action_open_matched_url,
        "show_in_hydrus": lambda: window._show_rows_in_hydrus(_selected_entries(window)),
        "send_upload": window.action_send_to_hydrus,
        "send_url": window.action_import_url_to_hydrus,
        "download_send": window.action_download_and_send_to_hydrus,
        "research": lambda: window._research_rows(_selected_entries(window)),
        "reset_result": lambda: window._reset_rows(_selected_entries(window)),
        "remove_row": lambda: window._remove_rows(_selected_entries(window)),
        "toggle_reviewed": lambda: _toggle_reviewed(window),
    }


def _guarded(window, action_id: str, handler: Callable[[], None]) -> Callable[[], None]:
    """Wraps a handler in the checks its menu entry gets for free.

    The right-click menu greys out what cannot run - an entry needing one
    row is disabled while twelve are selected. A shortcut has no greyed-out
    state to show, so the same conditions are checked here and answered in
    the status bar. Silently doing nothing would read as a broken key.
    """
    action = ACTIONS_BY_ID[action_id]
    navigational = action_id in (
        "next_row", "prev_row", "next_unreviewed", "prev_unreviewed",
        "next_candidate", "prev_candidate",
    )

    def run() -> None:
        if not navigational:
            rows = _selected_rows(window)
            if not rows:
                window.status_label.setText(f"{action.label}: select an image first")
                return
            if action.needs_single_row and len(rows) > 1:
                window.status_label.setText(
                    f"{action.label}: works on one image at a time "
                    f"({len(rows)} selected)"
                )
                return
        handler()

    return run


def install(window) -> None:
    """(Re)binds every review shortcut from the current settings.

    Safe to call again after the Shortcuts tab is edited: the previous
    QShortcut objects are destroyed first. Without that, an old binding
    would keep firing alongside the new one, and Qt would see the two of
    them as ambiguous and fire neither.
    """
    for existing in (getattr(window, "_review_shortcuts", [])
                     + getattr(window, "_review_page_shortcuts", [])):
        existing.setParent(None)
        existing.deleteLater()
    window._review_shortcuts = []
    window._review_page_shortcuts = []
    review_page = getattr(window, "review_page", None)

    bindings = resolve(getattr(window.settings, "review_shortcuts", None))
    action_handlers = handlers(window)
    installed = 0
    for action_id, key in bindings.items():
        handler = action_handlers.get(action_id)
        if handler is None:
            # A registry entry with no handler is the "menu item that does
            # nothing" bug in another costume; there is a test that this
            # never happens, but never bind a dead key even so.
            log.error("No handler for review shortcut %r - not binding it", action_id)
            continue
        if not key:
            continue        # deliberately unbound by the user
        sequence = QKeySequence(key)
        if sequence.isEmpty():
            log.warning("Ignoring unparseable shortcut %r for %r", key, action_id)
            continue
        guarded = _guarded(window, action_id, handler)
        shortcut = QShortcut(sequence, window.table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(guarded)
        window._review_shortcuts.append(shortcut)
        if review_page is not None:
            on_page = QShortcut(sequence, review_page)
            on_page.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            on_page.activated.connect(guarded)
            window._review_page_shortcuts.append(on_page)
        installed += 1
    log.debug("Installed %d review shortcut(s) on the image table", installed)


def open_matched_url(window, entry) -> None:
    """Opens a match's page in the browser, with the checks the preview's
    own right-click menu makes."""
    from gui import message
    if entry is None or not entry.matched_url:
        window.status_label.setText("Open match: this image has no match")
        return
    if not entry.matched_url.startswith(("http://", "https://")):
        message.warning(
            window, "Open match in browser",
            "This match doesn't have a valid web address to open.",
        )
        return
    QDesktopServices.openUrl(QUrl(entry.matched_url))
