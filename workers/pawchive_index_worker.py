"""Background work for the Pawchive Index window.

Indexing an artist is minutes of polite requests, and the artist search
downloads pawchive's full creator list (~15 MB) the first time - neither
belongs on the GUI thread.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.pawchive_index import PawchiveIndex, find_creators, index_creator

log = get_logger("pawchive_index_worker")


class PawchiveIndexWorker(QThread):
    """Indexes (or refreshes) a list of artists, one after another."""

    progress = pyqtSignal(str, int, int)        # message, done, total (0 = unknown)
    creator_done = pyqtSignal(object)           # CrawlResult
    finished_all = pyqtSignal()

    def __init__(self, jobs: List[Tuple[str, str]], index_path=None, parent=None):
        super().__init__(parent)
        self.jobs = list(jobs)
        self.index_path = index_path
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self):
        index = PawchiveIndex(self.index_path)
        try:
            for service, user_id in self.jobs:
                if self._stop:
                    break
                result = index_creator(
                    index, service, user_id,
                    should_stop=lambda: self._stop,
                    on_progress=lambda message, done, total: self.progress.emit(message, done, total),
                )
                self.creator_done.emit(result)
        except Exception as exc:
            # A background tool must not be able to take the app down.
            log.exception("Pawchive indexing failed: %s", exc)
        finally:
            self.finished_all.emit()


class PawchiveFindWorker(QThread):
    """Searches pawchive's artists by name."""

    done = pyqtSignal(object, object)           # list of creator dicts, error text or None

    def __init__(self, query: str, cache_path=None, parent=None):
        super().__init__(parent)
        self.query = query
        self.cache_path = cache_path

    def run(self):
        found: list = []
        error: Optional[str] = None
        try:
            found = find_creators(self.query, cache_path=self.cache_path)
        except Exception as exc:
            error = str(exc)
            log.warning("Pawchive artist search failed: %s", exc)
        self.done.emit(found, error)
