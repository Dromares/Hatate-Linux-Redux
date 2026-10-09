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


class _Shim:
    """Collects the cleanups `make_themed_window` registers, so a window can
    be built once per class instead of once per test."""

    def __init__(self):
        self.cleanups = []

    def addCleanup(self, fn, *a, **kw):
        self.cleanups.append((fn, a, kw))

    def run_cleanups(self):
        while self.cleanups:
            fn, a, kw = self.cleanups.pop()
            fn(*a, **kw)


class _PerModeWindow:
    """One real, themed MainWindow per mode, shared by every test in the
    class. Building one per test (and re-theming the whole application each
    time) re-polishes every widget earlier tests leaked, which stalled the
    full suite; the claims here are read-only, so sharing is safe."""

    MODE = "dark"

    @classmethod
    def setUpClass(cls):
        cls._shim = _Shim()
        _app, cls.win = make_themed_window(cls._shim, cls.MODE)
        cls.win.resize(1440, 900)
        cls.win.show()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls._shim.run_cleanups()

    def setUp(self):
        self.win.set_mode("queue")


class _WordmarkChecks:
    """G-01"""

    def test_kanji_mark_is_ryoku_kanji_at_22px(self):
        glyph = self.win.findChild(QLabel, "MarkGlyph")
        self.assertIsNotNone(glyph, "the in-window mark is not a kanji label")
        self.assertEqual(glyph.text(), "鏡")
        font = _font(glyph)
        self.assertEqual(font.family(), "Ryoku Kanji")
        self.assertEqual(font.pixelSize(), 22)

    def test_word_is_mono_uppercase_tracked_but_text_stays_sentence_case(self):
        word = self.win.findChild(QLabel, "MarkWord")
        self.assertIsNotNone(word)
        self.assertEqual(word.text(), "Hatate")
        self.assertTrue(_is_caps_mono(word), _font(word).toString())

    def test_mark_keeps_its_accessible_name(self):
        word = self.win.findChild(QLabel, "MarkWord")
        self.assertEqual(word.parentWidget().accessibleName(), "Hatate")


class _UppercaseChecks:
    """G-02, G-03, V-08, E-04, Y-04, Q-01 (casing)"""

    def test_mode_tabs(self):
        for key, label, tip in shell.MODES:
            button = self.win.mode_buttons[key]
            self.assertTrue(_is_caps_mono(button), key)
            self.assertEqual(button.text(), label)
            self.assertEqual(button.toolTip(), tip)

    def test_start_search(self):
        button = self.win.search_toggle_btn
        self.assertTrue(_is_caps_mono(button))
        self.assertEqual(button.text(), "▶  Start Search")
        self.assertGreaterEqual(button.height(), widgets.BUTTON_MIN_HEIGHT)

    def test_review_page_buttons(self):  # V-08
        self.win.set_mode("review")
        for text, button in _buttons(
            self.win, "Add tags…", "Remove", "Edit",
        ).items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))
                self.assertGreaterEqual(button.height(), widgets.BUTTON_MIN_HEIGHT)
        for button in self.win.review_view_buttons.values():
            self.assertTrue(_is_caps_mono(button), button.text())

    def test_activity_page_buttons(self):  # Y-04
        self.win.set_mode("activity")
        for text, button in _buttons(
            self.win, "Parser health…", "Clear search cache…",
        ).items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))

    def test_empty_queue_buttons(self):  # E-04
        self.win._refresh_queue_empty_state()
        for text, button in _buttons(
            self.win, "Add files…", "Add folder…", "Query Hydrus…",
        ).items():
            with self.subTest(button=text):
                self.assertTrue(_is_caps_mono(button))

    def test_filter_band_controls(self):  # Q-01 casing
        band = self.win.filter_bar
        for button in (band.filter_status_button, band.filter_site_button,
                       band.filter_upscale_button, band.filter_clear_button):
            with self.subTest(button=button.text()):
                self.assertTrue(_is_caps_mono(button))
        # The string is untouched: the accessible name and any reader of
        # `.text()` get sentence case.
        self.assertEqual(band.filter_status_button.text(), "Status: all")
        self.assertEqual(band.filter_clear_button.text(), "Clear")


class _GearChecks:
    """G-04"""

    def _gear(self):
        for button in self.win.findChildren(QPushButton):
            if button.text() == "⚙":
                return button
        self.fail("no gear button")

    def test_gear_is_a_36px_square(self):
        gear = self._gear()
        self.assertEqual((gear.width(), gear.height()), (36, 36))
        self.assertTrue(gear.property("boxed"))
        self.assertEqual(gear.toolTip(), "Preferences…")

    def test_gear_edge_is_painted(self):
        img = self._gear().grab().toImage()
        self.assertNotEqual(
            img.pixelColor(0, 18).rgb(), img.pixelColor(18, 18).rgb(),
            "no hairline box",
        )


class _TitleChecks:
    """G-10, ruling C-2: serif stays, size grows to 36px."""

    def test_titles_are_source_serif_36px(self):
        titles = self.win.findChildren(QLabel, "ScreenTitle")
        self.assertEqual(sorted(t.text() for t in titles), ["Queue.", "Review."])
        for title in titles:
            font = _font(title)
            self.assertEqual(font.family(), "Source Serif 4")
            self.assertEqual(font.pixelSize(), 36)


class _AllChecks(
    _PerModeWindow, _WordmarkChecks, _UppercaseChecks, _GearChecks, _TitleChecks,
):
    pass


class TestDark(_AllChecks, GuiTestCase):
    MODE = "dark"


class TestLight(_AllChecks, GuiTestCase):
    MODE = "light"


class TestBareWidgets(GuiTestCase):
    """Checks that need no window. The app-wide stylesheet is left alone
    except where a test renders pixels."""

    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        from gui.fonts import register_fonts
        self.app = QApplication.instance() or QApplication([])
        register_fonts()

    def test_the_kanji_glyph_exists_in_the_bundled_face(self):
        from PyQt6.QtGui import QFontMetrics
        font = QFont("Ryoku Kanji")
        self.assertTrue(QFontMetrics(font).inFontUcs4(ord(shell.MARK_GLYPH)))

    def test_uppercase_is_painted_not_typed(self):
        """A sentence-case button must render pixel-identical to a button
        whose text is already UPPERCASE, in the same font."""
        self.app.setStyleSheet(theme.stylesheet("dark"))
        self.addCleanup(lambda: self.app.setStyleSheet(""))
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

    def test_other_icon_buttons_stay_borderless_and_small(self):
        small = widgets.icon_button("⧉", "Copy")
        self.assertFalse(small.property("boxed"))
        self.assertGreater(small.maximumWidth(), widgets.ICON_BOX)  # not pinned to a box

    def test_stylesheet_rule_for_titles_is_36px(self):
        self.assertIn("font-size: 36px", theme.stylesheet("dark").split(
            "QLabel#ScreenTitle")[1].split("}")[0])

    # Ruling C-4 on DAN-1155: dialogs are not in the mockup.
    def test_opt_out_leaves_the_font_alone(self):
        button = widgets.pill_button("Generate new token", uppercase=False)
        self.assertNotEqual(button.font().capitalization(), CAPS)
        self.assertIsNone(button.property("voice"))

    def test_settings_dialog_helper_buttons_keep_sentence_case(self):
        # Unthemed on purpose: capitalization is a QFont property, set in
        # Python, and a themed 2,500-line dialog is the slowest thing here.
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


if __name__ == "__main__":
    unittest.main()
