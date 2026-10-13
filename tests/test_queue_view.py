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

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPalette
from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

from core.models import ImageEntry, MatchCandidate, MatchStatus
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


class _SpyPainter(QPainter):
    """A real painter that also records the fonts it was given and the
    rectangles it was asked to outline, so a paint test can assert on what
    the delegate decided without reading pixels back."""

    def __init__(self, device):
        super().__init__(device)
        self.fonts = []
        self.rects = []     # (QRectF, pen colour name)

    def setFont(self, font):
        self.fonts.append(QFont(font))
        super().setFont(font)

    def drawRect(self, rect):
        self.rects.append((QRectF(rect), self.pen().color().name()))
        super().drawRect(rect)


def _over(fg, bg):
    """'#rrggbb' of an alpha QColor composited on an opaque '#rrggbb'."""
    a = fg.alphaF()
    base = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    mix = [round(c * a + b * (1 - a)) for c, b in zip((fg.red(), fg.green(), fg.blue()), base, strict=True)]
    return "#%02x%02x%02x" % tuple(mix)


class TestQueueCellContent(GuiTestCase):
    """DAN-1172 (P5, rows Q-08..Q-13): what the Queue cells say and how the
    status chips are drawn."""

    CELL_W, CELL_H = 160, 49

    @classmethod
    def setUpClass(cls):
        _app()

    def _window(self, entries, mode="dark"):
        from core.config import Settings
        from tests.test_gui_harness import make_themed_window
        with mock.patch("gui.main_window.Settings.load", return_value=Settings()):
            _app_, win = make_themed_window(self, mode)
        # The delegate and the model read the mode from here, not from the
        # stylesheet, so a light-mode paint needs the setting to say so.
        win.settings.theme = mode
        win.resize(1440, 900)
        win.entries.extend(entries)
        win.table_model.refresh_all(win.entries)
        win.show()
        self.addCleanup(win.close)
        _app().processEvents()
        return win

    @staticmethod
    def _matched(local, remote, **kw):
        entry = ImageEntry(path="/tmp/cell.png", status=MatchStatus.GOOD, **kw)
        entry.local_width, entry.local_height = local
        entry.candidates = [MatchCandidate(
            url="https://danbooru.donmai.us/posts/1", similarity=90.0, engine="SauceNAO",
            width=remote[0], height=remote[1])]
        entry.select_candidate(0)
        return entry

    def _text(self, win, row, col, role=Qt.ItemDataRole.DisplayRole):
        return win.table_model.data(win.table_model.index(row, col), role)

    def _paint(self, win, row, col, selected=False):
        """Paint one cell through its real delegate; returns (spy, image)."""
        index = win.table_model.index(row, col)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, self.CELL_W, self.CELL_H)
        option.widget = win.table
        option.font = win.table.font()
        option.palette = win.table.palette()
        option.state = QStyle.StateFlag.State_Enabled
        if selected:
            option.state |= QStyle.StateFlag.State_Selected
        image = QImage(self.CELL_W, self.CELL_H, QImage.Format.Format_ARGB32)
        image.fill(QColor(win.table.palette().color(QPalette.ColorRole.Base)))
        spy = _SpyPainter(image)
        win.table.itemDelegateForColumn(col).paint(spy, option, index)
        spy.end()
        return spy, image

    # -- Q-11 ----------------------------------------------------------

    def test_size_ratio_reads_triangle_ratio_times(self):
        from gui.image_table_model import COL_SIZE_DELTA
        win = self._window([
            self._matched((1000, 1500), (2500, 3750)),   # 2.5x bigger
            self._matched((1000, 1500), (400, 600)),     # 0.4x
            self._matched((1000, 1500), (1000, 1500)),   # same
            self._matched((1000, 1500), (0, 0)),         # site states no size
            ImageEntry(path="/tmp/none.png"),            # no match at all
        ])
        got = [self._text(win, r, COL_SIZE_DELTA) for r in range(5)]
        self.assertEqual(got, ["\u25b2 2.5\u00d7", "0.4\u00d7", "=", "\u2014", "\u2014"])

    # -- Q-12 ----------------------------------------------------------

    def test_every_empty_cell_is_a_dash(self):
        from gui import image_table_model as m
        win = self._window([ImageEntry(path="/tmp/empty.png")])
        for col in (m.COL_ENGINE, m.COL_SIMILARITY, m.COL_SIZE_DELTA, m.COL_BOORU,
                    m.COL_CACHE, m.COL_SENT, m.COL_REVIEWED, m.COL_UPSCALE):
            with self.subTest(column=m.COLUMNS[col]):
                self.assertEqual(self._text(win, 0, col), "\u2014")
        # A count of zero is a fact, not an absence: it stays a number.
        self.assertEqual(self._text(win, 0, m.COL_TAGS), "0")

    def test_the_dash_clears_the_body_text_contrast_floor(self):
        from gui import image_table_model as m
        for mode in ("dark", "light"):
            win = self._window([ImageEntry(path="/tmp/empty.png")], mode)
            card = theme.palette(mode)["card"]
            for col in (m.COL_ENGINE, m.COL_SIMILARITY, m.COL_SIZE_DELTA, m.COL_BOORU,
                        m.COL_CACHE, m.COL_SENT, m.COL_REVIEWED, m.COL_UPSCALE):
                with self.subTest(mode=mode, column=m.COLUMNS[col]):
                    brush = self._text(win, 0, col, Qt.ItemDataRole.ForegroundRole)
                    self.assertIsNotNone(brush)
                    self.assertGreaterEqual(_contrast(_over(brush, card), card), 4.5)

    # -- Q-13 ----------------------------------------------------------

    def test_reviewed_shows_the_same_dot_sent_does(self):
        from gui import image_table_model as m
        sent = ImageEntry(path="/tmp/s.png", sent_to_hydrus=True, hydrus_import_confirmed=True)
        reviewed = ImageEntry(path="/tmp/r.png", reviewed=True)
        win = self._window([sent, reviewed])
        self.assertEqual(self._text(win, 1, m.COL_REVIEWED), "\u25cf")
        sent_key = self._text(win, 0, m.COL_SENT, m.CHIP_ROLE)
        reviewed_key = self._text(win, 1, m.COL_REVIEWED, m.CHIP_ROLE)
        self.assertEqual(theme.status_glyph(reviewed_key), theme.status_glyph(sent_key))
        self.assertEqual(theme.status_glyph(reviewed_key), "\u25cf")
        self.assertIsNone(self._text(win, 0, m.COL_REVIEWED, m.CHIP_ROLE))

    def test_the_reviewed_dot_is_painted_by_the_chip_delegate(self):
        from gui import image_table_model as m
        win = self._window([ImageEntry(path="/tmp/r.png", reviewed=True)])
        spy, _image = self._paint(win, 0, m.COL_REVIEWED)
        self.assertTrue(spy.fonts, "nothing was drawn for a reviewed row")

    # -- Q-08 ----------------------------------------------------------

    def test_status_label_is_mono_11px_and_its_glyph_13px(self):
        from gui import image_table_model as m
        win = self._window([ImageEntry(path="/tmp/g.png", status=MatchStatus.GOOD)])
        spy, _image = self._paint(win, 0, m.COL_STATUS)
        sizes = [f.pixelSize() for f in spy.fonts]
        self.assertIn(11, sizes, "the status label is not 11px")
        self.assertIn(13, sizes, "the status glyph is not 13px")
        label_font = next(f for f in spy.fonts if f.pixelSize() == 11)
        self.assertEqual(label_font.families()[0], "JetBrains Mono")

    # -- Q-09 ----------------------------------------------------------

    def test_error_is_boxed_and_no_other_status_is(self):
        from gui import image_table_model as m
        win = self._window([ImageEntry(path="/tmp/e.png", status=status)
                            for status in MatchStatus])
        for row, status in enumerate(MatchStatus):
            spy, _image = self._paint(win, row, m.COL_STATUS)
            with self.subTest(status=status.value):
                if status is MatchStatus.ERROR:
                    self.assertEqual(len(spy.rects), 1, "Error has no box")
                else:
                    self.assertEqual(spy.rects, [])

    def test_the_error_box_encloses_the_chip_and_clears_the_border_floor(self):
        from gui import image_table_model as m
        for mode in ("dark", "light"):
            win = self._window([ImageEntry(path="/tmp/e.png", status=MatchStatus.ERROR)], mode)
            spy, _image = self._paint(win, 0, m.COL_STATUS)
            (box, border), = spy.rects
            with self.subTest(mode=mode):
                cell = QRectF(0, 0, self.CELL_W, self.CELL_H)
                self.assertTrue(cell.contains(box), "the box leaves the cell")
                label = QFontMetrics(spy.fonts[-1]).horizontalAdvance("Error")
                self.assertGreater(box.width(), label + 2 * 6, "the box does not wrap the label")
                bg = theme.palette(mode)["card"]
                self.assertGreaterEqual(
                    _contrast(_over(theme.ink_color(mode, "ink_46"), bg), bg), 3.0)
                self.assertEqual(border, theme.ink_color(mode, "ink_46").name())

    def test_a_selected_error_box_takes_the_highlighted_text_colour(self):
        from gui import image_table_model as m
        win = self._window([ImageEntry(path="/tmp/e.png", status=MatchStatus.ERROR)])
        spy, _image = self._paint(win, 0, m.COL_STATUS, selected=True)
        (_box, border), = spy.rects
        highlighted = win.table.palette().color(QPalette.ColorRole.HighlightedText).name()
        self.assertEqual(border, highlighted)


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
