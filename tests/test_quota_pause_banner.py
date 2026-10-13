"""The quota-paused banner (DAN-1167 / P12b, gap rows S-03, S-04, R-06 paused).

SauceNAO's daily quota running out used to raise a modal `QMessageBox`
(and, on relaunch, only a status-bar line). Ruling C-4 on DAN-1155 makes
the in-page banner replace the modal: a run left alone for hours must not
sit waiting on a click. Each claim is measured on a real, themed
MainWindow at 1440x900 in both modes.
"""
import datetime
import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QPushButton

from core.models import ImageEntry, MatchStatus
from core.saucenao import QuotaPauseState, SauceNaoQuota
from gui import theme, widgets
from tests.test_flat_surfaces import MODES
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _contrast_ratio, _parse_hex

HEAD = "Run paused \u2014 SauceNAO's daily quota is exhausted"


def _entries():
    """3 sent, 2 decided and waiting, 1 reviewed (not waiting), 4 untouched."""
    sent = [ImageEntry(path=f"/tmp/p12b-s{i}.png", status=MatchStatus.GOOD,
                       sent_to_hydrus=True, hydrus_import_confirmed=True) for i in range(3)]
    waiting = [ImageEntry(path="/tmp/p12b-w0.png", status=MatchStatus.POOR),
               ImageEntry(path="/tmp/p12b-w1.png", status=MatchStatus.GOOD)]
    done = [ImageEntry(path="/tmp/p12b-r.png", status=MatchStatus.GOOD, reviewed=True)]
    untouched = [ImageEntry(path=f"/tmp/p12b-u{i}.png", status=MatchStatus.NOT_SEARCHED)
                 for i in range(4)]
    return sent + waiting + done + untouched


def _state(remaining=4, hours=3):
    reset = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    return QuotaPauseState(paused_at=time.time() - 60, searched=6, remaining=remaining,
                           reset_at=reset.isoformat())


