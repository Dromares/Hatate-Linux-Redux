"""DAN-1277 (GUI parity P13, rows U2-U6 of the DAN-1272 acceptance): the
app chrome - menubar and status bar, the 32px page gutter, the filter
bar's Clear link and "N hidden", the Review toolbar's boxed icon buttons
and the candidate picker.

Widths and heights are read off the laid-out window and colours off the
painted pixels, not off the stylesheet text.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from core.models import ImageEntry, MatchCandidate, MatchStatus
from gui import theme, widgets
from tests.test_flat_surfaces import MODES, _hairline, _near, _rgb
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _composite_over, _contrast_ratio, _parse_hex

MIN_TEXT_CONTRAST = 4.5
LONG_URL = "https://danbooru.donmai.us/posts/7340112?q=a_very_long_query_string_that_cannot_fit"


class _ChromeCase(GuiTestCase):
    def setUp(self):
        for target in ("gui.main_window.message.information",
                       "gui.main_window.MainWindow._restore_saved_session",
                       "gui.main_window.MainWindow._start_missing_file_check"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _window(self, mode, statuses=(MatchStatus.GOOD, MatchStatus.GOOD, MatchStatus.NOT_FOUND,
                                      MatchStatus.NOT_FOUND, MatchStatus.NOT_FOUND)):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        entries = []
        for i, status in enumerate(statuses):
            path = os.path.join(tmp.name, f"img_{i}.png")
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0x336699)
            image.save(path)
            entry = ImageEntry(path=path, status=status)
            if status is MatchStatus.GOOD:
                entry.candidates = [MatchCandidate(
                    url=LONG_URL, similarity=96.0, source_name="Danbooru", engine="SauceNAO")]
                entry.select_candidate(0)
            entries.append(entry)
        win.entries[:] = entries
        win.table_model.refresh_all(win.entries)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        self._settle()
        return win

    @staticmethod
    def _settle():
        for _ in range(3):
            QApplication.processEvents()

    def _shot(self, win):
        self._settle()
        return win.grab().toImage()

    @staticmethod
    def _at(win, widget, x=0, y=0):
        return widget.mapTo(win, QPoint(x, y))

    def _review(self, win):
        win.table.selectRow(0)
        win.set_mode("review")
        win._on_selection_changed()
        self._settle()


class TestPageGutter(_ChromeCase):
    """U3: 32px left and right on every screen."""

    def test_every_mode_starts_and_ends_32px_in_from_the_window_edge(self):
        for mode in MODES:
            win = self._window(mode)
            for key in ("queue", "review", "activity"):
                with self.subTest(mode=mode, screen=key):
                    win.set_mode(key)
                    self._settle()
                    page = win._mode_pages[key]
                    left = self._at(win, page).x()
                    right = win.width() - (left + page.width())
                    self.assertEqual((left, right), (32, 32),
                                     f"{mode}/{key}: measured gutter {left}px left, {right}px right")
                    self.assertEqual(widgets.PAGE_GUTTER, 32)

    def test_the_top_bar_and_run_strip_share_the_gutter(self):
        win = self._window("dark")
        self.assertEqual(self._at(win, win.mode_stack).x(), 32)
        strip = win.progress_bar.parentWidget()
        self.assertEqual(self._at(win, strip).x(), 32)


class TestMenuBar(_ChromeCase):
    """U2: 13px text, 28px with a hairline below."""

    def test_menubar_is_28px_with_13px_text_and_a_hairline(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                bar = win.menuBar()
                self.assertEqual(bar.height(), widgets.MENUBAR_HEIGHT)
                self.assertEqual(widgets.MENUBAR_HEIGHT, 28)
                self.assertEqual(bar.font().pixelSize(), 13)
                img = self._shot(win)
                want = _hairline(mode)
                got = _rgb(img, 700, bar.height() - 1)
                self.assertTrue(_near(got, want), f"{mode}: rule paints {got}, expected {want}")
                above = _rgb(img, 700, bar.height() - 3)
                self.assertTrue(_near(above, _parse_hex(theme.palette(mode)["page"])))

    def test_first_menu_starts_on_the_page_gutter(self):
        win = self._window("dark")
        bar = win.menuBar()
        first = bar.actionGeometry(bar.actions()[0])
        # Item padding is 12px, so the text begins 12px into the item.
        self.assertEqual(first.x() + 12, widgets.PAGE_GUTTER)


class TestStatusBar(_ChromeCase):
    """U2: 24px, 10px mono, dim, inset 32px, with a top rule."""

    def test_geometry_and_voice(self):
        win = self._window("dark")
        bar, label = win.status_bar, win.status_label
        self.assertEqual(bar.height(), widgets.STATUSBAR_HEIGHT)
        self.assertEqual(widgets.STATUSBAR_HEIGHT, 24)
        self.assertEqual(label.font().pixelSize(), 10)
        self.assertIn("mono", label.font().family().lower())
        text_x = self._at(win, label).x() + label.contentsMargins().left()
        self.assertEqual(text_x, widgets.PAGE_GUTTER)

    def test_top_rule_is_painted(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                img = self._shot(win)
                y = win.status_bar.geometry().top()
                got = _rgb(img, 700, y)
                self.assertTrue(_near(got, _hairline(mode)), f"{mode}: rule paints {got}")

    def test_text_clears_4_5_to_1_against_its_background(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self._shot(win)
                page = _parse_hex(theme.palette(mode)["page"])
                colour = win.status_label.palette().color(win.status_label.foregroundRole())
                painted = _composite_over(
                    (colour.red(), colour.green(), colour.blue()), colour.alphaF(), page)
                ratio = _contrast_ratio(painted, page)
                print(f"\nDAN-1277 status bar {mode}: {painted} on {page} = {ratio:.2f}:1")
                self.assertGreaterEqual(ratio, MIN_TEXT_CONTRAST)


class TestFilterBar(_ChromeCase):
    """U4: Clear is a link, the count is "N hidden" and right-aligned, the
    input flexes."""

    def _filter_to_good(self, win):
        win.filter_bar._statuses = {MatchStatus.GOOD}
        win.filter_bar.changed.emit()
        self._settle()

    def test_clear_is_an_underlined_link_not_a_bordered_button(self):
        win = self._window("dark")
        clear = win.filter_bar.filter_clear_button
        self.assertEqual(clear.objectName(), "LinkButton")
        self.assertTrue(clear.font().underline())
        self.assertEqual(clear.text(), "Clear")
        self.assertTrue(clear.isFlat())
        self._filter_to_good(win)
        img = self._shot(win)
        mid = clear.height() // 2
        edge = self._at(win, clear, 0, mid)
        outside = self._at(win, clear, -4, mid)
        # No box: the button's own left edge paints what the band beside it does.
        self.assertTrue(_near(_rgb(img, edge.x(), edge.y()), _rgb(img, outside.x(), outside.y())))

    def test_count_is_the_number_of_rows_the_filter_hides(self):
        win = self._window("dark")
        self.assertEqual(win.filter_bar.filter_count_label.text(), "")
        self._filter_to_good(win)
        self.assertEqual(win.table_model.visible_count(), 2)
        self.assertEqual(win.filter_bar.filter_count_label.text(), "3 hidden")
        self.assertIn("2 of 5", win.filter_bar.filter_count_label.toolTip())
        win.filter_bar.filter_text.setText("img_0")
        self._settle()
        self.assertEqual(win.table_model.visible_count(), 1)
        self.assertEqual(win.filter_bar.filter_count_label.text(), "4 hidden")
        win.filter_bar.clear()
        self._settle()
        self.assertEqual(win.filter_bar.filter_count_label.text(), "")

    def test_count_is_right_aligned_to_the_bar_edge(self):
        win = self._window("dark")
        self._filter_to_good(win)
        band, count = win.filter_bar, win.filter_bar.filter_count_label
        right_gap = band.contentsRect().right() - count.geometry().right()
        self.assertEqual(right_gap, band.layout().contentsMargins().right())

    def test_input_fills_the_row_and_grows_with_the_window(self):
        win = self._window("dark")
        field = win.filter_bar.filter_text
        before = field.width()
        self.assertGreater(before, 240, "the input is still capped near its old fixed width")
        win.resize(1800, 900)
        self._settle()
        self.assertGreater(field.width(), before)


class TestReviewToolbar(_ChromeCase):
    """U5: pop-out, shortcuts and show-in-queue are three boxed 36px buttons."""

    def test_three_boxed_36px_icon_buttons(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self._review(win)
                tools = [b for b in win.review_view_switch.parentWidget().findChildren(widgets.QPushButton)
                         if b.objectName() == "IconButton" and b.text() in ("⧉", "⌨", "⮐")]
                self.assertEqual(sorted(b.text() for b in tools), sorted(["⧉", "⌨", "⮐"]))
                for b in tools:
                    self.assertTrue(b.property("boxed"))
                    self.assertEqual((b.width(), b.height()), (36, 36))
                img = self._shot(win)
                b = tools[0]
                p = self._at(win, b, 0, b.height() // 2)
                edge = _rgb(img, p.x(), p.y())
                inside = _rgb(img, p.x() + 4, p.y())
                self.assertGreaterEqual(_contrast_ratio(edge, inside), 3.0 - 0.5,
                                        f"{mode}: no visible box edge {edge} vs {inside}")


class TestCandidatePicker(_ChromeCase):
    """U6: mono, ellipsised, with a visible chevron."""

    def test_long_match_is_ellipsised_in_mono_with_a_chevron(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self._review(win)
                combo = win.candidate_combo
                self.assertIn("mono", combo.font().family().lower())
                self.assertEqual(combo.height(), 32)
                shown = combo.elided_text()
                self.assertTrue(shown.endswith("…"), shown)
                self.assertTrue(shown.startswith("Danbooru"), shown)
                self.assertLess(len(shown), len(combo.currentText()))
                # Full text is still what the item holds.
                self.assertIn(LONG_URL, combo.currentText())
                # A visible chevron: the arrow box holds ink brighter than the field.
                img = self._shot(win)
                opt = combo._option()
                arrow = combo.style().subControlRect(
                    combo.style().ComplexControl.CC_ComboBox, opt,
                    combo.style().SubControl.SC_ComboBoxArrow, combo)
                base = _rgb(img, *(lambda p: (p.x() + 2, p.y() + 2))(self._at(win, combo, arrow.x(), arrow.y())))
                best = max(
                    _contrast_ratio(_rgb(img, p.x(), p.y()), base)
                    for p in (self._at(win, combo, arrow.x() + dx, arrow.y() + dy)
                              for dx in range(arrow.width()) for dy in range(arrow.height()))
                )
                self.assertGreaterEqual(best, 3.0, f"{mode}: chevron not visible (peak {best:.2f}:1)")

    def test_short_text_is_not_ellipsised(self):
        win = self._window("dark")
        self._review(win)
        combo = win.candidate_combo
        combo.setItemText(0, "Danbooru")
        self.assertEqual(combo.elided_text(), "Danbooru")


if __name__ == "__main__":
    unittest.main()
