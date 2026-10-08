"""Letting go of background QThreads without aborting the process.

Qt calls qFatal() - which kills the process immediately, with no Python
traceback and nothing written to the log - if a QThread is destroyed while
its thread is still going. PyQt holds its own reference to a running
QThread and drops it the moment the thread finishes, so a worker the app
no longer references gets destroyed inside that finishing window:
replacing `self.x_worker` while the old one was still fetching left it
with no owner, and it took the process down a second or two later when
its fetch completed.

That is the entire reason this exists. It was previously three methods
and a set living on MainWindow, which is where every other concern lived
too; the logic is about QThread lifetime and nothing about a window, and
it is far easier to reason about - and to test, without constructing a
whole GUI - on its own.
"""
from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import QTimer

from core.applog import get_logger

log = get_logger("gui.worker_lifecycle")

# Long enough for a worker that is about to finish anyway, short enough
# not to stall the GUI thread noticeably. Callers tearing down a whole
# batch pass something larger.
DEFAULT_GRACE_MS = 50


class WorkerRegistry:
    """Holds workers that have been replaced but whose threads are still
    running, and releases each one only when it is genuinely finished."""

    def __init__(self):
        self._retiring: set = set()

    def __len__(self) -> int:
        return len(self._retiring)

    def __contains__(self, worker) -> bool:
        return worker in self._retiring

    @property
    def retiring(self) -> frozenset:
        """The parked workers, for tests and diagnostics."""
        return frozenset(self._retiring)

    def prune(self) -> None:
        """Lets go of retired workers whose thread has genuinely ended.

        The isRunning() test is the whole point: Qt emits QThread::finished
        from *inside* the dying thread, while its `running` flag is still set
        and before `finished` is set. Releasing the last reference during that
        window is precisely what makes the destructor abort, so "finished was
        emitted" is not good enough - only "no longer running" is.
        """
        for worker in [w for w in self._retiring if not w.isRunning()]:
            self._retiring.discard(worker)

    def retire(self, worker, grace_ms: int = DEFAULT_GRACE_MS) -> None:
        """Stops referring to a background worker without ever destroying a
        QThread that is still running.

        Drops its signals (its result describes a selection the user has
        already moved on from, and must not reach the GUI), asks it to stop if
        it knows how, then gives it a moment. If it is still running after
        that, parks a reference here so nothing can collect it, and releases it
        only once isRunning() says the thread has really ended.
        """
        self.prune()
        if worker is None:
            return
        try:
            worker.disconnect()  # no stale results into a GUI that has moved on
        except TypeError:
            pass  # nothing was connected to it
        stop = getattr(worker, "stop", None)
        if callable(stop):
            stop()
        if not worker.isRunning():
            return
        worker.wait(grace_ms)
        if not worker.isRunning():
            return

        self._retiring.add(worker)
        # Re-check on the main thread shortly after it reports finished, when
        # the thread has actually wound up and letting go of it is safe.
        worker.finished.connect(lambda: QTimer.singleShot(0, self.prune))

    def retire_attrs(self, owner, names: Iterable[str],
                     grace_ms: int = DEFAULT_GRACE_MS,
                     clear: bool = False) -> None:
        """Retires each of `owner`'s named worker attributes.

        The same loop appeared three times - on quit, when replacing the
        working list, and when clearing it - each with its own spelling of
        the worker names. Passing the names keeps that list at the call
        site, where it is meaningful, while the sequence stays here.

        `clear` also sets each attribute to None, which the callers
        replacing a list want and the one shutting down does not care
        about.
        """
        for name in names:
            worker = getattr(owner, name, None)
            if worker is None:
                continue
            self.retire(worker, grace_ms=grace_ms)
            if clear:
                setattr(owner, name, None)

    def wait_for(self, worker, timeout_ms: int) -> Optional[bool]:
        """Waits for one worker to finish before retiring it elsewhere.

        Used for the session autosave on quit: letting a background write
        land after the final save would put a slightly older session on
        top of the one meant to be authoritative.
        """
        if worker is None or not worker.isRunning():
            return None
        log.debug("Waiting up to %dms for %s to finish", timeout_ms, type(worker).__name__)
        return worker.wait(timeout_ms)
