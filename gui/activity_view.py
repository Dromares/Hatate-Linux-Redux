"""Activity: what the unattended half of the app is doing.

A search runs for hours - 45 to 75 seconds an image by default, over a
list that can be tens of thousands long. You start it and walk away. The
question that half of the app has to be able to answer is "is it still
working, and is it working properly", and until now the answer lived in
two dialogs and a log file: Help > View Logs for what happened, Help >
Parser Health for whether the site parsers still work.

Both of those are modal, which is the wrong shape for something you want
to glance at. So the log is a live pane here, tailing while the run goes,
and the engine line-up is stated rather than inferred from memory of what
Settings says.

This page deliberately does NOT repeat the run strip. Progress, the
estimate, the countdown, the quota and the sent count are in the strip
above, visible from every mode - putting a second copy here would be two
places to keep in step and one of them wrong. What is here is what the
strip has no room for.
"""
from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget,
)

from core import engines
from core.engines import pipeline_summary
from core.applog import get_log_file_path, get_recent_logs
from gui import widgets

# While the page is on screen the log tails itself. Off it, the timer is
# stopped: re-reading a file every couple of seconds to paint a widget
# nobody is looking at is a silly thing to do for hours.
TAIL_INTERVAL_MS = 2000


class ActivityViewMixin:
    """The Activity mode. A mixin, in MUR's manner - it assigns to `self`."""

    def _build_activity_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(widgets.PAGE_SPACING)

        layout.addWidget(self._build_engines_card())
        layout.addWidget(self._build_log_card(), 1)

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(TAIL_INTERVAL_MS)
        self._log_timer.timeout.connect(self._tail_log)
        return page

    # ------------------------------------------------------------------
    # Engines
    # ------------------------------------------------------------------
    def _build_engines_card(self):
        card, layout = widgets.card(framed=False)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(widgets.section_header("01 // Engines"))

        self.engine_pipeline = widgets.hint("")
        self.engine_pipeline.setWordWrap(True)
        layout.addWidget(self.engine_pipeline)

        self.engine_chip_row = QHBoxLayout()
        self.engine_chip_row.setSpacing(8)
        layout.addLayout(self.engine_chip_row)

        self.engine_summary = widgets.hint("")
        layout.addWidget(self.engine_summary)

        row = QHBoxLayout()
        row.addWidget(widgets.pill_button("Parser health…", self.action_parser_health))
        row.addWidget(widgets.pill_button(
            "Clear search cache…", self.action_clear_search_cache))
        row.addStretch(1)
        row.addWidget(widgets.icon_button(
            "⮐", "Switch to Queue and focus the list.",
            self.action_show_in_queue,
        ))
        layout.addLayout(row)

        self._engine_chips = {}
        self.refresh_engines()
        return card

    def _engine_state(self, engine_id):
        """(on, note) for one engine, read from the live settings."""
        s = self.settings
        if engine_id in engines.PRIMARY_ENGINES:
            primary = engines.effective_primary(s.primary_engine)
            if engine_id == primary:
                return True, "primary"
            mode = s.secondary_engine_mode
            if mode == "disabled":
                return False, "off"
            return True, "always" if mode == "always" else "fallback"

        if engine_id == engines.PAWCHIVE:
            # Never held back - see search_engine._engine_waves.
            return (True, "on") if s.enable_pawchive else (False, "off")
        if engine_id == engines.PAWCHIVE_INDEX:
            return (True, "on") if s.enable_pawchive_index else (False, "off")

        enabled = {
            engines.ASCII2D: s.enable_ascii2d,
            engines.TRACEMOE: s.enable_tracemoe,
            engines.IQDB3D: s.enable_iqdb3d,
            engines.GOOGLE_IMAGES: s.enable_google_images,
            engines.GOOGLE_LENS: s.enable_google_lens,
            engines.YANDEX: s.enable_yandex,
        }.get(engine_id, False)
        if not enabled:
            return False, "off"
        return True, "fallback" if s.extras_only_as_fallback else "on"

    def refresh_engines(self):
        """Restates the line-up. Called on build and after Settings closes."""
        row = getattr(self, "engine_chip_row", None)
        if row is None:
            return
        while row.count():
            item = row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for engine_id in engines.ALL_ENGINES:
            on, note = self._engine_state(engine_id)
            chip = widgets.chip(
                f"{engines.label_for(engine_id)} · {note}",
                "EngineChipOn" if on else "EngineChipOff",
            )
            chip.setToolTip(
                "Runs on every image." if note in ("primary", "always", "on") else
                "Held back for the fallback wave - only run when the engines before it "
                "found nothing good enough." if note == "fallback" else
                "Switched off in Settings > Engine."
            )
            row.addWidget(chip)
        row.addStretch(1)

        # Update the pipeline summary
        self.engine_pipeline.setText(f"Pipeline: {pipeline_summary(self.settings)}")

        s = self.settings
        low, high = s.delay_min_seconds, s.delay_max_seconds
        # A range whose ends are equal is not a range. Reading "paced at
        # 5-5s" invites a second look to check it is not a bug.
        pace = (f"{low:.0f}s" if abs(high - low) < 0.5
                else f"{low:.0f}–{high:.0f}s")
        cache = (f"Cached results are reused for {s.search_cache_ttl_days:.0f} days."
                 if s.use_search_cache else "The search cache is off.")
        self.engine_summary.setText(f"Paced at {pace} an image.  {cache}")

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------
    def _build_log_card(self):
        card, layout = widgets.card(framed=False)
        layout.setContentsMargins(0, 0, 0, 0)

        self.log_follow_btn = widgets.icon_button(
            "⟳", "Keep the log up to date while this page is open.",
            self._on_follow_toggled, checkable=True,
        )
        self.log_follow_btn.setChecked(True)
        layout.addLayout(widgets.section_header(
            "02 // Log",
            self.log_follow_btn,
            widgets.icon_button("⧉", "Copy the whole log.", self._copy_log),
            widgets.icon_button(
                "↗", "Open the folder the log file is in.", self._open_log_folder),
        ))

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")   # the theme gives this one a monospace face
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view, 1)

        layout.addWidget(widgets.muted(f"Writing to {get_log_file_path()}"))
        self._tail_log()
        return card

    def _tail_log(self):
        """Re-reads the log, keeping the view where the reader put it.

        Jumping to the bottom on every tick would make the pane unusable
        for its actual purpose - scrolling back to find where something
        went wrong - so it only follows when already at the bottom.
        """
        view = getattr(self, "log_view", None)
        if view is None:
            return
        text = get_recent_logs() or "(no log entries yet)"
        if text == view.toPlainText():
            return

        bar = view.verticalScrollBar()
        was_at_bottom = bar.value() >= bar.maximum() - 4
        position = bar.value()

        view.setPlainText(text)
        bar.setValue(bar.maximum() if was_at_bottom else min(position, bar.maximum()))

    def _on_follow_toggled(self):
        if self.log_follow_btn.isChecked():
            self._tail_log()
            if self.current_mode() == "activity":
                self._log_timer.start()
        else:
            self._log_timer.stop()

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())

    def _open_log_folder(self):
        from pathlib import Path

        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(Path(get_log_file_path()).parent)))

    # ------------------------------------------------------------------
    def set_activity_live(self, live):
        """Starts or stops the tail as the page comes and goes."""
        timer = getattr(self, "_log_timer", None)
        if timer is None:
            return
        if live and self.log_follow_btn.isChecked():
            self._tail_log()
            self.refresh_engines()
            timer.start()
        else:
            timer.stop()
