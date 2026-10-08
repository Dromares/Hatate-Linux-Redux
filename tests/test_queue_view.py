"""The Queue page: its empty state, and the chips it draws.

The `MainWindow()` built below is bare/unstyled - fine for behaviour,
but it measures smaller than the real app. Use
`test_gui_harness.make_themed_window()` instead for anything that
measures size.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtWidgets import QApplication

from core.models import ImageEntry, MatchStatus
from gui import theme


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


if __name__ == "__main__":
    unittest.main()
