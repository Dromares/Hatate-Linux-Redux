from __future__ import annotations

from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.config import Settings
from core.hydrus_tag_lookup import TagLookupResult, apply_existing_hydrus_tags
from core.models import ImageEntry

log = get_logger("hydrus_lookup_worker")


class HydrusTagLookupWorker(QThread):
    """Thin Qt wrapper around core.hydrus_tag_lookup.apply_existing_hydrus_tags
    - hashes newly-added files and imports any tags Hydrus already has for
    them, off the UI thread since hashing + a network round trip can take
    a moment for a large batch."""

    done = pyqtSignal(object)  # TagLookupResult

    def __init__(self, entries: List[ImageEntry], settings: Settings, parent=None):
        super().__init__(parent)
        self.entries = entries
        self.settings = settings

    def run(self):
        log.debug("Checking Hydrus for existing tags on %d newly-added file(s)", len(self.entries))
        try:
            result = apply_existing_hydrus_tags(self.entries, self.settings)
        except Exception as exc:
            log.exception("Unexpected error during Hydrus tag lookup: %s", exc)
            result = TagLookupResult(0, error=str(exc))
        self.done.emit(result)
