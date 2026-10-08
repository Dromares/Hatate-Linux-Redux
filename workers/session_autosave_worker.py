from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.models import ImageEntry
from core.session_db import save_entries

log = get_logger("session_autosave_worker")


class SessionAutosaveWorker(QThread):
    """Writes the automatic session without freezing the window.

    Autosaving used to run inline on a timer, and at 29,000 entries that
    was a third of a second of dict-building plus another sixth of
    serializing - a visible stall every two minutes, growing with the
    list. Two thirds of that cost is reading the entries, not the file
    I/O, so moving only the write off the GUI thread would have bought
    very little; the whole job runs here instead.

    The entry LIST is copied by the caller, on the GUI thread, before this
    starts - see __init__. The entries themselves are read live, which is
    what the app already did: a search worker mutates them while an
    autosave is in progress either way. The lists inside them are
    snapshotted as they are read (core.session._snapshot), so a
    concurrent append can't raise mid-write.

    The result is therefore a slightly smeared moment rather than an exact
    one - some entries a little newer than others. That is the right
    trade for a periodic crash-recovery file, and the next pass corrects
    it. Anything that must be exact (quitting, "Save Session As") still
    saves synchronously.
    """

    done = pyqtSignal(bool, int, object)   # succeeded, entry count, revisions written

    def __init__(self, entries: List[ImageEntry],
                 last_revisions: Optional[Dict[str, int]] = None,
                 pass_number: int = 0, parent=None):
        super().__init__(parent)
        # Copy the list itself here, on the caller's thread: it costs
        # microseconds and means rows added or removed while the save runs
        # cannot change what is being written out from under it.
        self.entries = list(entries)
        # What the last successful save wrote, so only rows whose entry
        # has moved since get re-encoded. None means "write everything".
        self.last_revisions = dict(last_revisions) if last_revisions is not None else None
        self.pass_number = pass_number

    def run(self):
        ok = False
        revisions = self.last_revisions or {}
        try:
            ok, revisions = save_entries(
                self.entries, self.last_revisions, pass_number=self.pass_number,
            )
        except Exception as exc:
            # Never let this escape: an exception out of a QThread's run()
            # is delivered into Qt and aborts the process, and a failed
            # autosave is not worth that.
            log.exception("Session autosave failed: %s", exc)
            ok = False
        self.done.emit(ok, len(self.entries), revisions)
