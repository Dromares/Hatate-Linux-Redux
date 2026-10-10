"""The interrupted-run banner (DAN-1166 / P12a, gap rows S-01, S-02, R-06).

DAN-660's banner was a one-line count with the actions to its right. It now
wears the mockup's frame and voice: bracketed, a kicker, reassuring prose
with counts, a meta line of what is on disk, the actions beneath - with the
action hierarchy untouched - and the row that was in flight says so in its
Engine cell while the run strip says how long ago the run stopped. Each
claim is measured on a real, themed MainWindow at 1440x900 in both modes.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from core import crashlog
from core.models import ImageEntry, MatchStatus
from gui import theme, widgets
from gui.image_table_model import COL_ENGINE, INTERRUPTED_MARKER
from tests.test_flat_surfaces import MODES, _near, _rgb
from tests.test_ghost_kanji import _ink_over
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _contrast_ratio, _parse_hex

HOUR = 3600
AUTOSAVE_AGE = 2 * HOUR + 9 * 60
STOPPED_AGE = 2 * HOUR + 14 * 60
CLEAN_EXIT_AGE = 3 * 86400 + 4 * HOUR


def _log_line(epoch, text):
    stamp = time.strftime(crashlog._LOG_STAMP_FORMAT, time.localtime(epoch))
    return f"{stamp} [INFO] hatate.x: {text}\n"


class TestPreviousRunStoppedAt(unittest.TestCase):
    """`stopped Xh Ym ago`: the crashed run's last line in app.log."""

    def _stopped_at(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.log"
            path.write_text(text, encoding="utf-8")
            return crashlog.previous_run_stopped_at(path)

    def test_it_is_the_last_line_before_this_runs_start(self):
        base = 1_700_000_000
        text = (_log_line(base, "Logging started, writing to a")
                + _log_line(base + 60, "searching")
                + _log_line(base + 120, "last thing the crashed run said")
                + _log_line(base + 9000, "Logging started, writing to a")
                + _log_line(base + 9001, "this run"))
        self.assertEqual(self._stopped_at(text), time.mktime(time.localtime(base + 120)))

    def test_continuation_lines_do_not_count(self):
        # A traceback's lines carry no stamp; the stamped line above them does.
        base = 1_700_000_000
        text = (_log_line(base, "Logging started, writing to a")
                + _log_line(base + 30, "boom")
                + "Traceback (most recent call last):\n  File x\n"
                + _log_line(base + 500, "Logging started, writing to a"))
        self.assertEqual(self._stopped_at(text), time.mktime(time.localtime(base + 30)))

    def test_a_first_ever_run_has_no_previous_run(self):
        self.assertIsNone(self._stopped_at(_log_line(1_700_000_000, "Logging started, writing to a")))

    def test_a_log_with_no_start_line_or_no_file_is_unknown(self):
        self.assertIsNone(self._stopped_at(_log_line(1_700_000_000, "something")))
        self.assertIsNone(crashlog.previous_run_stopped_at(Path("/nonexistent/app.log")))


class TestLastCleanExit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in (("RUNNING_MARKER", self.dir / "crash.log.running"),
                            ("CLEAN_EXIT_FILE", self.dir / "last_clean_exit"),
                            ("CONFIG_DIR", self.dir)):
            patcher = patch.object(crashlog, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_clean_shutdown_records_when(self):
        crashlog.mark_running()
        crashlog.mark_clean_shutdown()
        self.assertAlmostEqual(crashlog.last_clean_exit_age_seconds(), 0, delta=5)

    def test_a_crash_does_not_overwrite_it(self):
        (self.dir / "last_clean_exit").write_text(str(time.time() - CLEAN_EXIT_AGE))
        crashlog.mark_running()          # a run that never reaches a clean exit
        self.assertAlmostEqual(crashlog.last_clean_exit_age_seconds(), CLEAN_EXIT_AGE, delta=5)

    def test_none_ever_recorded_is_unknown_not_guessed(self):
        self.assertIsNone(crashlog.last_clean_exit_age_seconds())
        (self.dir / "last_clean_exit").write_text("not a number")
        self.assertIsNone(crashlog.last_clean_exit_age_seconds())


class _InterruptedWindow(GuiTestCase):
    def _window(self, mode, entries=None, *, unclean=True, autosave=AUTOSAVE_AGE,
                stopped=STOPPED_AGE, clean_exit=CLEAN_EXIT_AGE, faults=1):
        from gui import main_window as mw
        from gui.fonts import register_fonts
        app = QApplication.instance() or QApplication([])
        register_fonts()
        app.setStyleSheet(theme.stylesheet(mode))
        self.addCleanup(lambda: app.setStyleSheet(""))
        if entries is None:
            entries = [
                ImageEntry(path="/tmp/p12a-0.png", status=MatchStatus.GOOD,
                           sent_to_hydrus=True, hydrus_import_confirmed=True),
                ImageEntry(path="/tmp/p12a-1.png", status=MatchStatus.POOR),
                ImageEntry(path="/tmp/p12a-2.png", status=MatchStatus.NOT_SEARCHED),
                ImageEntry(path="/tmp/p12a-3.png", status=MatchStatus.SEARCHING),
                ImageEntry(path="/tmp/p12a-4.png", status=MatchStatus.NOT_SEARCHED),
            ]
        stopped_at = None if stopped is None else time.time() - stopped
        with patch.object(mw, "has_saved_session", return_value=True), \
             patch.object(mw, "load_session", return_value=list(entries)), \
             patch.object(mw, "get_quota_pause_state", return_value=None), \
             patch.object(mw.crashlog, "faults_in_previous_run", return_value=faults), \
             patch.object(mw.crashlog, "last_clean_exit_age_seconds", return_value=clean_exit), \
             patch.object(mw.crashlog, "previous_run_stopped_at", return_value=stopped_at), \
             patch.object(mw, "saved_session_age_seconds", return_value=autosave), \
             patch.object(mw.MainWindow, "_start_missing_file_check"), \
             patch.object(mw.MainWindow, "_start_hydrus_reconcile"):
            win = mw.MainWindow(unclean_shutdown=unclean)
        self.addCleanup(win.deleteLater)
        win.action_set_theme(mode)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        for _ in range(3):
            QApplication.processEvents()
        return win

    @staticmethod
    def _label(win, name):
        return win.run_banner.findChild(QLabel, name)


class TestFrameAndKicker(_InterruptedWindow):
    def test_the_banner_wears_the_four_corner_brackets(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                brackets = win.run_banner.findChild(widgets.Brackets)
                self.assertIsNotNone(brackets)
                self.assertEqual(brackets.size(), win.run_banner.size())
                self.assertEqual(len(brackets.corner_rects()), 8)   # four L shapes

    def test_the_kicker_reads_run_interrupted_partial_results_survived(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                head = self._label(self._window(mode), "RunBannerHead")
                head.ensurePolished()
                self.assertEqual(head.text(), "Run interrupted — partial results survived")
                # The capitals are the font's, so it paints as the mockup's kicker.
                self.assertEqual(head.font().capitalization(), QFont.Capitalization.AllUppercase)
                self.assertEqual(head.font().bold(), True)

    def test_the_kicker_is_the_loudest_ink_and_clears_contrast(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                head = self._label(win, "RunBannerHead")
                head.ensurePolished()
                loud = head.palette().color(head.foregroundRole())
                card = _parse_hex(theme.palette(mode)["card"])
                self.assertGreaterEqual(
                    _contrast_ratio((loud.red(), loud.green(), loud.blue()), card), 4.5)


class TestProse(_InterruptedWindow):
    def test_it_reassures_and_carries_the_counts(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                text = self._window(mode)._run_banner_body.text()
                self.assertIn("Hatate did not exit cleanly during this run.", text)
                self.assertIn("Everything up to the last autosave is safe.", text)
                self.assertIn("1 of 5 images are already sent to Hydrus", text)
                self.assertIn("1 is decided and waiting for your review", text)
                # The row that was mid-search is unreached too: 3, not 2.
                self.assertIn("and 3 were never reached.", text)

    def test_the_wrapped_prose_is_not_clipped(self):
        """A wrapped label inside a frame used to be given one line's height
        and drew over the meta line; every line of it must be visible, in
        a window narrow enough to force a third line too."""
        for mode in MODES:
            for width in (1440, 700):
                with self.subTest(mode=mode, width=width):
                    win = self._window(mode)
                    win.resize(width, 900)
                    for _ in range(3):
                        QApplication.processEvents()
                    body = self._label(win, "RunBannerBody")
                    self.assertGreaterEqual(body.height(), body.heightForWidth(body.width()))
                    meta = win.run_banner.findChild(widgets.QWidget, "MetaLine")
                    body_bottom = body.mapTo(win.run_banner, body.rect().bottomLeft()).y()
                    meta_top = meta.mapTo(win.run_banner, meta.rect().topLeft()).y()
                    self.assertLessEqual(body_bottom, meta_top)

    def test_counts_by_category(self):
        from gui.main_window import _interrupted_prose
        entries = [
            ImageEntry(path="a", status=MatchStatus.GOOD, sent_to_hydrus=True,
                       hydrus_import_confirmed=True),
            ImageEntry(path="b", status=MatchStatus.GOOD, sent_to_hydrus=True,
                       hydrus_import_confirmed=True),
            ImageEntry(path="c", status=MatchStatus.GOOD, sent_to_hydrus=True),
            ImageEntry(path="d", status=MatchStatus.GOOD),
            ImageEntry(path="e", status=MatchStatus.POOR),
            ImageEntry(path="f", status=MatchStatus.POOR, reviewed=True),
            ImageEntry(path="g", status=MatchStatus.NOT_SEARCHED),
        ]
        text = _interrupted_prose(entries)
        self.assertIn("2 of 7 images are already sent to Hydrus", text)
        self.assertIn("1 is still queued with Hydrus", text)
        self.assertIn("2 are decided and waiting for your review", text)
        self.assertIn("and 1 was never reached.", text)

    def test_nothing_queued_is_not_announced(self):
        from gui.main_window import _interrupted_prose
        text = _interrupted_prose([ImageEntry(path="a", status=MatchStatus.NOT_SEARCHED)])
        self.assertNotIn("queued", text)


class TestMetaLine(_InterruptedWindow):
    def _meta(self, win):
        labels = win.run_banner.findChildren(QLabel, "MetaLabel")
        figures = win.run_banner.findChildren(QLabel, "MetaFigure")
        return [(a.text(), b.text()) for a, b in zip(labels, figures, strict=True)]

    def test_it_reads_autosave_clean_exit_and_crash_log(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.assertEqual(
                    self._meta(self._window(mode)),
                    [("Last autosave", "2h 9m ago"),
                     ("Last clean exit", "3d 4h ago"),
                     ("Crash log", "1 new entry")])

    def test_it_paints_as_the_mockups_uppercase_mono_line(self):
        win = self._window("dark")
        for label in win.run_banner.findChildren(QLabel, "MetaLabel"):
            label.ensurePolished()
            self.assertEqual(label.font().capitalization(), QFont.Capitalization.AllUppercase)

    def test_it_sits_flush_left_under_the_prose(self):
        win = self._window("dark")
        body = self._label(win, "RunBannerBody")
        first = win.run_banner.findChildren(QLabel, "MetaLabel")[0]
        body_x = body.mapTo(win.run_banner, body.rect().topLeft()).x()
        first_x = first.mapTo(win.run_banner, first.rect().topLeft()).x()
        self.assertEqual(first_x, body_x)

    def test_an_unknown_clean_exit_or_autosave_is_left_out_not_invented(self):
        win = self._window("dark", clean_exit=None, autosave=None, faults=0)
        self.assertEqual(self._meta(win), [("Crash log", "no new entry")])

    def test_two_faults_pluralise(self):
        win = self._window("dark", faults=2)
        self.assertIn(("Crash log", "2 new entries"), self._meta(win))


class TestActionsUnchanged(_InterruptedWindow):
    """The hierarchy DAN-660 got right is not to move: Resume primary, View
    Crash Log secondary, Discard the quietest thing on the banner."""

    def test_resume_is_the_one_primary_and_view_crash_log_is_not(self):
        win = self._window("dark")
        buttons = win.run_banner.findChildren(QPushButton)
        self.assertEqual([b.text() for b in buttons], ["Resume Queue", "View Crash Log"])
        self.assertEqual([b.objectName() for b in buttons], ["Primary", ""])

    def test_discard_is_still_a_link_not_a_button(self):
        win = self._window("dark")
        discard = win.run_banner.findChild(widgets.LinkLabel)
        self.assertIsNotNone(discard)
        self.assertEqual(discard.objectName(), "RunBannerDiscard")
        self.assertEqual(len(win.run_banner.findChildren(QPushButton)), 2)

    def test_the_actions_sit_below_the_prose_in_one_row_discard_last(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                frame = win.run_banner

                def top_left(widget, frame=frame):
                    return widget.mapTo(frame, widget.rect().topLeft())

                resume, crash_log = frame.findChildren(QPushButton)
                discard = frame.findChild(widgets.LinkLabel)
                body = self._label(win, "RunBannerBody")
                meta = frame.findChild(widgets.QWidget, "MetaLine")
                self.assertGreater(top_left(resume).y(), top_left(meta).y())
                self.assertGreater(top_left(meta).y(), top_left(body).y())
                self.assertLess(abs(top_left(resume).y() - top_left(crash_log).y()), 2)
                self.assertLess(top_left(resume).x(), top_left(crash_log).x())
                self.assertLess(top_left(crash_log).x(), top_left(discard).x())

    def test_discard_is_not_louder_than_it_was(self):
        """Measured on pixels, since a QSS colour is not on the label's
        palette: the most strongly inked pixel of the link's text is the
        body-safe dim tier it has always been, never the loud one."""
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                discard = win.run_banner.findChild(widgets.LinkLabel)
                # Grabbed out of the frame, so the card fill is under the text.
                rect = discard.rect().translated(discard.mapTo(win.run_banner, discard.rect().topLeft()))
                img = win.run_banner.grab(rect).toImage()
                card = _parse_hex(theme.palette(mode)["card"])
                pixels = [_rgb(img, x, y) for x in range(img.width()) for y in range(img.height())]
                loudest = max(pixels, key=lambda p: sum(abs(a - b) for a, b in zip(p, card, strict=True)))
                quiet = _ink_over(mode, "ink_65", over="card")
                self.assertTrue(_near(loudest, quiet, tol=12), f"{mode}: {loudest} vs {quiet}")


class TestRowMarker(_InterruptedWindow):
    def _engine_cells(self, win):
        model = win.table_model
        return [(model.entry_at(r).filename, model.index(r, COL_ENGINE))
                for r in range(model.rowCount())]

    def test_the_restore_flags_the_row_that_was_in_flight_and_resets_it(self):
        win = self._window("dark")
        flagged = [e for e in win.entries if e.interrupted_mid_search]
        self.assertEqual([e.filename for e in flagged], ["p12a-3.png"])
        # Unsearched, so Resume Queue (which takes only those) picks it up.
        self.assertEqual(flagged[0].status, MatchStatus.NOT_SEARCHED)
        self.assertFalse(any(e.status == MatchStatus.SEARCHING for e in win.entries))

    def test_a_clean_restore_leaves_a_stale_row_alone(self):
        win = self._window("dark", unclean=False)
        self.assertFalse(any(e.interrupted_mid_search for e in win.entries))

    def test_only_that_rows_engine_cell_says_so_in_italic(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                for name, index in self._engine_cells(win):
                    shown = index.data(Qt.ItemDataRole.DisplayRole)
                    font = index.data(Qt.ItemDataRole.FontRole)
                    if name == "p12a-3.png":
                        self.assertEqual(shown, "— interrupted mid-search")
                        self.assertEqual(shown, INTERRUPTED_MARKER)
                        self.assertTrue(font.italic())
                    else:
                        self.assertEqual(shown, "", name)
                        self.assertIsNone(font, name)

    def test_the_marker_is_dim_but_clears_the_text_floor(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                cell = dict(self._engine_cells(win))["p12a-3.png"]
                colour = cell.data(Qt.ItemDataRole.ForegroundRole)
                got = (colour.red(), colour.green(), colour.blue())
                card = _parse_hex(theme.palette(mode)["card"])
                self.assertGreaterEqual(_contrast_ratio(got, card), 4.5)

    def test_the_marker_breaks_at_the_space_and_fits_the_row_in_the_default_column(self):
        """The default Engine column is wide enough that the marker wraps to
        two lines at its space ("— interrupted" / "mid-search") rather than
        at its hyphen or into three, and two lines fit the row."""
        from PyQt6.QtCore import QRect
        from PyQt6.QtGui import QFontMetrics
        from gui import main_window as mw
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                # The constant, not the live column: a saved header layout
                # on the machine running the tests would otherwise decide it.
                self.assertEqual(mw.ENGINE_COLUMN_WIDTH, 120)
                cell = dict(self._engine_cells(win))["p12a-3.png"]
                font = win.table.font()
                font.setItalic(True)
                metrics = QFontMetrics(font)
                inner = mw.ENGINE_COLUMN_WIDTH - 16     # the view's cell margins
                lines = metrics.boundingRect(
                    QRect(0, 0, inner, 1000), int(Qt.TextFlag.TextWordWrap),
                    cell.data(Qt.ItemDataRole.DisplayRole))
                self.assertLessEqual(lines.height(), 2 * metrics.lineSpacing() + 1)
                self.assertLessEqual(lines.height(), win.table.verticalHeader().defaultSectionSize())
                self.assertGreater(metrics.horizontalAdvance("\u2014 interrupted mid-"), inner)

    def test_it_goes_once_the_row_has_a_result_or_is_searched_again(self):
        win = self._window("dark")
        entry = next(e for e in win.entries if e.interrupted_mid_search)
        cell = dict(self._engine_cells(win))["p12a-3.png"]
        entry.status = MatchStatus.NOT_FOUND
        self.assertEqual(cell.data(Qt.ItemDataRole.DisplayRole), "")

    def test_the_flag_is_not_saved(self):
        from core.session import _entry_to_dict
        entry = ImageEntry(path="/tmp/p12a-saved.png", interrupted_mid_search=True)
        self.assertNotIn("interrupted_mid_search", _entry_to_dict(entry))

    def test_flagging_does_not_make_an_entry_look_changed(self):
        entry = ImageEntry(path="/tmp/p12a-rev.png")
        before = entry.revision
        entry.interrupted_mid_search = True
        self.assertEqual(entry.revision, before)

    def test_the_search_worker_spends_the_flag(self):
        import inspect
        from workers import search_worker
        source = inspect.getsource(search_worker)
        spent = source.index("entry.interrupted_mid_search = False")
        self.assertLess(spent, source.index("entry.status = MatchStatus.SEARCHING"))


class TestStripNote(_InterruptedWindow):
    def test_the_strip_says_how_long_ago_it_stopped(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self.assertEqual(win.run_note_label.text(), "stopped 2h 14m ago")

    def test_the_figure_is_the_bold_part(self):
        win = self._window("dark")
        figures = [p.text() for p in win.run_note_label.findChildren(QLabel, "RunFigure")]
        self.assertIn("2h", figures)

    def test_with_no_log_to_read_the_autosave_is_the_fallback(self):
        win = self._window("dark", stopped=None)
        self.assertEqual(win.run_note_label.text(), "stopped 2h 9m ago")

    def test_nothing_known_says_nothing(self):
        win = self._window("dark", stopped=None, autosave=None)
        self.assertEqual(win.run_note_label.text(), "")

    def test_a_clean_restore_has_no_note(self):
        win = self._window("dark", unclean=False)
        self.assertEqual(win.run_note_label.text(), "")

    def test_discarding_the_session_clears_it(self):
        from gui import main_window as mw
        win = self._window("dark")
        with patch.object(mw.message, "question",
                          return_value=mw.QMessageBox.StandardButton.Yes), \
             patch.object(mw, "clear_session"):
            win.action_clear_session()
        self.assertEqual(win.run_note_label.text(), "")

    def test_starting_a_search_clears_it(self):
        from gui import main_window as mw
        win = self._window("dark")
        self.assertEqual(win.run_note_label.text(), "stopped 2h 14m ago")
        with patch.object(mw, "SearchWorker") as worker_class:
            worker_class.return_value = worker_class
            try:
                win._launch_search_worker(list(win.entries))
            except Exception:
                pass   # past the line under test the launch needs a live worker
        self.assertEqual(win.run_note_label.text(), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
