from __future__ import annotations

import os
import tempfile
from typing import List, Optional

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QListWidget, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core.applog import get_logger
from core.config import Settings
from core.hydrus_client import HydrusClient, HydrusError
from core.models import ImageEntry, Tag, TagSource
from core.tag_rules import apply_namespace_remap
from gui import message, widgets

log = get_logger("gui.hydrus_query")


class HydrusQueryDialog(QDialog):
    """Files > Query Hydrus: enter tags, list matching files, import the
    selected ones into the working list as ImageEntry objects (downloaded
    to a temp dir so IQDB/SauceNAO can read them from disk)."""

    def __init__(self, settings: Settings, existing_hashes: Optional[set] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Query Hydrus")
        self.setMinimumSize(420, 400)
        self.settings = settings
        self.client = HydrusClient(settings.hydrus)
        self.existing_hashes = existing_hashes or set()
        self.imported_entries: List[ImageEntry] = []
        self.skipped_duplicate_count = 0
        self._file_ids: List[int] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        layout.addWidget(QLabel("Tags (one per line, ANDed together):"))
        self.tags_edit = QPlainTextEdit()
        layout.addWidget(self.tags_edit)

        search_btn = QPushButton("Search Hydrus")
        search_btn.clicked.connect(self._do_search)
        layout.addWidget(search_btn)

        self.results_list = QListWidget()
        self.results_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.results_list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import selected")  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
        buttons.accepted.connect(self._do_import)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _do_search(self):
        tags = [t.strip() for t in self.tags_edit.toPlainText().splitlines() if t.strip()]
        if not tags:
            message.warning(self, "Query Hydrus", "Enter at least one tag.")
            return
        log.info("Querying Hydrus for tags: %s", tags)
        try:
            file_ids = self.client.search_files(tags)
            log.debug("Hydrus search_files returned %d file id(s)", len(file_ids))
            metadata = self.client.get_file_metadata(file_ids) if file_ids else []
        except HydrusError as exc:
            log.error("Query Hydrus search failed: %s", exc)
            message.critical(self, "Query Hydrus", str(exc))
            return

        self.results_list.clear()
        self._file_ids = []
        self._metadata = metadata
        for m in metadata:
            already_added = " [already in list]" if m.hash in self.existing_hashes else ""
            self.results_list.addItem(
                f"{m.hash[:12]}…  ({m.mime}, {m.width}x{m.height}, {len(m.tags)} tag(s)){already_added}"
            )
            self._file_ids.append(m.file_id)

        if not metadata:
            log.info("Query Hydrus: no files matched %s", tags)
            message.information(self, "Query Hydrus", "No files matched those tags.")

    def _do_import(self):
        selected_rows = [i.row() for i in self.results_list.selectedIndexes()]
        if not selected_rows:
            message.warning(self, "Query Hydrus", "Select at least one file to import.")
            return

        tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-")
        log.debug("Importing %d selected file(s) from Hydrus into %s", len(selected_rows), tmp_dir)
        failed = 0
        for row in selected_rows:
            meta = self._metadata[row]

            if meta.hash in self.existing_hashes:
                self.skipped_duplicate_count += 1
                log.debug("Skipping %s - already in the list, not downloading again", meta.hash[:12])
                continue

            ext = (meta.mime or "").split("/")[-1] or "jpg"
            dest = os.path.join(tmp_dir, f"{meta.hash}.{ext}")
            try:
                self.client.download_file(meta.file_id, dest)
            except HydrusError as exc:
                failed += 1
                log.error("Failed to download Hydrus file %s: %s", meta.hash[:12], exc)
                message.warning(self, "Query Hydrus", f"Skipped {meta.hash[:12]}: {exc}")
                continue
            entry = ImageEntry(path=dest, hydrus_file_id=meta.file_id, hydrus_hash=meta.hash)
            if meta.tags:
                remapped_tags = apply_namespace_remap(
                    [_parse_tag(name) for name in meta.tags], self.settings,
                )
                entry.add_tags(remapped_tags, replace_source=TagSource.HYDRUS)
            self.imported_entries.append(entry)
            self.existing_hashes.add(meta.hash)  # catch duplicates within this same selection too

        log.info(
            "Query Hydrus import complete: %d succeeded, %d duplicates skipped, %d failed",
            len(self.imported_entries), self.skipped_duplicate_count, failed,
        )
        self.accept()


def _parse_tag(raw: str) -> Tag:
    if ":" in raw:
        namespace, name = raw.split(":", 1)
    else:
        namespace, name = None, raw
    return Tag(name=name, source=TagSource.HYDRUS, namespace=namespace)
