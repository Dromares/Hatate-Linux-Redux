from __future__ import annotations

from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.config import Settings
from core.hydrus_client import HydrusClient
from core.hydrus_reconcile import ReconcileResult, entries_awaiting_confirmation, reconcile
from core.models import ImageEntry

log = get_logger("hydrus_reconcile_worker")


class HydrusReconcileWorker(QThread):
    """Thin Qt wrapper around core.hydrus_reconcile.reconcile.

    Off the UI thread because a large library means a request per few
    hundred hashes, and this runs at startup - freezing the window before
    it has drawn would be the worst possible time for it.
    """

    progress = pyqtSignal(int, int)          # hashes asked about, total
    done = pyqtSignal(object)                # ReconcileResult

    def __init__(self, entries: List[ImageEntry], settings: Settings, parent=None):
        super().__init__(parent)
        self.entries = entries
        self.settings = settings
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        waiting = entries_awaiting_confirmation(self.entries)
        if not waiting:
            log.debug("Nothing is waiting on Hydrus; skipping reconcile")
            self.done.emit(ReconcileResult([], [], 0))
            return

        log.info("Reconciling %d entr%s still waiting on Hydrus",
                 len(waiting), "y" if len(waiting) == 1 else "ies")
        try:
            client = HydrusClient(self.settings.hydrus)
            result = reconcile(
                waiting, client.deletion_states,
                should_stop=lambda: self._stop,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
        except Exception as exc:
            # Never let this take the app down: it runs unprompted at
            # startup, and a Hydrus that is offline or answering oddly is
            # an ordinary condition, not a fault worth a crash.
            log.warning("Could not reconcile with Hydrus: %s", exc)
            result = ReconcileResult([], [], 0)
        self.done.emit(result)
