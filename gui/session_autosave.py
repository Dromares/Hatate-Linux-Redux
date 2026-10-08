"""Writing the automatic session on a timer, off the GUI thread.

A search over a large batch runs for hours or days, so a crash or a power
cut in the middle would otherwise lose which files had already been sent
to Hydrus. That is the whole point of autosaving, and it is why the write
happens on a worker: at 29,000 entries doing it inline froze the window
for as long as the encode took, on a timer, forever.

Split out of MainWindow because none of it is about a window - it is a
timer, a worker, and the bookkeeping that makes each save incremental.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from PyQt6.QtCore import QTimer

from core.applog import get_logger
from workers.session_autosave_worker import SessionAutosaveWorker

log = get_logger("gui.session_autosave")

# Below this the timer would spend more time saving than not. The setting
# is the user's, but it is not allowed to be self-defeating.
MIN_INTERVAL_SECONDS = 10

# How long to let an in-flight write finish on quit. It happens BEFORE the
# final save: letting a background write land afterwards would put a
# slightly older session on top of the one meant to be authoritative.
SHUTDOWN_WAIT_MS = 10_000


class SessionAutosaver:
    """Owns the autosave timer, its worker, and the revision bookkeeping
    that lets each save rewrite only what changed."""

    def __init__(self, entries_provider: Callable, workers, parent=None):
        """`entries_provider` returns the CURRENT working list - several
        paths rebind it wholesale rather than mutating it, so a captured
        reference would save the wrong list. `workers` is the
        WorkerRegistry that keeps a finishing QThread alive long enough
        not to abort the process."""
        self._entries = entries_provider
        self._workers = workers
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self.save_now)
        self.worker: Optional[SessionAutosaveWorker] = None
        # {path: revision} as of the last successful save. Drives the
        # incremental write - only entries whose revision has moved since
        # get re-encoded. None means "nothing written yet, write it all".
        self._saved_revisions: Optional[Dict[str, int]] = None
        self._pass = 0

    def mark_saved(self, revisions: Dict[str, int]) -> None:
        """Records that these entries are already on disk as they stand -
        called with the list just restored from the automatic session, so
        the first autosave writes what changed rather than everything."""
        self._saved_revisions = dict(revisions)

    def apply_settings(self, settings) -> None:
        """(Re)starts or stops the timer to match the settings. Called at
        startup and whenever preferences are applied, so a changed
        interval takes effect immediately rather than at the next launch."""
        self._timer.stop()
        if not settings.autosave_session:
            log.info("Session autosave is off")
            return
        interval = max(MIN_INTERVAL_SECONDS, int(settings.autosave_interval_seconds))
        self._timer.start(interval * 1000)
        log.info("Session autosave every %ds", interval)

    def save_now(self) -> None:
        """Starts a background write, if one is warranted.

        Only the app's own session file - a session the user saved by name
        is theirs, and silently overwriting it would be a nasty surprise.
        An empty list is skipped so that clearing the list doesn't quietly
        destroy a session that still had useful contents.
        """
        entries = self._entries()
        if not entries:
            return
        if self.worker is not None and self.worker.isRunning():
            # The previous one hasn't finished. Queuing another would just
            # serialize the same list twice; the next tick will catch up.
            log.debug("Skipping autosave - the previous one is still writing")
            return

        self._workers.retire(self.worker)
        self._pass += 1
        self.worker = SessionAutosaveWorker(entries, self._saved_revisions, self._pass)
        self.worker.done.connect(self._on_done)
        self.worker.start()

    def shutdown(self) -> None:
        """Lets an in-flight write finish, then stops the timer.

        Ordering matters and is the reason this is one call: the wait has
        to happen before the caller's final save, or a background write
        lands on top of the one meant to be authoritative.
        """
        self._workers.wait_for(self.worker, SHUTDOWN_WAIT_MS)
        self._workers.retire(self.worker, grace_ms=2000)
        self._timer.stop()

    def _on_done(self, ok: bool, count: int, revisions: object) -> None:
        if ok:
            # Remember what was written, so the next pass only re-encodes
            # what has changed since. Kept on failure too - the worker
            # hands back the PREVIOUS map then, so a failed save can't
            # convince the next one that unwritten rows are current.
            self._saved_revisions = revisions if isinstance(revisions, dict) else None
            log.debug("Autosaved session (%d entries)", count)
        else:
            log.warning("Autosave failed - see earlier log lines for why")
