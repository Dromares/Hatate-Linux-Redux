from __future__ import annotations

from typing import List

from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.models import ImageEntry
from core.upscale_detect import (
    UpscaleCheckResult, check_upscaler_metadata, compare_to_source, detect_naive_upscale,
)

log = get_logger("upscale_check_worker")


class UpscaleCheckWorker(QThread):
    """Runs the upscale-detection heuristics for a batch of entries. This
    involves several resize operations per image (the self-consistency
    test), which can take a moment for large images, so it's opt-in via
    the right-click menu rather than run automatically during search."""

    entry_checked = pyqtSignal(object, object)  # ImageEntry, UpscaleCheckResult
    finished_all = pyqtSignal()

    def __init__(self, entries: List[ImageEntry], parent=None):
        super().__init__(parent)
        self.entries = entries

    def run(self):
        log.info("Checking %d image(s) for signs of upscaling", len(self.entries))
        for entry in self.entries:
            if entry.local_width is None or entry.local_height is None:
                try:
                    with Image.open(entry.path) as im:
                        entry.local_width, entry.local_height = im.size
                except (OSError, ValueError) as exc:
                    log.warning("Could not read dimensions for %s: %s", entry.filename, exc)

            source_comparison = None
            candidate = entry.selected_candidate
            if (
                candidate and candidate.width and candidate.height
                and entry.local_width and entry.local_height
            ):
                source_comparison = compare_to_source(
                    entry.local_width, entry.local_height, candidate.width, candidate.height,
                )

            metadata_signature = check_upscaler_metadata(entry.path)
            self_consistency = detect_naive_upscale(entry.path)

            result = UpscaleCheckResult(
                source_comparison=source_comparison, self_consistency=self_consistency,
                metadata_signature=metadata_signature,
            )
            log.info(
                "%s: source_flagged=%s self_consistency=%s metadata_signature=%s",
                entry.filename,
                source_comparison.flagged if source_comparison else "n/a",
                self_consistency.confidence,
                metadata_signature,
            )
            self.entry_checked.emit(entry, result)

        self.finished_all.emit()
