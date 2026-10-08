"""Shows whether each site's parser is actually working.

A parser that stops working is silent: the match still appears with its
URL, the "no tags extracted" line goes to a log nobody opens, and the
only symptom is that one site's results look thin. Three parsers were in
that state at once and it took a person noticing to find out.

This window answers the question directly, two ways:

  * what this session has seen - counts of what every site produced,
    gathered as searches ran, costing nothing.
  * a live check - one known post per site, fetched now, asserting the
    parser still gets tags, dimensions and a picture out of it. That is
    the audit that found the three broken parsers, made repeatable.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialogButtonBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from core.applog import get_logger
from core import parser_health, parser_probe
from gui import theme, widgets

log = get_logger("gui.parser_health")

# ink_65: confirmed-working, the same tier Status's "good"/"sent" chips
# use. ink_100: broken, the "needs a decision" tier Status's
# "error"/"not_found" chips use - a parser that stopped working is
# exactly that, not a softer note. (A third WARN_COLOUR used to live
# here; it had no caller - this dialog only ever renders ok/not-ok.)


class _ProbeWorker(QThread):
    """Runs the live check off the GUI thread - it is a real request to
    each of a dozen sites and takes several seconds."""

    progress = pyqtSignal(int, int, str)
    done = pyqtSignal(object)

    def __init__(self, timeout: float, parent=None):
        super().__init__(parent)
        self.timeout = timeout

    def run(self):
        results: List[parser_probe.ProbeResult] = []
        try:
            results = parser_probe.run(
                timeout=self.timeout,
                on_progress=lambda done, total, site: self.progress.emit(done, total, site),
            )
        except Exception as exc:
            # A diagnostic window must not be able to take the app down.
            log.exception("Parser check failed: %s", exc)
        self.done.emit(results)


class ParserHealthDialog(QDialog):
    COLUMNS = ["Site", "This session", "Live check", "Last problem"]

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Parser Health")
        self.resize(760, 460)
        self._probe: Optional[_ProbeWorker] = None
        self._results: dict = {}
        self._mode = theme.resolve_mode(settings.theme)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        layout.addWidget(widgets.heading("Parser Health"))
        layout.addWidget(QLabel(
            "Whether each site's tag parsing still works.\n"
            "A parser that breaks does so silently - matches keep appearing, just without "
            "tags - so this is the place to check when one site's results look thin."
        ))

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            len(self.COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, stretch=1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.check_button = QPushButton("Check every site now")
        self.check_button.setToolTip(
            "Fetches one known post from each site and checks the parser still reads it.\n"
            "This makes real requests to a dozen sites, so it takes a few seconds - and a "
            "site being down will show here as a failure, which is worth knowing too."
        )
        self.check_button.clicked.connect(self._start_probe)
        row.addWidget(self.check_button)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row.addWidget(buttons)
        layout.addLayout(row)

        self._populate()

    # -- rendering ------------------------------------------------------
    def _populate(self):
        health = {h.site: h for h in parser_health.snapshot()}
        sites = sorted(set(health) | set(self._results)
                       | {site for site, _ in parser_probe.PROBES})
        self.table.setRowCount(len(sites))
        for row, site in enumerate(sites):
            self.table.setItem(row, 0, QTableWidgetItem(site))

            entry = health.get(site)
            session_item = QTableWidgetItem(entry.summary() if entry else "not used yet")
            if entry and entry.suspect:
                session_item.setForeground(theme.ink_color(self._mode, "ink_100"))
            elif entry and entry.ok:
                session_item.setForeground(theme.ink_color(self._mode, "ink_65"))
            self.table.setItem(row, 1, session_item)

            result = self._results.get(site)
            if result is None:
                live_item = QTableWidgetItem("—")
            else:
                live_item = QTableWidgetItem(result.summary())
                live_item.setForeground(
                    theme.ink_color(self._mode, "ink_65" if result.ok else "ink_100")
                )
            self.table.setItem(row, 2, live_item)

            problem = ""
            if result is not None and not result.ok:
                problem = result.problem or ""
            elif entry and entry.last_problem:
                problem = entry.last_problem
            self.table.setItem(row, 3, QTableWidgetItem(problem))
        self.table.resizeColumnsToContents()

    # -- the live check -------------------------------------------------
    def _start_probe(self):
        if self._probe and self._probe.isRunning():
            return
        self.check_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setMaximum(len(parser_probe.PROBES))
        self.progress.setValue(0)
        self.status.setText("Checking…")
        self._probe = _ProbeWorker(self.settings.search_timeout, self)
        self._probe.progress.connect(self._on_progress)
        self._probe.done.connect(self._on_done)
        self._probe.start()

    def _on_progress(self, done: int, total: int, site: str):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        if site:
            self.status.setText(f"Checking {site}…")

    def _on_done(self, results):
        self.check_button.setEnabled(True)
        self.progress.setVisible(False)
        results = results or []
        self._results = {r.site: r for r in results}
        self._populate()

        broken = [r for r in results if not r.ok]
        if not results:
            self.status.setText("The check could not run - see Help > View Logs.")
        elif broken:
            bad = theme.ink_color(self._mode, "ink_100").name()
            self.status.setText(
                f"<b style='color:{bad}'>{len(broken)} of {len(results)} sites have a "
                f"problem:</b> " + ", ".join(r.site for r in broken) +
                ". A site being down looks the same as a broken parser here, so it is worth "
                "opening one of those posts in a browser before concluding anything."
            )
        else:
            ok = theme.ink_color(self._mode, "ink_65").name()
            self.status.setText(
                f"<b style='color:{ok}'>All {len(results)} sites read correctly.</b>"
            )

    def closeEvent(self, event):
        # Never leave the worker running past the window - a QThread
        # destroyed while running aborts the process.
        if self._probe and self._probe.isRunning():
            self._probe.wait(15000)
        super().closeEvent(event)
