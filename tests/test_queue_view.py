"""The Queue page: its empty state, and the chips it draws.

The `MainWindow()` built below is bare/unstyled - fine for behaviour,
but it measures smaller than the real app. Use
`test_gui_harness.make_themed_window()` instead for anything that
measures size.
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtWidgets import QApplication

from core.models import ImageEntry, MatchStatus
from gui import theme
from tests.test_gui_smoke import GuiTestCase


# Module-level, and deliberately so: a QApplication with no Python
# reference left is collected, and the next QWidget built then dies with
# "Must construct a QApplication before a QWidget".
_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestChipsCoverEveryStatus(unittest.TestCase):
    """A status with no glyph draws as plain text while every other row in
    the column is marked, which reads as "this one is fine"."""

    def test_every_status_has_a_glyph(self):
        """Glyph is mode-independent by design (gui/theme.py's docstring):
        the shape a status draws doesn't change with the theme, only the
        ink tier's resolved colour does - so this no longer needs the
        per-mode loop test_every_status_has_a_hue_in_both_modes used."""
        for status in MatchStatus:
            with self.subTest(status=status.value):
                self.assertIsNotNone(theme.status_glyph(status.value))
                self.assertIsNotNone(theme.status_weight(status.value))

    def test_every_status_has_chip_wording(self):
        for status in MatchStatus:
            with self.subTest(status=status.value):
                self.assertTrue(theme.chip_label(status.value))

    def test_the_sent_column_keys_have_glyphs_too(self):
        for key in ("sent", "queued"):
            with self.subTest(key=key):
                self.assertIsNotNone(theme.status_glyph(key))
                self.assertIsNotNone(theme.status_weight(key))

    def test_an_unknown_key_is_answered_rather_than_raised(self):
        """A status added to the model before the palette catches up must
        draw as plain text, not take the window down on a repaint."""
        self.assertIsNone(theme.status_glyph("no_such_status"))
        # status_weight falls back to the "inert" tier rather than None,
        # since a chip with no colour at all crashes QColor; an unknown
        # key drawing invisibly-faint is the safe failure, not a raise.
        self.assertEqual(theme.status_weight("no_such_status"), "ink_45")
        self.assertEqual(theme.chip_label("no_such_status", "fallback"), "fallback")


class TestTheEmptyState(unittest.TestCase):
    """REGRESSION GUARD: an empty FILTER is not an empty LIST.

    The drop zone replaces the table when there is nothing to show. If it
    keyed on how many rows are currently VISIBLE, then filtering to
    something that matches nothing would replace the list with "Drop
    images here" - telling the user their images are gone when they are
    only hidden, on a page whose filter bar is right there saying how
    many it hid.
    """

    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        from gui.main_window import MainWindow
        self.win = MainWindow()
        self.win.entries.clear()
        self.win.table_model.refresh_all(self.win.entries)

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()

    def _showing_drop_zone(self):
        return self.win.queue_stack.currentIndex() == 1

    def test_an_empty_list_shows_the_drop_zone(self):
        self.win._refresh_queue_empty_state()
        self.assertTrue(self._showing_drop_zone())

    def test_a_list_with_images_shows_the_table(self):
        self.win.entries.append(ImageEntry(path="/tmp/a.png"))
        self.win.table_model.refresh_all(self.win.entries)
        self.win._refresh_queue_empty_state()
        self.assertFalse(self._showing_drop_zone())

    def test_a_filter_that_matches_nothing_still_shows_the_table(self):
        self.win.entries.append(ImageEntry(path="/tmp/a.png"))
        self.win.table_model.refresh_all(self.win.entries)

        self.win.filter_bar.filter_text.setText("no-such-file-anywhere")
        self.win._apply_filter()

        self.assertEqual(self.win.table_model.visible_count(), 0,
                         "the filter was expected to hide the row")
        self.assertTrue(self.win.entries, "the entry must still be in the list")
        self.assertFalse(
            self._showing_drop_zone(),
            "a filter matching nothing must not claim the list is empty",
        )


class TestQueueTableChrome(GuiTestCase):
    """DAN-1171 (P4, rows Q-03..Q-07): the Queue table's chrome. Every
    number here is a claim the acceptance list makes, so each is measured
    on a themed window rather than read off a constant."""

    ROW_HEIGHT = 49

    @classmethod
    def setUpClass(cls):
        _app()

    def _window(self, width, mode="dark", entries=8):
        """A themed, shown window with a first-run (no saved) column layout."""
        from core.config import Settings
        from tests.test_gui_harness import make_themed_window
        # A fresh Settings(), not Settings.load(): a layout saved by an
        # earlier test's closeEvent would stop the default widths applying.
        with mock.patch("gui.main_window.Settings.load", return_value=Settings()):
            _app_, win = make_themed_window(self, mode)
        win.resize(width, 900)
        win.entries.extend(
            ImageEntry(path=f"/tmp/chrome_{i}.png", status=MatchStatus.GOOD,
                       similarity=90, booru_name="Danbooru")
            for i in range(entries))
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        win.show()
        self.addCleanup(win.close)
        for _ in range(5):
            _app().processEvents()
        return win

    def test_no_horizontal_scrollbar_at_1360_and_1440(self):
        for width in (1360, 1440):
            with self.subTest(width=width):
                win = self._window(width)
                self.assertEqual(win.table.horizontalScrollBar().maximum(), 0,
                                 "the default column layout overflows the viewport")

    def test_the_columns_fill_the_viewport_with_no_dead_strip(self):
        for width in (1360, 1440):
            with self.subTest(width=width):
                win = self._window(width)
                total = sum(win.table.columnWidth(c) for c in range(win.table_model.columnCount()))
                self.assertEqual(total, win.table.viewport().width())

    def test_columns_stay_user_resizable_and_movable(self):
        from PyQt6.QtWidgets import QHeaderView
        win = self._window(1440)
        header = win.table.horizontalHeader()
        self.assertTrue(header.sectionsMovable())
        self.assertTrue(header.stretchLastSection())
        last = win.table_model.columnCount() - 1
        for col in range(last):
            self.assertEqual(header.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive)
        before = win.table.columnWidth(1)
        win.table.setColumnWidth(1, before + 40)
        self.assertEqual(win.table.columnWidth(1), before + 40)

    def test_a_saved_layout_is_not_overwritten_by_the_defaults(self):
        win = self._window(1440)
        win.table.setColumnWidth(1, 333)
        win._default_widths_pending = False   # what a click on a divider does
        win.resize(1360, 900)
        for _ in range(5):
            _app().processEvents()
        self.assertEqual(win.table.columnWidth(1), 333)

    def test_the_default_widths_follow_the_window_until_the_user_resizes(self):
        win = self._window(1360)
        win.resize(1440, 900)
        for _ in range(5):
            _app().processEvents()
        self.assertEqual(win.table.horizontalScrollBar().maximum(), 0)
        total = sum(win.table.columnWidth(c) for c in range(win.table_model.columnCount()))
        self.assertEqual(total, win.table.viewport().width())

    def test_no_row_number_header(self):
        win = self._window(1440)
        self.assertFalse(win.table.verticalHeader().isVisible())

    def test_rows_are_49px_with_zebra_and_no_grid(self):
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                win = self._window(1440, mode)
                self.assertEqual(win.table.rowHeight(0), self.ROW_HEIGHT)
                self.assertEqual(win.table.rowHeight(7), self.ROW_HEIGHT)
                self.assertTrue(win.table.alternatingRowColors())
                self.assertFalse(win.table.showGrid())

    def test_the_zebra_stripe_is_quiet_but_visible(self):
        """Not card_alt - that is the interaction tone and read as a loud
        tan stripe in the light theme - and not invisible either."""
        from gui.theme import DARK, LIGHT
        for name, pal in (("dark", DARK), ("light", LIGHT)):
            with self.subTest(mode=name):
                self.assertNotEqual(pal["zebra"], pal["card"])
                self.assertNotEqual(pal["zebra"], pal["card_alt"])
                ratio = _contrast(pal["zebra"], pal["card"])
                self.assertGreater(ratio, 1.0)
                self.assertLess(ratio, 1.2, "a zebra this strong is noise, not structure")

    def test_the_alternate_row_colour_reaches_the_view(self):
        from PyQt6.QtGui import QPalette
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                win = self._window(1440, mode)
                alt = win.table.palette().color(QPalette.ColorRole.AlternateBase)
                self.assertEqual(alt.name(), theme.palette(mode)["zebra"])

    def test_every_cell_draws_a_hairline_rule_under_it(self):
        """Pixel check on a rendered row: the bottom edge of a plain cell
        is the ink_18 rule, not the row's own fill."""
        win = self._window(1440)
        pixmap = win.table.viewport().grab()
        image = pixmap.toImage()
        row_bottom = win.table.rowViewportPosition(1) + self.ROW_HEIGHT - 1
        inside = image.pixelColor(400, row_bottom - 6)
        rule = image.pixelColor(400, row_bottom)
        self.assertNotEqual(rule.name(), inside.name(), "no rule drawn under the row")

    def test_headers_are_uppercase_mono_left_aligned_and_size_diff_is_short(self):
        from gui import image_table_model as itm
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QFont
        self.assertIn("Size diff.", itm.COLUMNS)
        self.assertNotIn("Size Difference", itm.COLUMNS)
        win = self._window(1440)
        model = win.table_model
        for col in range(model.columnCount()):
            font = model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.FontRole)
            self.assertEqual(font.capitalization(), QFont.Capitalization.AllUppercase)
        header = win.table.horizontalHeader()
        self.assertTrue(header.defaultAlignment() & Qt.AlignmentFlag.AlignLeft)
        # The stored text is not shouted: copy and accessible names stay as written.
        self.assertEqual(
            model.headerData(itm.COL_SIZE_DELTA, Qt.Orientation.Horizontal,
                             Qt.ItemDataRole.DisplayRole), "Size diff.")

    def test_a_selected_row_keeps_its_chip_contrast(self):
        """DAN-1151 must not regress: ink on the stamp fill is 1:1, so the
        selected chip text has to come from the view's highlighted-text
        colour, which pairs with that fill."""
        from PyQt6.QtGui import QPalette
        for mode in ("dark", "light"):
            with self.subTest(mode=mode):
                win = self._window(1440, mode)
                pal = win.table.palette()
                fill = pal.color(QPalette.ColorRole.Highlight).name()
                text = pal.color(QPalette.ColorRole.HighlightedText).name()
                self.assertGreaterEqual(_contrast(text, fill), 4.5)


def _contrast(fg, bg):
    """WCAG contrast ratio of two '#rrggbb' colours."""
    def lum(hex_):
        channels = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


if __name__ == "__main__":
    unittest.main()
