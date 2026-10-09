"""The Review compare header's similarity readout (DAN-1169, gap row V-01).

The mockup puts the number a reviewer decides on in the compare header as
a 28px readout - `● 96% SIMILARITY ▲ 2.5× larger` - where production had
a small centred line above the images. B-5 keeps production's honesty: a
score that is only the engine's ranking says `~96% RANKING`, never
SIMILARITY. R-3 (DAN-1178) keeps the zoom controls out of the header and
inside the Wipe/Differences frame, so the readout fits the compare column.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QPushButton

from core.models import ImageEntry, MatchCandidate, MatchStatus
from gui.preview_text import comparison_readout
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase

MODES = ("dark", "light")
COMPARE_COLUMN_WIDTH = 1012   # the mockup's compare column at 1440x900


def _entry(measured, similarity=96.0, local=(1000, 1000)):
    entry = ImageEntry(path="/nonexistent/local.png")
    entry.similarity = similarity
    entry.similarity_measured = measured
    entry.local_width, entry.local_height = local
    return entry


class TestReadoutParts(unittest.TestCase):
    def test_a_measured_score_reads_similarity_with_a_good_glyph(self):
        readout = comparison_readout(
            _entry(True), MatchCandidate(url="u", width=2500, height=2500))
        self.assertEqual(
            (readout.glyph, readout.value, readout.label, readout.diff),
            ("●", "96%", "Similarity", "▲ 2.5× larger"))

    def test_a_ranking_never_says_similarity(self):
        """B-5: `~96% RANKING`, in the label and in the value's mark."""
        readout = comparison_readout(
            _entry(False), MatchCandidate(url="u", width=2500, height=2500))
        self.assertEqual((readout.value, readout.label), ("~96%", "Ranking"))
        self.assertNotIn("imilarity", readout.label)

    def test_a_smaller_match_draws_no_up_arrow(self):
        readout = comparison_readout(
            _entry(True), MatchCandidate(url="u", width=500, height=500))
        self.assertEqual(readout.diff, "2× smaller")

    def test_no_candidate_says_nothing(self):
        readout = comparison_readout(_entry(True), None)
        self.assertEqual((readout.glyph, readout.value, readout.label, readout.diff),
                         ("", "", "", ""))

    def test_the_tooltip_keeps_what_the_banner_said(self):
        readout = comparison_readout(
            _entry(False), MatchCandidate(url="u", width=2500, height=2500))
        self.assertIn("~96% similar (ranking)", readout.tooltip)
        self.assertIn("2.5× larger", readout.tooltip)


class TestReadoutInTheHeader(GuiTestCase):
    def setUp(self):
        # A session restored from an earlier test can name files that are
        # gone, and the window then raises a modal "Missing files" box,
        # which blocks an offscreen run for good. Nothing here is about it.
        patcher = mock.patch("gui.main_window.message.information")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _review_window(self, mode, measured=True):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "img.png")
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        image.save(path)
        entry = ImageEntry(path=path, status=MatchStatus.GOOD)
        entry.candidates = [MatchCandidate(
            url="https://danbooru.donmai.us/posts/1", similarity=96.0,
            source_name="Danbooru", engine="IQDB", width=20, height=20)]
        entry.local_width = entry.local_height = 8
        entry.select_candidate(0)
        entry.similarity_measured = measured
        win.entries[:] = [entry]
        win.table_model.refresh_all(win.entries)
        win.resize(1440, 900)
        win.set_mode("review")
        win.show()
        self.addCleanup(win.close)
        win.table.selectRow(0)
        win._on_selection_changed()
        QApplication.processEvents()
        return win

    def test_the_readout_is_28px_and_shows_every_part(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                self.assertTrue(win.comparison_readout.isVisible())
                self.assertEqual(win.readout_value.fontInfo().pixelSize(), 28)
                self.assertEqual(win.readout_value.text(), "96%")
                self.assertEqual(win.readout_label.text(), "Similarity")
                self.assertEqual(win.readout_glyph.text(), "●")
                self.assertEqual(win.readout_diff.text(), "▲ 2.5× larger")

    def test_a_ranking_row_is_marked_in_the_window(self):
        win = self._review_window("dark", measured=False)
        self.assertEqual(win.readout_value.text(), "~96%")
        self.assertEqual(win.readout_label.text(), "Ranking")

    def test_the_banner_row_is_gone(self):
        win = self._review_window("dark")
        self.assertFalse(hasattr(win, "comparison_banner"))

    def test_the_readout_fits_the_compare_column(self):
        """R-3: in the 1012px column the readout sits clear of the view
        switch on its left and the header tools on its right."""
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                card = win.review_splitter.widget(0)
                # What the header NEEDS, not what it is stretched to.
                self.assertLessEqual(card.minimumSizeHint().width(),
                                     COMPARE_COLUMN_WIDTH)
                switch = win.review_view_switch
                tools = [b for b in card.findChildren(QPushButton)
                         if b.toolTip().startswith(("Open the comparison", "Show all"))]
                self.assertEqual(len(tools), 2)

                def span(widget):
                    left = widget.mapTo(card, widget.rect().topLeft()).x()
                    return left, left + widget.width()

                r_left, r_right = span(win.comparison_readout)
                self.assertGreaterEqual(r_left, span(switch)[1])
                self.assertLessEqual(r_right, min(span(t)[0] for t in tools))
                self.assertLessEqual(max(span(t)[1] for t in tools), card.width())

    def test_zoom_floats_inside_the_frame_not_in_the_header(self):
        win = self._review_window("dark")
        win.review_stack.setCurrentIndex(1)
        win._show_comparison_controls(True)
        tools = win.review_zoom_tools
        # Before the event loop runs: a late worker result for the selected
        # row can reset the view and hide the controls again.
        self.assertTrue(tools.isVisibleTo(win.review_wipe))
        QApplication.processEvents()
        self.assertIs(tools.parentWidget(), win.review_wipe)
        header_widgets = (win.comparison_readout, win.review_view_switch)
        for widget in header_widgets:
            self.assertFalse(widget.isAncestorOf(tools))
            self.assertNotEqual(widget.parentWidget(), win.review_wipe)
        frame = win.review_wipe.rect()
        self.assertTrue(frame.contains(tools.geometry()))
        # top right of the frame, as in review-wipe.html
        self.assertGreater(tools.geometry().center().x(), frame.center().x())
        self.assertLess(tools.geometry().center().y(), frame.center().y())
        win._show_comparison_controls(False)
        self.assertFalse(tools.isVisibleTo(win.review_wipe))


if __name__ == "__main__":
    unittest.main()
