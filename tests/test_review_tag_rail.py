"""The Review side rail's match navigation and tag list (DAN-1170, gap rows
V-05 and V-07).

V-05: `MATCH n OF m` in a hairline box with 36px boxed steppers, in place of
a serif heading and ~12px bare glyphs. V-07: tag rows are the mockup's 32px
hairline-ruled rows with the `[Source]` marker dimmed and the tag bright.
CEO ruling R-1 (DAN-1178) sets the depth: at 1440x900 at least five full
rows plus a clipped one are visible, and the list scrolls.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFontMetrics, QImage
from PyQt6.QtWidgets import QApplication

from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
from gui import table_delegates, theme, widgets
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _contrast_ratio

MODES = ("dark", "light")
MIN_CONTRAST = 4.5
TAG_NAMES = ["solo", "outdoors", "sky", "cloud", "smile", "sitting", "flower", "1girl", "scenery"]


def _rgb(color):
    return color.red(), color.green(), color.blue()


def _strongest_contrast(image, x0, x1, y0, y1, fill):
    best = 1.0
    for y in range(y0, y1):
        for x in range(x0, x1):
            best = max(best, _contrast_ratio(_rgb(image.pixelColor(x, y)), fill))
    return best


class TestReviewTagRail(GuiTestCase):
    def setUp(self):
        patcher = mock.patch("gui.main_window.message.information")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _review_window(self, mode, candidates=3):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "img.png")
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        image.save(path)
        entry = ImageEntry(path=path, status=MatchStatus.GOOD)
        entry.candidates = [
            MatchCandidate(url=f"https://danbooru.donmai.us/posts/{k}",
                           similarity=96.0 - k, source_name="Danbooru", engine="IQDB")
            for k in range(candidates)]
        entry.select_candidate(0)
        entry.tags = ([Tag(n, TagSource.BOORU) for n in TAG_NAMES]
                      + [Tag("mine", TagSource.HYDRUS)])
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

    # -- V-05: match navigation ----------------------------------------
    def test_match_label_sits_in_a_hairline_box_with_the_steppers(self):
        win = self._review_window("dark")
        nav = win.review_match_nav
        self.assertEqual(win.review_match_label.text(), "Match 1 of 3")
        self.assertEqual(win.review_match_label.objectName(), "MatchLabel")
        self.assertEqual(win.review_match_label.font().capitalization(),
                         win.review_match_label.font().Capitalization.AllUppercase)
        for part in (win.review_match_label, win.review_prev_match_btn,
                     win.review_next_match_btn):
            self.assertIs(part.parentWidget(), nav)
        self.assertEqual(nav.objectName(), "MatchNav")

    def test_steppers_are_36px_boxed_triangles_with_the_bracket_keys(self):
        win = self._review_window("dark")
        for button, glyph, key in ((win.review_prev_match_btn, "◀", "["),
                                   (win.review_next_match_btn, "▶", "]")):
            self.assertEqual(button.text(), glyph)
            self.assertEqual((button.width(), button.height()),
                             (widgets.ICON_BOX, widgets.ICON_BOX))
            self.assertEqual(widgets.ICON_BOX, 36)
            self.assertTrue(button.property("boxed"))
            self.assertIn(f"({key})", button.toolTip())

    def test_steppers_walk_the_candidates_and_disable_at_the_ends(self):
        win = self._review_window("dark")
        prev, nxt = win.review_prev_match_btn, win.review_next_match_btn
        self.assertEqual((prev.isEnabled(), nxt.isEnabled()), (False, True))
        nxt.click()
        self.assertEqual(win.candidate_combo.currentIndex(), 1)
        self.assertEqual(win.review_match_label.text(), "Match 2 of 3")
        self.assertEqual((prev.isEnabled(), nxt.isEnabled()), (True, True))
        nxt.click()
        self.assertEqual((prev.isEnabled(), nxt.isEnabled()), (True, False))
        prev.click()
        self.assertEqual(win.candidate_combo.currentIndex(), 1)

    def test_no_candidates_means_no_steppers_and_the_picker_stays_below(self):
        win = self._review_window("dark", candidates=0)
        self.assertEqual(win.review_match_label.text(), "No match")
        self.assertFalse(win.review_prev_match_btn.isEnabled())
        self.assertFalse(win.review_next_match_btn.isEnabled())
        # B-6: the candidate picker is still there, directly under the nav box.
        layout = win.review_match_nav.parentWidget().layout()
        self.assertIs(layout.itemAt(layout.indexOf(win.review_match_nav) + 1).widget(),
                      win.candidate_combo)

    # -- V-07: the tag list --------------------------------------------
    def test_rows_are_32px_and_at_least_five_full_rows_show_at_1440x900(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                tags = win.tag_list
                self.assertEqual(table_delegates.TAG_ROW_HEIGHT, 32)
                self.assertEqual(tags.sizeHintForRow(0), 32)
                height = tags.viewport().height()
                self.assertGreaterEqual(height // 32, 5)
                self.assertGreater(height % 32, 0, "a sixth row is clipped, not absent")
                # ...and it scrolls to the rest.
                self.assertGreater(tags.count() * 32, height)
                self.assertGreater(tags.verticalScrollBar().maximum(), 0)

    def test_the_marker_is_dimmed_the_tag_is_bright_and_each_row_is_ruled(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                tags = win.tag_list
                tags.clearSelection()
                tags.setCurrentRow(-1)
                QApplication.processEvents()
                image = tags.viewport().grab().toImage()
                fill = _rgb(image.pixelColor(3, 3))
                pad = table_delegates.TAG_ROW_PAD
                font = theme.mono_font(tags.font())
                font.setPixelSize(table_delegates.TAG_FONT_PX)
                source, _tag = table_delegates._TAG_SOURCE.match(tags.item(0).text()).groups()
                marker_w = QFontMetrics(font).horizontalAdvance(source + " ")
                text_y = (4, 28)
                marker = _strongest_contrast(image, pad, pad + marker_w - 4, *text_y, fill)
                tag = _strongest_contrast(image, pad + marker_w, pad + marker_w + 40, *text_y, fill)
                self.assertGreaterEqual(marker, MIN_CONTRAST)
                self.assertGreater(tag, marker + 1.0, "tag must read brighter than its marker")
                # the hairline under row 0 is ink, not the row's fill
                self.assertNotEqual(_rgb(image.pixelColor(pad, 31)), fill)
                self.assertEqual(_rgb(image.pixelColor(pad, 30)), fill)

    def test_a_tag_without_a_marker_draws_as_one_bright_run(self):
        win = self._review_window("dark")
        win.edit_tags_btn.setChecked(True)
        QApplication.processEvents()
        self.assertNotIn("[", win.tag_list.item(0).text())
        self.assertEqual(win.tag_list.sizeHintForRow(0), 32)
        self.assertIsNone(table_delegates._TAG_SOURCE.match(win.tag_list.item(0).text()))
        self.assertEqual(table_delegates._TAG_SOURCE.match("[Booru] a:b").groups(), ("[Booru]", "a:b"))


if __name__ == "__main__":
    unittest.main()
