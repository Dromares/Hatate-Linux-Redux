"""The mockup's type voice (DAN-1161, gap rows G-01, G-02, G-03, G-04, G-10,
V-08, E-04, Y-04 casing, Q-01 casing).

Page-level controls are mono, UPPERCASE and tracked; the wordmark is the
kanji 鏡 beside HATATE; the gear is a 36px box; screen titles are 36px.
Qt has no `text-transform`, so uppercase is `QFont.AllUppercase` and the
string itself must stay sentence case (accessible names, tooltips, `.text()`).
These tests read the fonts Qt resolved on a really-themed MainWindow and
render real pixels, so a stylesheet rule that quietly resets a font fails
here instead of in a screenshot.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QPushButton, QToolButton

from core.config import Settings
from gui import shell, theme, widgets
from gui.settings_dialog import SettingsDialog
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase

CAPS = QFont.Capitalization.AllUppercase
MODES = ("dark", "light")


def _font(widget):
    widget.ensurePolished()
    return widget.font()


def _is_caps_mono(widget):
    font = _font(widget)
    return (
        font.capitalization() == CAPS
        and font.letterSpacing() > 100  # PercentageSpacing: tracked
        and font.family() == "JetBrains Mono"
    )


def _buttons(win, *texts):
    found = {}
    for button in win.findChildren(QPushButton):
        if button.text() in texts:
            found[button.text()] = button
    missing = set(texts) - set(found)
    assert not missing, f"no such button(s): {sorted(missing)}"
    return found


class _WindowCase(GuiTestCase):
    def setUp(self):
        # Bare widgets (no window) still need an application, themed and
        # with the bundled fonts, or the fonts under test never resolve.
        from PyQt6.QtWidgets import QApplication
        from gui.fonts import register_fonts
        self.app = QApplication.instance() or QApplication([])
        register_fonts()
        self.app.setStyleSheet(theme.stylesheet("dark"))

    def _window(self, mode="dark"):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        return win


class TestWordmark(_WindowCase):
    """G-01"""

    def test_kanji_mark_is_ryoku_kanji_at_22px(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                glyph = win.findChild(QLabel, "MarkGlyph")
                self.assertIsNotNone(glyph, "the in-window mark is not a kanji label")
                self.assertEqual(glyph.text(), "鏡")
                font = _font(glyph)
                self.assertEqual(font.family(), "Ryoku Kanji")
                self.assertEqual(font.pixelSize(), 22)

    def test_word_is_mono_uppercase_tracked_but_text_stays_sentence_case(self):
        win = self._window()
        word = win.findChild(QLabel, "MarkWord")
        self.assertIsNotNone(word)
        self.assertEqual(word.text(), "Hatate")
        self.assertTrue(_is_caps_mono(word), _font(word).toString())

    def test_mark_keeps_its_accessible_name(self):
        win = self._window()
        word = win.findChild(QLabel, "MarkWord")
        self.assertEqual(word.parentWidget().accessibleName(), "Hatate")

    def test_the_kanji_glyph_exists_in_the_bundled_face(self):
        from PyQt6.QtGui import QFontMetrics
        self._window()
        font = QFont("Ryoku Kanji")
        self.assertTrue(QFontMetrics(font).inFontUcs4(ord(shell.MARK_GLYPH)))


class TestUppercaseVoice(_WindowCase):
    """G-02, G-03, V-08, E-04, Y-04, Q-01 (casing)"""

    def test_mode_tabs(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                for key, label, tip in shell.MODES:
                    button = win.mode_buttons[key]
                    self.assertTrue(_is_caps_mono(button), key)
                    self.assertEqual(button.text(), label)
                    self.assertEqual(button.toolTip(), tip)

    def test_start_search(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                button = win.search_toggle_btn
                self.assertTrue(_is_caps_mono(button))
                self.assertEqual(button.text(), "▶  Start Search")
                self.assertGreaterEqual(button.height(), widgets.BUTTON_MIN_HEIGHT)

    def test_review_page_buttons(self):  # V-08
        win = self._window()
        win.set_mode("review")
        for text, button in _buttons(
            win, "Add tags…", "Remove", "Edit",
        ).items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))
                self.assertGreaterEqual(button.height(), widgets.BUTTON_MIN_HEIGHT)
        for button in win.review_view_buttons.values():
            self.assertTrue(_is_caps_mono(button), button.text())

    def test_activity_page_buttons(self):  # Y-04
        win = self._window()
        win.set_mode("activity")
        for text, button in _buttons(win, "Parser health…", "Clear search cache…").items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))

    def test_empty_queue_buttons(self):  # E-04
        win = self._window()
        win.entries[:] = []
        win.table_model.refresh_all(win.entries)
        win._refresh_queue_empty_state()
        for text, button in _buttons(
            win, "Add files…", "Add folder…", "Query Hydrus…",
        ).items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))

    def test_filter_band_controls(self):  # Q-01 casing
        win = self._window()
        band = win.filter_bar
        for button in (band.filter_status_button, band.filter_site_button,
                       band.filter_upscale_button, band.filter_clear_button):
            with self.subTest(button=button.text()):
                self.assertTrue(_is_caps_mono(button))
        # The string is untouched: the accessible name and any reader of
        # `.text()` get sentence case.
        self.assertEqual(band.filter_status_button.text(), "Status: all")
        self.assertEqual(band.filter_clear_button.text(), "Clear")

    def test_uppercase_is_painted_not_typed(self):
        """A sentence-case button must render pixel-identical to a button
        whose text is already UPPERCASE, in the same font."""
        self._window()
        lower = widgets.pill_button("Add tags…")
        upper = widgets.pill_button("ADD TAGS…")
        for b in (lower, upper):
            b.resize(160, 40)
            b.ensurePolished()
        self.assertEqual(lower.grab().toImage(), upper.grab().toImage())
        # ...and differs from the same text with the voice switched off.
        plain = widgets.pill_button("Add tags…", uppercase=False)
        plain.resize(160, 40)
        self.assertNotEqual(lower.grab().toImage(), plain.grab().toImage())


class TestDialogsKeepSentenceCase(_WindowCase):
    """Ruling C-4 on DAN-1155: dialogs are not in the mockup."""

    def test_opt_out_leaves_the_font_alone(self):
        button = widgets.pill_button("Generate new token", uppercase=False)
        self.assertNotEqual(button.font().capitalization(), CAPS)
        self.assertIsNone(button.property("voice"))

    def test_settings_dialog_helper_buttons(self):
        self._window()
        dialog = SettingsDialog(Settings())
        self.addCleanup(dialog.deleteLater)
        for button in (dialog.mcp_open_log_btn, dialog.mcp_restart_btn):
            self.assertNotEqual(_font(button).capitalization(), CAPS, button.text())
        by_text = {b.text(): b for b in dialog.findChildren(QPushButton)}
        self.assertNotEqual(
            _font(by_text["Generate new token"]).capitalization(), CAPS)
        for button in dialog.mcp_audit_filter_buttons.values():
            self.assertNotEqual(_font(button).capitalization(), CAPS, button.text())
        self.assertFalse(
            [b for b in dialog.findChildren(QToolButton)
             if b.property("voice") == "caps"])


class TestBoxedGear(_WindowCase):
    """G-04"""

    def _gear(self, win):
        for button in win.findChildren(QPushButton):
            if button.text() == "⚙":
                return button
        self.fail("no gear button")

    def test_gear_is_a_36px_square(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gear = self._gear(self._window(mode))
                self.assertEqual((gear.width(), gear.height()), (36, 36))
                self.assertTrue(gear.property("boxed"))
                self.assertEqual(gear.toolTip(), "Preferences…")

    def test_gear_edge_is_painted(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gear = self._gear(self._window(mode))
                img = gear.grab().toImage()
                page = img.pixelColor(18, 18)
                edge = img.pixelColor(0, 18)
                self.assertNotEqual(edge.rgb(), page.rgb(), "no hairline box")

    def test_other_icon_buttons_stay_borderless_and_small(self):
        self._window()
        small = widgets.icon_button("⧉", "Copy")
        self.assertFalse(small.property("boxed"))
        self.assertLess(small.sizeHint().width(), widgets.ICON_BOX + 20)


class TestScreenTitles(_WindowCase):
    """G-10, ruling C-2: serif stays, size grows to 36px."""

    def test_titles_are_source_serif_36px(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                titles = [
                    label for label in win.findChildren(QLabel, "ScreenTitle")
                ]
                self.assertEqual(
                    sorted(t.text() for t in titles), ["Queue.", "Review."])
                for title in titles:
                    font = _font(title)
                    self.assertEqual(font.family(), "Source Serif 4")
                    self.assertEqual(font.pixelSize(), 36)

    def test_stylesheet_rule_matches(self):
        self.assertIn("font-size: 36px", theme.stylesheet("dark").split(
            "QLabel#ScreenTitle")[1].split("}")[0])


if __name__ == "__main__":
    unittest.main()
