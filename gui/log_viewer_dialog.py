from __future__ import annotations

from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtCore import QUrl

from core.applog import get_log_file_path, get_recent_logs
from core.crashlog import get_crash_log_path, get_recent_crash_log
from gui import widgets


class LogViewerDialog(QDialog):
    """Files > View Logs: shows recent log lines (network requests, errors,
    search results) so failures like a rejected Hydrus upload can be
    diagnosed without a terminal.

    Also the home of crash.log (DAN-485): a hard crash - a segfault, or a
    QThread destroyed mid-run - never gets as far as writing to app.log, so
    that diagnosis lives in a separate file this dialog can switch to."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.resize(760, 480)
        self._showing_crash_log = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.PAGE_SPACING)
        self.path_label = QLabel()
        layout.addWidget(self.path_label)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self.text.font()
        font.setFamily("monospace")
        self.text.setFont(font)
        layout.addWidget(self.text)

        btn_row = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        btn_row.addWidget(refresh_btn)

        copy_btn = QPushButton("Copy all")
        copy_btn.clicked.connect(self._copy_all)
        btn_row.addWidget(copy_btn)

        open_btn = QPushButton("Open log file location")
        open_btn.clicked.connect(self._open_log_folder)
        btn_row.addWidget(open_btn)

        self.crash_log_btn = QPushButton("View Crash Log")
        self.crash_log_btn.clicked.connect(self._toggle_crash_log)
        btn_row.addWidget(self.crash_log_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

        self.refresh()

    def refresh(self):
        if self._showing_crash_log:
            self.path_label.setText(f"Crash log: {get_crash_log_path()}")
            logs = get_recent_crash_log()
            self.text.setPlainText(logs or "(no crash log yet - nothing has crashed)")
        else:
            self.path_label.setText(f"Log file: {get_log_file_path()}")
            logs = get_recent_logs()
            self.text.setPlainText(logs or "(no log entries yet)")
        # Scroll to the bottom so the most recent entries are visible.
        cursor = self.text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.text.setTextCursor(cursor)

    def _toggle_crash_log(self):
        self._showing_crash_log = not self._showing_crash_log
        self.crash_log_btn.setText("View App Log" if self._showing_crash_log else "View Crash Log")
        self.refresh()

    def _copy_all(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    def _open_log_folder(self):
        from pathlib import Path
        path = get_crash_log_path() if self._showing_crash_log else get_log_file_path()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
