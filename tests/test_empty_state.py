"""The empty Queue (DAN-1164 / P11a, gap rows E-01, E-02, E-03).

Every claim in the acceptance list is measured on a real, themed
MainWindow at the mockup's 1440x900, in both modes: the heading's face,
size and full stop, the bracketed full-height frame, where the headline
sits, and that the actions read in the uppercase mono voice.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

from core.models import ImageEntry, MatchStatus
from gui import shell, theme, widgets
from tests.test_flat_surfaces import MODES, _near, _rgb
from tests.test_ghost_kanji import _ink_over
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _composite_over, _parse_hex, _parse_rgba

# The mockup's headline top, as a share of the window: empty-1440.png has the
# 36px headline's line box starting at y=336 of 900.
HEADLINE_TOP_SHARE = 0.37
HEADLINE_TOP_TOLERANCE = 0.02


class _EmptyWindow(GuiTestCase):
    def _window(self, mode):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        for _ in range(3):
            QApplication.processEvents()
        return win

    @staticmethod
    def _zone(win):
        return win.queue_stack.currentWidget()

    @staticmethod
    def _title(win):
        zone = win.queue_stack.currentWidget()
        return zone.findChild(QLabel, "DisplayTitle")


class TestHeading(_EmptyWindow):
    """E-01: `Drop images here.` in the serif at the 36px display size."""

    def test_the_heading_is_serif_36px_with_the_full_stop(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                self.assertEqual(win.queue_stack.currentIndex(), 1)
                title = self._title(win)
                self.assertEqual(title.text(), "Drop images here.")
                title.ensurePolished()
                font = title.font()
                self.assertEqual(font.family(), "Source Serif 4")
                self.assertEqual(font.pixelSize(), 36)

    def test_it_is_not_a_second_screen_title(self):
        """`ScreenTitle` stays "the first thing on a page": Queue. / Review."""
        win = self._window("dark")
        titles = win.findChildren(QLabel, "ScreenTitle")
        self.assertEqual(sorted(t.text() for t in titles), ["Queue.", "Review."])


class TestFrame(_EmptyWindow):
    """E-02: a full-height hairline frame with the P10 brackets."""

    def test_the_label_is_a_numbered_section_label(self):
        win = self._window("dark")
        label = win.queue_section_label
        self.assertEqual(label.objectName(), "SectionLabel")
        self.assertEqual(label.text(), "01 // Queue")  # R-4: contiguous per page
        self.assertEqual(label.font().capitalization(),
                         QFont.Capitalization.AllUppercase)
        self.assertIs(label.parentWidget(), self._zone(win))

    def test_the_frame_fills_the_page_height(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                zone = self._zone(win)
                self.assertEqual(zone.size(), win.queue_stack.size())
                # A "full-height" frame: it runs to the bottom of the page,
                # down to the status bar, and is most of the window.
                bottom = zone.mapTo(win, zone.rect().bottomLeft()).y()
                self.assertGreaterEqual(
                    bottom, win.height() - win.statusBar().height() - 24)
                self.assertGreater(zone.height() / win.height(), 0.65)

    def test_the_frame_is_a_hairline_with_corner_brackets(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                zone = self._zone(win)
                brackets = zone.findChild(widgets.Brackets)
                self.assertIsNotNone(brackets, "no bracket overlay on the frame")
                self.assertEqual(brackets.geometry(), zone.rect())
                img = win.grab().toImage()
                origin = zone.mapTo(win, zone.rect().topLeft())
                x0, y0 = origin.x(), origin.y()
                w, h = zone.width(), zone.height()
                ink = _ink_over(mode, "ink_65", over="card")
                # Inside the hairline, on each arm of each corner.
                for dx, dy in ((1, 1), (11, 1), (1, 11),
                               (w - 2, 1), (w - 12, 1), (w - 2, 11),
                               (1, h - 2), (11, h - 2), (1, h - 12),
                               (w - 2, h - 2), (w - 12, h - 2), (w - 2, h - 12)):
                    got = _rgb(img, x0 + dx, y0 + dy)
                    self.assertTrue(
                        _near(got, ink), f"{mode}: ({dx},{dy}) {got} != bracket {ink}")
                # Between the brackets the frame is the plain hairline.
                p = theme.palette(mode)
                rgb, alpha = _parse_rgba(p["ink_18"])
                hair = _composite_over(rgb, alpha, _parse_hex(p["card"]))
                for dx, dy in ((w // 2, 0), (0, h // 2), (w - 1, h // 2),
                               (w // 2, h - 1)):
                    got = _rgb(img, x0 + dx, y0 + dy)
                    self.assertTrue(
                        _near(got, hair), f"{mode}: ({dx},{dy}) {got} != hairline {hair}")

    def test_the_brackets_do_not_swallow_the_actions(self):
        win = self._window("dark")
        zone = self._zone(win)
        for button in zone.findChildren(QPushButton):
            centre = button.rect().center()
            self.assertIs(
                zone.childAt(button.mapTo(zone, centre)), button, button.text())


class TestTopWeighted(_EmptyWindow):
    """E-03: the headline's top sits at ~37% of the window height."""

    def test_the_headline_is_about_37_percent_down(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                title = self._title(win)
                top = title.mapTo(win, title.rect().topLeft()).y()
                share = top / win.height()
                self.assertAlmostEqual(
                    share, HEADLINE_TOP_SHARE, delta=HEADLINE_TOP_TOLERANCE,
                    msg=f"{mode}: headline top at {top}px = {share:.3f}")

    def test_the_block_is_above_the_centre_of_its_frame(self):
        win = self._window("dark")
        zone = self._zone(win)
        title = self._title(win)
        block_mid = title.mapTo(zone, title.rect().center()).y()
        self.assertLess(block_mid, zone.height() * 0.45)


class TestActions(_EmptyWindow):
    """E-04 rides on P1; this pins that the drop zone still gets it."""

    def test_the_actions_are_uppercase_mono(self):
        win = self._window("dark")
        buttons = self._zone(win).findChildren(QPushButton)
        self.assertEqual([b.text() for b in buttons],
                         ["Add files…", "Add folder…", "Query Hydrus…"])
        for button in buttons:
            button.ensurePolished()
            self.assertEqual(button.font().capitalization(),
                             QFont.Capitalization.AllUppercase, button.text())

    def test_the_actions_are_12px_apart(self):  # DAN-1275: `--space-3`, not Qt's 6
        win = self._window("dark")
        buttons = self._zone(win).findChildren(QPushButton)
        for left, right in zip(buttons, buttons[1:], strict=False):
            gap = (right.mapTo(win, right.rect().topLeft()).x()
                   - left.mapTo(win, left.rect().topRight()).x() - 1)
            self.assertEqual(gap, 12, (left.text(), right.text()))


class TestEmptyGhost(_EmptyWindow):
    """G-07's last piece: the Empty page's own watermark."""

    def test_the_empty_queue_wears_the_empty_glyph(self):
        win = self._window("dark")
        self.assertEqual(win.ghost.glyph(), shell.EMPTY_GHOST_GLYPH)

    def test_a_list_brings_the_queue_glyph_back_and_clearing_it_goes_again(self):
        win = self._window("dark")
        path = os.path.join(os.path.dirname(__file__), "_path.py")
        win.entries.append(ImageEntry(path=path, status=MatchStatus.NOT_SEARCHED))
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        self.assertEqual(win.ghost.glyph(), shell.GHOST_GLYPHS["queue"])
        win.entries.clear()
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        self.assertEqual(win.ghost.glyph(), shell.EMPTY_GHOST_GLYPH)

    def test_the_other_modes_keep_their_own_glyph_while_the_list_is_empty(self):
        win = self._window("dark")
        for key in ("review", "activity"):
            win.set_mode(key)
            self.assertEqual(win.ghost.glyph(), shell.GHOST_GLYPHS[key], key)
        win.set_mode("queue")
        self.assertEqual(win.ghost.glyph(), shell.EMPTY_GHOST_GLYPH)


if __name__ == "__main__":
    unittest.main()
