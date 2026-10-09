"""DAN-1162 (GUI parity P3, rows R-01/R-02/R-03/R-05 of the DAN-1156 gap
register): the run strip is a mono `00 // RUN` line with a 2px hairline
gauge, bold figures, a right-aligned ETA, and a sentence when idle.

The contrast claims are measured on pixels the window actually paints, not
read off the palette, because the failure being fixed was exactly a palette
value that looked fine and painted as an empty black slab.
"""
import unittest

from PyQt6.QtGui import QFont, QFontInfo
from PyQt6.QtWidgets import QApplication, QLabel

from core.models import ImageEntry, MatchStatus
from gui import shell, theme
from tests.test_flat_surfaces import MODES, _rgb, _WindowCase
from tests.test_theme import _composite_over, _contrast_ratio, _parse_hex, _parse_rgba

# WCAG 1.4.11: a UI component's boundary needs 3:1 against what it sits on.
MIN_NON_TEXT_CONTRAST = 3.0
# The ask: the label text the strip is read by is body text.
MIN_TEXT_CONTRAST = 4.5


def _settle(win):
    for _ in range(3):
        QApplication.processEvents()


class TestStripTrackIsVisible(_WindowCase):
    """R-02: the empty gauge must read as a gauge, not a black slab."""

    def _gauge_pixels(self, win):
        """(track, strip, fill): painted colours of the gauge at 0, the
        strip beside it, and the gauge once full."""
        gauge = win.progress_bar
        gauge.setMaximum(10)
        gauge.setValue(0)
        img = self._shot(win)
        top = self._at(win, gauge, 10, 0)
        track = _rgb(img, top.x(), top.y())
        beside = self._at(win, gauge, gauge.width() + 4, 0)
        strip = _rgb(img, beside.x(), beside.y())
        gauge.setValue(10)
        img = self._shot(win)
        fill = _rgb(img, top.x(), top.y())
        return track, strip, fill

    def test_track_edge_clears_3_to_1_against_the_strip(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                track, strip, _ = self._gauge_pixels(win)
                ratio = _contrast_ratio(track, strip)
                self.assertGreaterEqual(
                    ratio, MIN_NON_TEXT_CONTRAST,
                    f"{mode}: painted track {track} vs strip {strip} = {ratio:.2f}:1",
                )

    def test_fill_is_distinct_from_the_track(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                track, _, fill = self._gauge_pixels(win)
                self.assertGreaterEqual(_contrast_ratio(fill, track), MIN_NON_TEXT_CONTRAST)

    def test_the_track_token_itself_clears_the_floor(self):
        """Guards the palette against the next "make it quieter" edit, which
        would otherwise only show up as a painted-pixel failure."""
        for mode in MODES:
            with self.subTest(mode=mode):
                p = theme.palette(mode)
                page = _parse_hex(p["page"])
                rgb, alpha = _parse_rgba(p["ink_46"])
                track = _composite_over(rgb, alpha, page)
                self.assertGreaterEqual(_contrast_ratio(track, page), MIN_NON_TEXT_CONTRAST)


class TestStripGeometry(_WindowCase):
    def test_gauge_is_a_220px_two_pixel_hairline(self):
        win = self._window("dark")
        _settle(win)
        self.assertEqual(win.progress_bar.width(), 220)
        self.assertEqual(win.progress_bar.height(), 2)

    def test_run_label_is_leftmost_then_gauge(self):
        win = self._window("dark")
        _settle(win)
        tag = win.run_tag
        self.assertEqual(tag.text(), "00 // Run")
        self.assertEqual(tag.font().capitalization(), QFont.Capitalization.AllUppercase)
        self.assertLess(tag.geometry().right(), win.progress_bar.geometry().left())
        self.assertLess(
            self._at(win, tag, 0, 0).x(), self._at(win, win.progress_bar, 0, 0).x(),
        )

    def test_eta_is_right_aligned_and_last(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                _settle(win)
                strip_right = self._at(win, win.run_strip, win.run_strip.width(), 0).x()
                eta = win.run_eta_label
                eta_right = self._at(win, eta, eta.width(), 0).x()
                # Inside the strip's own 18px right margin plus its 1px frame.
                self.assertGreaterEqual(eta_right, strip_right - 20)
                for other in (win.run_progress_label, win.sent_count_label,
                              win.wait_countdown_label, win.saucenao_quota_label):
                    self.assertLessEqual(
                        self._at(win, other, other.width(), 0).x(),
                        self._at(win, eta, 0, 0).x(),
                    )


class TestStripVoice(_WindowCase):
    def _running(self, win):
        win._run_active = True
        win.entries[0].sent_to_hydrus = True
        win.entries[0].hydrus_import_confirmed = True
        win._on_worker_progress(2, 8)
        win._on_wait_countdown("Rate limit", 38.0, 60.0)
        win._refresh_sent_count_label()
        _settle(win)

    def test_mockup_vocabulary_while_running(self):
        win = self._window("dark")
        self._running(win)
        self.assertEqual(win.run_progress_label.text(), "2/8 searched")
        self.assertEqual(win.wait_countdown_label.text(), "waiting 38s")
        self.assertTrue(win.sent_count_label.text().startswith("1 sent"))

    def test_gauge_means_searched_and_sent_is_a_separate_count(self):
        """R-04/B-4: the gauge is `2/8 searched`; `sent` is not its fraction."""
        win = self._window("dark")
        self._running(win)
        self.assertEqual(win.progress_bar.value(), 2)
        self.assertEqual(win.progress_bar.maximum(), 8)
        self.assertNotIn("/", win.sent_count_label.text())
        self.assertNotIn("sent", win.run_progress_label.text())

    def test_quota_reads_without_a_colon(self):
        import core.saucenao as sn
        win = self._window("dark")
        sn._last_quota = sn.SauceNaoQuota(
            short_remaining=16, short_limit=17, long_remaining=58, long_limit=200,
        )
        self.addCleanup(setattr, sn, "_last_quota", None)
        win._refresh_saucenao_quota_label()
        self.assertEqual(win.saucenao_quota_label.text(), "SauceNAO 142/200 used today")

    def test_eta_reads_in_its_own_readout(self):
        win = self._window("dark")
        win._run_active = True
        win._on_worker_progress(1, 24000)
        self.assertEqual(win.run_eta_label.text(), "ETA —")     # nothing to average yet
        win._run_estimate.reset()
        for done, t in ((1, 0.0), (2, 60.0), (3, 120.0)):
            win._run_estimate.record(done, 24000, now=t)
        win.progress_bar.setValue(3)
        win._refresh_run_progress_label()
        eta = win.run_eta_label.text()
        self.assertTrue(eta.startswith("ETA ~16d"), eta)
        self.assertNotIn("left", win.run_progress_label.text())

    def test_figures_are_bold_and_labels_are_mono(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self._running(win)
                figures = [lab for lab in win.run_strip.findChildren(QLabel)
                           if lab.objectName() == "RunFigure" and lab.isVisible()]
                words = [lab for lab in win.run_strip.findChildren(QLabel)
                         if lab.objectName() == "RunReadout" and lab.isVisible()]
                self.assertTrue(figures and words)
                for lab in figures:
                    lab.ensurePolished()
                    self.assertTrue(QFontInfo(lab.font()).bold(), f"{lab.text()!r} is not bold")
                for lab in words:
                    lab.ensurePolished()
                    self.assertFalse(QFontInfo(lab.font()).bold(), f"{lab.text()!r} is bold")
                for lab in figures + words:
                    self.assertEqual(lab.font().family(), "JetBrains Mono", lab.text())

    def test_label_text_clears_the_body_text_floor(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                p = theme.palette(mode)
                page = _parse_hex(p["page"])
                for key in ("ink_65", "ink_100"):
                    rgb, alpha = _parse_rgba(p[key])
                    ink = _composite_over(rgb, alpha, page)
                    self.assertGreaterEqual(_contrast_ratio(ink, page), MIN_TEXT_CONTRAST)

    def test_a_new_reading_leaves_no_old_text_under_it(self):
        """REGRESSION (found in the first screenshot): the previous
        fragments were only scheduled for deletion, so until the event loop
        ran they still painted, overprinting the new reading."""
        win = self._window("dark")
        readout = win.run_progress_label
        readout.setText("2/8 searched")
        readout.setText("nothing queued")
        self.assertEqual(
            [lab.text() for lab in readout.findChildren(QLabel)], ["nothing queued"],
        )

    def test_readout_text_and_figures_lose_nothing(self):
        for text in ("2/8 searched", "waiting 38s", "ETA ~16d 22h · ends 3 Oct",
                     "SauceNAO 142/200 used today", "ETA —", "1,234/24,000 searched"):
            with self.subTest(text=text):
                self.assertEqual("".join(f for f, _ in shell.split_figures(text)), text)
        self.assertEqual(
            [f for f, is_fig in shell.split_figures("2/8 searched") if is_fig], ["2/8"],
        )
        self.assertEqual(
            [f for f, is_fig in shell.split_figures("ETA ~16d 22h") if is_fig], ["~16d", "22h"],
        )


class TestIdleStrip(_WindowCase):
    """R-05: an idle strip says it is idle. It is never blank."""

    def test_empty_window_reads_nothing_queued_eta_dash(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                win.entries[:] = []
                win.table_model.refresh_all(win.entries)
                win._refresh_sent_count_label()
                _settle(win)
                self.assertEqual(win.run_progress_label.text(), "nothing queued")
                self.assertEqual(win.run_eta_label.text(), "ETA —")
                self.assertTrue(win.run_progress_label.isVisible())
                self.assertTrue(win.run_eta_label.isVisible())

    def test_a_fresh_window_is_not_blank_before_anything_refreshes(self):
        from tests.test_gui_harness import make_themed_window
        _app, win = make_themed_window(self, "dark")
        self.addCleanup(win.close)
        self.assertEqual(win.run_progress_label.text(), "nothing queued")
        self.assertEqual(win.run_eta_label.text(), "ETA —")

    def test_listed_but_unsearched_images_are_not_called_nothing_queued(self):
        win = self._window("dark")
        win._refresh_sent_count_label()
        self.assertEqual(win.run_progress_label.text(), "3 not searched")
        self.assertEqual(win.run_eta_label.text(), "ETA —")

    def test_a_finished_run_keeps_its_count_and_drops_the_eta(self):
        win = self._window("dark")
        win._run_active = True
        for done, t in ((1, 0.0), (2, 60.0), (3, 120.0)):
            win._run_estimate.record(done, 8, now=t)
        win._on_worker_progress(3, 8)
        self.assertTrue(win.run_eta_label.text().startswith("ETA ~"))
        win._on_worker_finished()
        self.assertEqual(win.run_progress_label.text(), "3/8 searched")
        self.assertEqual(win.run_eta_label.text(), "ETA —")
        win.entries.append(ImageEntry(path="/nowhere.png", status=MatchStatus.NOT_SEARCHED))
        win._refresh_sent_count_label()
        self.assertEqual(win.run_progress_label.text(), "3/8 searched")


if __name__ == "__main__":
    unittest.main()
