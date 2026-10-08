from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.config import Settings
from core.models import ImageEntry
from core.search_engine import fetch_candidate_details

log = get_logger("candidate_worker")


class CandidateFetchWorker(QThread):
    """Fetches the picture + booru tags for one MatchCandidate the user just
    picked from the dropdown. Only used for candidates that weren't already
    fetched (the top match is fetched during the main search)."""

    done = pyqtSignal(object, int)  # ImageEntry, candidate index
    wait_countdown = pyqtSignal(str, float, float)  # label, seconds remaining, total seconds -
                                                      # same shared shape as SearchWorker's

    def __init__(self, entry: ImageEntry, index: int, settings: Settings, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.index = index
        self.settings = settings

    def run(self):
        if 0 <= self.index < len(self.entry.candidates):
            candidate = self.entry.candidates[self.index]
            log.debug("Fetching details for candidate %d (%s) of %s",
                      self.index, candidate.url, self.entry.filename)
            try:
                fetch_candidate_details(
                    candidate, self.settings, on_tick=self.wait_countdown.emit,
                    local_path=self.entry.path,
                )
            except Exception as exc:
                log.exception("Unexpected error fetching candidate details: %s", exc)
        else:
            log.warning("Candidate index %d out of range for %s (%d candidates)",
                        self.index, self.entry.filename, len(self.entry.candidates))
        self.done.emit(self.entry, self.index)
