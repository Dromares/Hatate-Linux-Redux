"""Files > Pawchive Index: choose the artists whose images are indexed.

The exact lookup finds byte-identical copies of any pawchive post. This
window builds the index that finds RESIZED or RE-SAVED copies too, one
artist at a time - see core/pawchive_index.py for what is stored and what
it costs. Searching the index is part of every search once something is
indexed (Settings > Engine), and makes no request.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.applog import get_logger
from core.pawchive_index import PawchiveIndex, creator_from_url
from gui import message, widgets
from workers.pawchive_index_worker import PawchiveFindWorker, PawchiveIndexWorker

log = get_logger("gui.pawchive_index")


def _hairline() -> QFrame:
    """A thin rule marking where one zone of the dialog ends and the next
    begins - same shape review_view.py uses between the tag row and the
    action buttons below it."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    return line


class PawchiveIndexDialog(QDialog):
    COLUMNS = ["Artist", "Service", "Images", "Posts", "Indexed"]

    def __init__(self, entries: Callable[[], list], index_path=None, cache_path=None,
                 parent=None):
        super().__init__(parent)
        self._entries = entries
        self._index_path = index_path
        self._cache_path = cache_path
        self.index = PawchiveIndex(index_path)
        self._worker: Optional[PawchiveIndexWorker] = None
        self._finder: Optional[PawchiveFindWorker] = None
        self._results: list = []
        self._creators: list = []

        self.setWindowTitle("Pawchive Index")
        self.resize(760, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)

        intro = QLabel(
            "Finds <b>resized or re-saved</b> copies of pawchive posts by the artists "
            "listed here. Exact copies of any artist are already found by the Pawchive "
            "lookup in Settings; this catches the ones whose file has changed.<br><br>"
            "Indexing downloads a small thumbnail of each of an artist's images, about "
            "one a second, and refreshing only fetches what is new. Searching the index "
            "happens on this computer and sends nothing."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, stretch=1)

        layout.addWidget(_hairline())
        add_row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText("Artist name, or a pawchive link")
        self.query.returnPressed.connect(self._find_or_add)
        add_row.addWidget(self.query, stretch=1)
        self.find_button = QPushButton("Find / Add")
        self.find_button.setToolTip(
            "A pawchive link (to the artist or any of their posts) is added straight away.\n"
            "A name is searched for among pawchive's artists - the first search downloads "
            "their list (~15 MB), which is kept for a week.")
        self.find_button.clicked.connect(self._find_or_add)
        add_row.addWidget(self.find_button)
        self.from_matches_button = QPushButton("Add artists from my matches")
        self.from_matches_button.setToolTip(
            "Adds every artist with a pawchive match in the current list - most usefully "
            "the exact copies the Pawchive lookup found, whose other images are the likeliest "
            "to turn up again edited.")
        self.from_matches_button.clicked.connect(self._add_from_matches)
        add_row.addWidget(self.from_matches_button)
        layout.addLayout(add_row)

        layout.addWidget(_hairline())
        self.results = QListWidget()
        self.results.setVisible(False)
        self.results.setMaximumHeight(150)
        layout.addWidget(self.results)
        self.add_checked_button = QPushButton("Add the ticked artists")
        self.add_checked_button.setVisible(False)
        self.add_checked_button.clicked.connect(self._add_checked)
        layout.addWidget(self.add_checked_button)

        layout.addWidget(_hairline())
        actions = QHBoxLayout()
        self.index_selected_button = QPushButton("Index selected")
        self.index_selected_button.clicked.connect(lambda: self._start_indexing(selected_only=True))
        actions.addWidget(self.index_selected_button)
        self.index_all_button = QPushButton("Index / refresh all")
        self.index_all_button.clicked.connect(lambda: self._start_indexing(selected_only=False))
        actions.addWidget(self.index_all_button)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove_selected)
        actions.addWidget(self.remove_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip("Stops after the current request. What was indexed is kept, "
                                    "and indexing again carries on from there.")
        self.stop_button.clicked.connect(self._stop)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        layout.addLayout(actions)

        layout.addWidget(_hairline())
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self._populate()

    # -- the list -------------------------------------------------------
    def _populate(self):
        self._creators = self.index.creators()
        self.table.setRowCount(len(self._creators))
        for row, creator in enumerate(self._creators):
            images = str(creator.image_count)
            if creator.unfingerprinted:
                images += f" ({creator.unfingerprinted} without a thumbnail)"
            indexed = (time.strftime("%Y-%m-%d %H:%M", time.localtime(creator.indexed_at))
                       if creator.indexed_at else "not yet")
            for column, text in enumerate(
                    (creator.name or creator.user_id, creator.service, images,
                     str(creator.post_count or ""), indexed)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, (creator.service, creator.user_id))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        total = sum(c.image_count - c.unfingerprinted for c in self._creators)
        self.setWindowTitle(f"Pawchive Index - {len(self._creators)} artist(s), "
                            f"{total:,} image(s) searchable")

    def _selected_creators(self) -> List[tuple]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
        return [(self._creators[r].service, self._creators[r].user_id) for r in rows]

    def _select_creators(self, keys) -> None:
        """Selects every one of them. Not selectRow(), which REPLACES the
        selection - adding several artists left only the last selected, so
        "Index selected" indexed one of them."""
        from PyQt6.QtCore import QItemSelectionModel
        keys = set(keys)
        self.table.clearSelection()
        model = self.table.selectionModel()
        for row, creator in enumerate(self._creators):
            if (creator.service, creator.user_id) in keys:
                model.select(self.table.model().index(row, 0),  # type: ignore[union-attr]  # PyQt6 stub: return type is Optional but guaranteed set at this call site
                             QItemSelectionModel.SelectionFlag.Select
                             | QItemSelectionModel.SelectionFlag.Rows)

    # -- adding ---------------------------------------------------------
    def _find_or_add(self):
        text = self.query.text().strip()
        if not text:
            return
        creator = creator_from_url(text)
        if creator:
            added = self.index.add_creator(*creator)
            self._populate()
            self._select_creators([creator])
            self.query.clear()
            self.status.setText(
                f"Added {creator[1]} ({creator[0]}) - click Index selected to index them."
                if added else "That artist is already in the list.")
            return
        if self._finder and self._finder.isRunning():
            return
        self.find_button.setEnabled(False)
        self.status.setText(f"Searching pawchive's artists for \"{text}\"…")
        self._finder = PawchiveFindWorker(text, self._cache_path, self)
        self._finder.done.connect(self._on_found)
        self._finder.start()

    def _on_found(self, found, error):
        self.find_button.setEnabled(True)
        if error:
            self.status.setText(f"Could not search pawchive's artists: {error}")
            return
        self._results = found or []
        self.results.clear()
        for creator in self._results:
            item = QListWidgetItem(
                f"{creator.get('name')}  ({creator.get('service')})  -  "
                f"{creator.get('favorited') or 0:,} favourites")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole,
                         (creator.get("service"), str(creator.get("id")), creator.get("name")))
            self.results.addItem(item)
        shown = bool(self._results)
        self.results.setVisible(shown)
        self.add_checked_button.setVisible(shown)
        self.status.setText(f"{len(self._results)} artist(s) found - tick the ones to add."
                            if shown else "No pawchive artist by that name.")

    def _add_checked(self):
        added = []
        for i in range(self.results.count()):
            item = self.results.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                service, user_id, name = item.data(Qt.ItemDataRole.UserRole)
                if self.index.add_creator(service, user_id, name):
                    added.append((service, user_id))
        self.results.setVisible(False)
        self.add_checked_button.setVisible(False)
        self._populate()
        self._select_creators(added)
        self.status.setText(f"Added {len(added)} artist(s) - click Index selected to index them."
                            if added else "Nothing new was added.")

    def _add_from_matches(self):
        found = set()
        for entry in self._entries() or []:
            for candidate in getattr(entry, "candidates", None) or []:
                creator = creator_from_url(candidate.url or "")
                if creator:
                    found.add(creator)
        added = [c for c in sorted(found) if self.index.add_creator(*c)]
        self._populate()
        self._select_creators(added)
        if not found:
            self.status.setText("No pawchive matches in the current list yet - searching with "
                                "the Pawchive lookup on finds them.")
        else:
            self.status.setText(f"{len(found)} artist(s) have pawchive matches; {len(added)} "
                                "were new and are selected - click Index selected.")

    def _remove_selected(self):
        chosen = self._selected_creators()
        if not chosen:
            return
        if message.question(
                self, "Remove artists",
                f"Remove {len(chosen)} artist(s) and everything indexed for them?",
                default=QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for service, user_id in chosen:
            self.index.remove_creator(service, user_id)
        self._populate()
        self.status.setText(f"Removed {len(chosen)} artist(s).")

    # -- indexing -------------------------------------------------------
    def _start_indexing(self, selected_only: bool):
        if self._worker and self._worker.isRunning():
            return
        jobs = self._selected_creators() if selected_only else [
            (c.service, c.user_id) for c in self._creators]
        if not jobs:
            self.status.setText("Select an artist first." if selected_only
                                else "Add an artist first.")
            return
        self._summary: list[str] = []
        self._set_busy(True)
        self._worker = PawchiveIndexWorker(jobs, self._index_path, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.creator_done.connect(self._on_creator_done)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _set_busy(self, busy: bool):
        for button in (self.index_selected_button, self.index_all_button, self.remove_button,
                       self.from_matches_button, self.find_button, self.add_checked_button):
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)

    def _on_progress(self, text: str, done: int, total: int):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.status.setText(f"{text}: {done:,} of {total:,}")
        else:
            self.progress.setRange(0, 0)
            self.status.setText(f"{text}…" + (f" ({done:,} so far)" if done else ""))

    def _on_creator_done(self, result):
        who = result.name or result.user_id
        if result.error:
            self._summary.append(f"{who}: {result.error}")
        else:
            line = f"{who}: {result.images:,} image(s), {result.new:,} new"
            if result.unfingerprinted:
                line += f", {result.unfingerprinted} without a thumbnail"
            if result.stopped:
                line += " - stopped"
            self._summary.append(line)
        self._populate()

    def _on_finished(self):
        self._set_busy(False)
        self.status.setText("<br>".join(self._summary) or "Nothing was indexed.")

    def _stop(self):
        if self._worker and self._worker.isRunning():
            self._worker.request_stop()
            self.stop_button.setEnabled(False)
            self.status.setText("Stopping after the current request…")

    def closeEvent(self, event):
        # A QThread destroyed while running aborts the process, so stop and
        # wait - the worker checks for a stop several times a second.
        for worker in (self._worker, self._finder):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "request_stop"):
                    worker.request_stop()
                worker.wait(15000)
        super().closeEvent(event)