class _PausedWindow(GuiTestCase):
    def _window(self, mode, entries=None, *, state="fresh", persisted=False):
        """A themed window. `persisted` builds it WITH a pause already on
        disk (the relaunch path); otherwise the pause is fired live."""
        from gui import main_window as mw
        from gui.fonts import register_fonts
        app = QApplication.instance() or QApplication([])
        register_fonts()
        app.setStyleSheet(theme.stylesheet(mode))
        self.addCleanup(lambda: app.setStyleSheet(""))
        entries = _entries() if entries is None else entries
        pause = _state() if state == "fresh" else state
        patches = [
            patch.object(mw, "get_quota_pause_state", return_value=pause if persisted else None),
            patch.object(mw, "has_saved_session", return_value=bool(entries)),
            patch.object(mw, "load_session", return_value=list(entries)),
            patch.object(mw.MainWindow, "_start_missing_file_check"),
            patch.object(mw.MainWindow, "_start_hydrus_reconcile"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        win = mw.MainWindow()
        self.addCleanup(win.deleteLater)
        win.action_set_theme(mode)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        if not persisted:
            with patch.object(mw, "get_quota_pause_state", return_value=pause):
                win._on_paused_out_of_quota(6, 4)
        for _ in range(3):
            QApplication.processEvents()
        return win

    @staticmethod
    def _label(win, name):
        return win.quota_banner.findChild(QLabel, name)

    @staticmethod
    def _buttons(win):
        return win.quota_banner.findChildren(QPushButton)


class TestNoModal(_PausedWindow):
    def test_a_quota_pause_raises_no_modal(self):
        """The pause must return without opening any dialog: a blocking
        `exec()` here is a run waiting on a click nobody is there to make."""
        from gui import main_window as mw
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode, persisted=True)
                boom = AssertionError("a quota pause opened a modal")
                with patch.object(QDialog, "exec", side_effect=boom), \
                     patch.object(QMessageBox, "exec", side_effect=boom), \
                     patch.object(mw.message, "build", side_effect=boom), \
                     patch.object(mw.message, "information", side_effect=boom), \
                     patch.object(mw.message, "warning", side_effect=boom), \
                     patch.object(mw.message, "question", side_effect=boom):
                    win._on_paused_out_of_quota(6, 4)
                visible = [w for w in QApplication.topLevelWidgets()
                           if isinstance(w, QMessageBox) and w.isVisible()]
                self.assertEqual(visible, [])
                self.assertTrue(win.quota_banner.isVisibleTo(win))

    def test_a_pause_with_nothing_recorded_still_raises_no_modal(self):
        win = self._window("dark", state=None)
        with patch.object(QDialog, "exec", side_effect=AssertionError("modal")):
            win._on_paused_out_of_quota(6, 4)
        self.assertTrue(win.quota_banner.isVisibleTo(win))


class TestBannerVariant(_PausedWindow):
    def test_the_kicker_reads_run_paused_quota_exhausted(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                head = self._label(self._window(mode), "RunBannerHead")
                head.ensurePolished()
                self.assertEqual(head.text(), HEAD)
                self.assertEqual(head.font().capitalization(), QFont.Capitalization.AllUppercase)

    def test_the_kicker_clears_text_contrast(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                head = self._label(win, "RunBannerHead")
                head.ensurePolished()
                loud = head.palette().color(head.foregroundRole())
                card = _parse_hex(theme.palette(mode)["card"])
                self.assertGreaterEqual(
                    _contrast_ratio((loud.red(), loud.green(), loud.blue()), card), 4.5)

    def test_it_wears_the_same_brackets_as_the_interrupted_banner(self):
        win = self._window("dark")
        brackets = win.quota_banner.findChild(widgets.Brackets)
        self.assertIsNotNone(brackets)
        self.assertEqual(brackets.size(), win.quota_banner.size())
        self.assertEqual(len(brackets.corner_rects()), 8)

    def test_the_prose_carries_the_counts(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                text = self._label(self._window(mode), "RunBannerBody").text()
                self.assertIn("rather than finish the batch with weaker matches", text)
                self.assertIn("3 of 10 images are already sent", text)
                self.assertIn("2 are waiting on your review", text)
                self.assertIn("4 are untouched until you resume", text)
                self.assertIn("Nothing was lost.", text)

    def test_the_prose_is_not_clipped_at_any_width(self):
        for mode in MODES:
            for width in (1440, 700):
                with self.subTest(mode=mode, width=width):
                    win = self._window(mode)
                    win.resize(width, 900)
                    for _ in range(3):
                        QApplication.processEvents()
                    body = self._label(win, "RunBannerBody")
                    self.assertGreaterEqual(body.height(), body.heightForWidth(body.width()))
                    meta = win.quota_banner.findChild(widgets.QWidget, "MetaLine")
                    body_bottom = body.mapTo(win.quota_banner, body.rect().bottomLeft()).y()
                    meta_top = meta.mapTo(win.quota_banner, meta.rect().topLeft()).y()
                    self.assertLessEqual(body_bottom, meta_top)

    def test_the_meta_line_gives_the_time_the_count_and_the_reset(self):
        win = self._window("dark")
        labels = [lbl.text() for lbl in win.quota_banner.findChildren(QLabel, "MetaLabel")]
        figures = [lbl.text() for lbl in win.quota_banner.findChildren(QLabel, "MetaFigure")]
        self.assertEqual(labels, ["Quota exhausted at", "Images waiting", "Resets"])
        self.assertEqual(figures[1], "4")
        self.assertRegex(figures[0], r"^\d\d:\d\d$")
        self.assertRegex(figures[2], r"^\d\d:\d\d UTC$")

    def test_without_a_list_the_prose_falls_back_to_the_workers_count(self):
        win = self._window("dark", entries=[])
        text = self._label(win, "RunBannerBody").text()
        self.assertIn("4 images are untouched until you resume", text)
        self.assertNotIn("already sent", text)


class TestActions(_PausedWindow):
    def test_review_is_the_primary_and_continue_is_the_secondary(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                review, cont = self._buttons(win)
                self.assertEqual(review.text(), "Review the 2 waiting \u2192")
                self.assertEqual(cont.text(), "Continue with other engines")
                self.assertEqual(review.objectName(), "Primary")
                self.assertNotEqual(cont.objectName(), "Primary")
                self.assertLess(review.x(), cont.x())
                # Page-level buttons speak the uppercase mono voice.
                self.assertEqual(review.font().capitalization(), QFont.Capitalization.AllUppercase)

    def test_review_opens_review_on_the_first_waiting_row(self):
        win = self._window("dark")
        win.set_mode("queue")
        win._quota_banner_parts['review_button'].click()
        self.assertEqual(win._current_mode, "review")
        self.assertEqual(win._current_entry().path, "/tmp/p12b-w0.png")

    def test_review_is_hidden_when_nothing_is_waiting(self):
        entries = [e for e in _entries() if not e.needs_review or e.status == MatchStatus.NOT_SEARCHED]
        win = self._window("dark", entries=entries)
        self.assertFalse(win._quota_banner_parts['review_button'].isVisibleTo(win.quota_banner))
        self.assertTrue(win._quota_banner_parts['continue_button'].isVisibleTo(win.quota_banner))

    def test_continue_launches_without_saucenao_and_clears_the_banner(self):
        """Same behaviour the modal's button had (DAN-486): one run, SauceNAO
        skipped, the persisted setting untouched. Starting it retires the
        banner and the strip's pause."""
        from gui import main_window as mw
        win = self._window("dark")
        before = win.settings.continue_without_saucenao_on_quota
        with patch.object(mw, "SearchWorker", MagicMock()) as worker_cls, \
             patch.object(mw, "clear_quota_pause"), patch.object(mw, "reset_daily_limit_flag"):
            win._quota_banner_parts['continue_button'].click()
        self.assertTrue(worker_cls.call_args.kwargs["force_continue_without_saucenao"])
        self.assertEqual(win.settings.continue_without_saucenao_on_quota, before)
        self.assertFalse(win.quota_banner.isVisibleTo(win))
        self.assertEqual(win.run_note_label.text(), "")
        self.assertNotEqual(win.run_eta_label.text(), "ETA paused")

    def test_clearing_the_list_retires_the_banner(self):
        from gui import main_window as mw
        win = self._window("dark")
        with patch.object(mw.message, "question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(mw, "clear_session", return_value=True):
            win.action_clear_session()
        self.assertFalse(win.quota_banner.isVisibleTo(win))
        self.assertEqual(win.run_note_label.text(), "")


class TestPersistedAcrossRestart(_PausedWindow):
    def test_a_pause_on_disk_shows_the_banner_not_just_a_status_line(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode, persisted=True)
                self.assertTrue(win.quota_banner.isVisibleTo(win))
                self.assertEqual(self._label(win, "RunBannerHead").text(), HEAD)
                self.assertIn("2 are waiting on your review", self._label(win, "RunBannerBody").text())
                # The status-bar echo stays (G-06).
                self.assertIn("6 searched", win.status_label.text())

    def test_a_stale_pause_shows_no_banner(self):
        stale = _state(hours=-1)
        win = self._window("dark", state=stale, persisted=True)
        self.assertFalse(win.quota_banner.isVisibleTo(win))
        self.assertEqual(win.run_note_label.text(), "")

    def test_no_pause_shows_no_banner(self):
        win = self._window("dark", state=None, persisted=True)
        self.assertFalse(win.quota_banner.isVisibleTo(win))

    def test_the_banner_sits_below_a_crash_recovery_banner_when_both_are_true(self):
        from gui import main_window as mw
        win = self._window("dark", persisted=True)
        with patch.object(mw.crashlog, "faults_in_previous_run", return_value=0):
            win._show_run_banner(win.entries)
        self.assertTrue(win.run_banner.isVisibleTo(win))
        self.assertTrue(win.quota_banner.isVisibleTo(win))
        self.assertLess(win.run_banner.y(), win.quota_banner.y())


class TestStrip(_PausedWindow):
    def _paused_window(self, mode="dark", persisted=False):
        quota = SauceNaoQuota(long_remaining=0, long_limit=200)
        with patch("gui.main_window.get_last_quota", return_value=quota):
            win = self._window(mode, persisted=persisted)
            win._refresh_saucenao_quota_label()
        return win

    def test_the_strip_reads_paused_the_count_and_eta_paused(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._paused_window(mode)
                self.assertEqual(win.run_note_label.text(), "paused \u2014 quota exhausted")
                self.assertIn("200/200", win.saucenao_quota_label.text())
                self.assertEqual(win.run_eta_label.text(), "ETA paused")

    def test_the_run_ending_does_not_wipe_the_pause_from_the_strip(self):
        """The worker's finish follows the pause signal and refreshes the
        strip; it used to put `ETA \u2014` back."""
        win = self._paused_window()
        win._on_worker_finished()
        self.assertEqual(win.run_eta_label.text(), "ETA paused")
        self.assertEqual(win.run_note_label.text(), "paused \u2014 quota exhausted")

    def test_after_a_relaunch_the_strip_still_says_paused(self):
        win = self._paused_window(persisted=True)
        self.assertEqual(win.run_note_label.text(), "paused \u2014 quota exhausted")
        self.assertEqual(win.run_eta_label.text(), "ETA paused")

    def test_an_ordinary_idle_strip_is_unchanged(self):
        win = self._window("dark", state=None, persisted=True)
        self.assertEqual(win.run_eta_label.text(), "ETA \u2014")
        self.assertEqual(win.run_note_label.text(), "")


if __name__ == "__main__":
    unittest.main()
