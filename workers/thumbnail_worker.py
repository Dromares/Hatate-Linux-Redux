from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from core.applog import get_logger
from core.config import Settings
from core.hydrus_client import HydrusClient, HydrusError
from core.models import ImageEntry

log = get_logger("thumbnail_worker")


class ThumbnailWorker(QThread):
    """Decodes row thumbnails in the background.

    Decoding these on the GUI thread meant adding a large batch froze the
    whole window - a thousand-image batch on a network share is minutes of
    blocking disk reads during which Qt can't repaint, so the app looks
    hung on whatever message was showing when it started.

    Emits QImage rather than QPixmap deliberately: QPixmap is only safe to
    construct on the GUI thread, while QImage is safe anywhere. The GUI
    thread converts each one as it arrives."""

    thumbnail_ready = pyqtSignal(object, object)  # ImageEntry, QImage
    progress = pyqtSignal(int, int)               # done, total
    finished_all = pyqtSignal()

    # Hashes are checked against Hydrus in batches rather than one call
    # per file. 256 matches the batch size Hydrus's own client uses when
    # loading a page of thumbnails.
    HASH_CHECK_BATCH = 256

    def __init__(self, entries: List[ImageEntry], size: int,
                 settings: Optional[Settings] = None, parent=None):
        super().__init__(parent)
        self.entries = entries
        self.size = size
        self.settings = settings
        self._stop_requested = False

    def _thumbnail_source(self) -> str:
        return getattr(self.settings, "thumbnail_source", "local") if self.settings else "local"

    def _known_to_hydrus(self, client: HydrusClient) -> set:
        """Which of this batch's files Hydrus already has.

        Asked up front, in batches, because /get_files/thumbnail never
        404s - an unknown file comes back as Hydrus's generic icon. Only
        hashes confirmed here are safe to fetch thumbnails for."""
        hashes = [e.hydrus_hash for e in self.entries if e.hydrus_hash]
        if not hashes:
            return set()

        known = set()
        for i in range(0, len(hashes), self.HASH_CHECK_BATCH):
            if self._stop_requested:
                break
            batch = hashes[i:i + self.HASH_CHECK_BATCH]
            try:
                known |= client.filter_known_hashes(batch)
            except HydrusError as exc:
                log.warning("Could not check hashes against Hydrus: %s", exc)
                break
        log.info("Hydrus already has %d/%d file(s) in this batch", len(known), len(hashes))
        return known

    def _decode_from_bytes(self, data: bytes):
        image = QImage()
        if not image.loadFromData(data):
            return None
        if image.width() > self.size or image.height() > self.size:
            image = image.scaled(
                QSize(self.size, self.size),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return image

    def stop(self):
        self._stop_requested = True

    def _decode(self, path: str):
        """Scaled decode - asks the decoder for a small image directly
        rather than loading full resolution and shrinking afterwards,
        which matters a lot for the very large files this app handles."""
        try:
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            original = reader.size()
            if original.isValid() and (original.width() > self.size or original.height() > self.size):
                scaled = original.scaled(
                    QSize(self.size, self.size), Qt.AspectRatioMode.KeepAspectRatio,
                )
                reader.setScaledSize(scaled)
            image = reader.read()
            return image if not image.isNull() else None
        except Exception as exc:
            log.debug("Could not decode thumbnail for %s: %s", path, exc)
            return None

    def run(self):
        total = len(self.entries)
        source = self._thumbnail_source()

        if source == "off":
            log.info("Row thumbnails are turned off - not reading any of the %d file(s)", total)
            self.finished_all.emit()
            return

        client: Optional[HydrusClient] = None
        known_hashes: set = set()
        if source == "hydrus" and self.settings and self.settings.hydrus.access_key:
            client = HydrusClient(self.settings.hydrus)
            known_hashes = self._known_to_hydrus(client)
        elif source == "hydrus":
            log.info("Thumbnail source is 'hydrus' but no Hydrus access key is set - decoding locally")

        log.info(
            "Generating %d row thumbnail(s) in the background (source=%s, %d from Hydrus)",
            total, source, len(known_hashes),
        )
        from_hydrus = 0
        decoded_locally = 0
        for i, entry in enumerate(self.entries):
            if self._stop_requested:
                log.info("Thumbnail generation stopped early at %d/%d", i, total)
                break

            image = None
            if client is not None and entry.hydrus_hash in known_hashes:
                data = client.get_thumbnail(entry.hydrus_hash)
                if data:
                    image = self._decode_from_bytes(data)
                    if image is not None:
                        from_hydrus += 1
            if image is None:
                # Either not a Hydrus file, or Hydrus couldn't supply a
                # usable thumbnail - fall back to reading the file, which
                # is what this always used to do.
                image = self._decode(entry.path)
                if image is not None:
                    decoded_locally += 1

            if image is not None:
                self.thumbnail_ready.emit(entry, image)
            self.progress.emit(i + 1, total)

        log.info(
            "Finished row thumbnails: %d from Hydrus (no file read), %d decoded from disk",
            from_hydrus, decoded_locally,
        )
        self.finished_all.emit()
