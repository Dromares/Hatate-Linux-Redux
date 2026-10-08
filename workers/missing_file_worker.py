from __future__ import annotations

from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.missing_files import find_missing_paths
from core.models import ImageEntry

log = get_logger("missing_file_worker")


class MissingFileWorker(QThread):
    """Checks which of a restored session's files still exist.

    Runs in the background rather than inline on session load: the check
    touches the filesystem once per directory, and on a network share
    even that is slow enough that doing it during startup would leave the
    window unresponsive before it had drawn anything. The list is usable
    immediately and rows get flagged as the answers arrive.
    """

    finished_checking = pyqtSignal(object)   # set of missing paths

    def __init__(self, entries: List[ImageEntry], parent=None):
        super().__init__(parent)
        self.entries = entries
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        paths = [e.path for e in self.entries]
        log.info("Checking whether %d session file(s) still exist", len(paths))
        missing = find_missing_paths(paths, stop_check=lambda: self._stop_requested)
        if self._stop_requested:
            log.info("Missing-file check cancelled")
            self.finished_checking.emit(set())
            return
        log.info("Missing-file check complete: %d missing", len(missing))
        self.finished_checking.emit(missing)
