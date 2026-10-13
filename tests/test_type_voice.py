"""The mockup's type voice (DAN-1161, gap rows G-01, G-02, G-03, G-04, G-10,
V-08, E-04, Y-04 casing, Q-01 casing) and the Activity restyle (DAN-1173: Y-02,
Y-05, Y-07, U1).

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
from tests._qt_app import ensure_app

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton

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

    def test_word_is_11px_the_mockups_text_label(self):  # DAN-1394
        self.assertEqual(_font(self.win.findChild(QLabel, "MarkWord")).pixelSize(), 11)

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
            self.assertEqual(button.sizeHint().height(), 38, key)  # DAN-1275: mockup tab fill
            self.assertEqual(_font(button).pixelSize(), 11, key)  # DAN-1394: --text-label

    def test_mode_group_is_the_mockups_width(self):  # DAN-1394: 296px, was 310
        self.assertAlmostEqual(self.win.mode_switch.sizeHint().width(), 296, delta=3)
        self.assertAlmostEqual(
            self.win.mode_buttons["activity"].sizeHint().width(), 112, delta=3)

    def test_start_search(self):
        button = self.win.search_toggle_btn
        self.assertTrue(_is_caps_mono(button))
        self.assertEqual(button.text(), "▶ Start Search")
        self.assertGreaterEqual(button.height(), widgets.BUTTON_MIN_HEIGHT)
        # DAN-1275: the mockup's button is 160px wide; ours is 160 +/- 4.
        self.assertAlmostEqual(button.sizeHint().width(), 160, delta=4)

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

    def test_filter_band_controls(self):  # Q-01 casing, corrected by DAN-1275
        band = self.win.filter_bar
        # Clear is a command: the page-level mono caps voice.
        self.assertTrue(_is_caps_mono(band.filter_clear_button))
        # The combos show state, not a command: mono and tracked like the
        # mockup's `.field--button`, but sentence case (not AllUppercase).
        for button in (band.filter_status_button, band.filter_site_button,
                       band.filter_upscale_button):
            with self.subTest(button=button.text()):
                font = _font(button)
                self.assertEqual(font.family(), "JetBrains Mono")
                self.assertGreater(font.letterSpacing(), 100)
                self.assertNotEqual(font.capitalization(), CAPS)
                # DAN-1394: --text-label, and the mockup's 32px field.
                self.assertEqual(font.pixelSize(), 11)
                self.assertEqual(button.sizeHint().height(), 32)
        self.assertEqual(band.filter_status_button.text(), "Status: All")
        self.assertEqual(band.filter_clear_button.text(), "Clear")


class _MetaLineChecks:
    """DAN-1394: the run banner's meta line is `--text-micro`, 10px."""

    def test_meta_line_is_10px_mono(self):
        row, labels = widgets.meta_line([("Quota exhausted at", "15:22"), ("Resets", "12:40 UTC")])
        self.addCleanup(row.deleteLater)
        self.assertTrue(labels)
        for label in labels:
            with self.subTest(label=label.text()):
                self.assertEqual(_font(label).pixelSize(), 10)
                self.assertEqual(_font(label).family(), "JetBrains Mono")


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
        self.assertEqual(sorted(t.text() for t in titles), ["Activity.", "Queue.", "Review."])
        for title in titles:
            font = _font(title)
            self.assertEqual(font.family(), "Source Serif 4")
            self.assertEqual(font.pixelSize(), 36)


class _ActivityChecks:
    """Y-02, Y-05, Y-07, U1 (DAN-1173)"""

    def test_engine_chips_are_uppercase_mono_and_keep_their_sentence_case_text(self):
        self.win.set_mode("activity")
        chips = [self.win.engine_chip_row.itemAt(i).widget()
                 for i in range(self.win.engine_chip_row.count())]
        chips = [c for c in chips if c is not None]
        # B-9: the three chips added after the mockup stay.
        self.assertEqual(len(chips), 10)
        for chip in chips:
            with self.subTest(chip=chip.text()):
                self.assertTrue(_is_caps_mono(chip), _font(chip).toString())
                self.assertEqual(_font(chip).pixelSize(), 11)
        self.assertEqual(chips[0].text(), "IQDB · primary")

    def test_chips_wrap_instead_of_widening_the_window(self):
        self.win.set_mode("activity")
        page = self.win.mode_stack.currentWidget()
        # Ten mono-caps chips in one row ask for ~1380px; they have to wrap
        # (the mockup's `flex-wrap`) so the page still fits a 1440 window.
        self.assertLessEqual(
            page.minimumSizeHint().width(), 1440 - 2 * widgets.PAGE_GUTTER)
        row = self.win.engine_chip_row
        QApplication.processEvents()   # the rebuilt chips are shown on the next turn
        self.assertGreater(row.heightForWidth(300), row.heightForWidth(5000))

    def test_log_toolbar_is_three_boxed_36px_buttons(self):
        self.win.set_mode("activity")
        buttons = [self.win.log_follow_btn] + [
            b for b in self.win.findChildren(QPushButton, "IconButton")
            if b.toolTip() in ("Copy the whole log.",
                               "Open the folder the log file is in.")]
        self.assertEqual([b.text() for b in buttons], ["↻", "⧉", "⤢"])
        for button in buttons:
            with self.subTest(button=button.text()):
                self.assertTrue(button.property("boxed"))
                self.assertEqual((button.width(), button.height()), (36, 36))
                # Sample beside the glyph, not through it: the centre pixel
                # can land on a stroke of the border's own ink colour.
                img = button.grab().toImage()
                for y in (9, 18, 27):
                    self.assertNotEqual(
                        img.pixelColor(0, y).rgb(), img.pixelColor(4, y).rgb(),
                        f"no hairline box at y={y}")

    def test_log_toolbar_sits_right_of_the_section_rule(self):
        self.win.set_mode("activity")
        follow = self.win.log_follow_btn
        rule = follow.parentWidget().findChildren(
            widgets.QFrame, "SectionRule")[-1]
        self.assertLess(rule.geometry().right(), follow.geometry().left())

    def test_log_footer_is_10px_mono(self):
        self.win.set_mode("activity")
        footer = self.win.log_footer
        self.assertTrue(footer.text().startswith("Writing to "))
        font = _font(footer)
        self.assertEqual(font.family(), "JetBrains Mono")
        self.assertEqual(font.pixelSize(), 10)

    def test_activity_has_a_title_in_queues_slot(self):  # U1
        self.win.set_mode("activity")
        title = self.win.activity_title
        self.assertEqual(title.text(), "Activity.")
        self.assertTrue(title.isVisible())
        # Same distance from the section below as Queue's title has from its
        # body: its own 4px margin plus the body gap.
        card = self.win.engine_pipeline.parentWidget()
        while card.parentWidget() is not title.parentWidget():
            card = card.parentWidget()
        gap = card.geometry().top() - title.geometry().bottom() - 1
        self.assertEqual(gap, widgets.SCREEN_TITLE_GAP + widgets.QUEUE_BODY_GAP)
        # ...and the title is the first thing on the page, as on Queue.
        self.assertLess(title.geometry().top(), card.geometry().top())


class _AllChecks(
    _PerModeWindow, _WordmarkChecks, _UppercaseChecks, _MetaLineChecks, _GearChecks,
    _TitleChecks, _ActivityChecks,
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
        from gui.fonts import register_fonts
        self.app = ensure_app()
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
