"""Review's keycaps: the KeyCap widget, the keys inside the buttons, and the
bottom hint bar (DAN-1175, gap rows V-10 and V-11).

Qt's rich text cannot draw a bordered inline span, so a key name in a box is
a widget (`widgets.KeyCap`). These tests hold the three places it lands:
`U`/`D`/`O`/`Space`/`Return`/`J`/`K`/`N` inside the buttons, `[` / `]` beside
the match steppers, and the hint bar along the foot of the page.

Every geometric or contrast claim is measured on the real, themed window at
1440x900 (`make_themed_window` builds under the live stylesheet, which is the
only way a size means anything - see tests/test_gui_harness.py). The contrast
numbers are read off the rendered pixels, not derived from the palette.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtGui import QFontMetrics, QImage
from PyQt6.QtWidgets import QApplication, QFrame

from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
from gui import widgets
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import _contrast_ratio

MODES = ("dark", "light")
MIN_TEXT_CONTRAST = 4.5     # a key name is text
DEFAULT_KEYS = {"send": "Return", "url": "U", "download": "D", "mark": "Space",
                "open": "O", "prev": "K", "next_unreviewed": "N", "next": "J"}


def _rgb(color):
    return color.red(), color.green(), color.blue()


def _strongest_contrast(image, rect, fill):
    best = 1.0
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            best = max(best, _contrast_ratio(_rgb(image.pixelColor(x, y)), fill))
    return best


class _ReviewCase(GuiTestCase):
    def setUp(self):
        patcher = mock.patch("gui.main_window.message.information")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _review_window(self, mode="dark", candidates=3, reviewed=False, rows=1):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        entries = []
        for n in range(rows):
            path = os.path.join(tmp.name, f"img{n}.png")
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0x336699)
            image.save(path)
            entry = ImageEntry(path=path, status=MatchStatus.GOOD)
            entry.candidates = [
                MatchCandidate(url=f"https://danbooru.donmai.us/posts/{k}",
                               similarity=96.0 - k, source_name="Danbooru", engine="IQDB")
                for k in range(candidates)]
            if candidates:
                entry.select_candidate(0)
            entry.tags = [Tag("solo", TagSource.BOORU)]
            entry.reviewed = reviewed
            entries.append(entry)
        win.entries[:] = entries
        win.table_model.refresh_all(win.entries)
        win.resize(1440, 900)
        win.show()
        win.set_mode("review")
        win.select_table_row(0)
        QApplication.processEvents()
        QApplication.processEvents()
        return win

    @staticmethod
    def _keyed(win):
        return {"send": win.review_send_btn, "url": win.review_url_btn,
                "download": win.review_download_btn, "mark": win.review_mark_btn,
                "open": win.review_open_btn, "prev": win.review_prev_btn,
                "next_unreviewed": win.review_next_unreviewed_btn,
                "next": win.review_next_btn}


class TestKeyCapWidget(GuiTestCase):
    def setUp(self):
        # Held on the instance: PyQt destroys a QApplication it created when
        # the last Python reference goes (DAN-1351), and these tests make bare
        # widgets with no window harness to own one.
        self.app = QApplication.instance() or QApplication([])

    def test_text_is_the_key_as_given_and_the_capitals_are_the_fonts(self):
        cap = widgets.KeyCap("Return")
        self.assertEqual(cap.text(), "Return")
        self.assertEqual(cap.objectName(), "KeyCap")
        self.assertEqual(cap.font().capitalization(),
                         cap.font().Capitalization.AllUppercase)

    def test_a_cap_keeps_its_own_height_inside_a_taller_row(self):
        from PyQt6.QtWidgets import QHBoxLayout, QWidget
        row = QWidget()
        lay = QHBoxLayout(row)
        cap = widgets.KeyCap("J")
        lay.addWidget(cap)
        row.resize(200, 60)
        row.show()
        QApplication.processEvents()
        self.assertEqual(cap.height(), cap.sizeHint().height())
        self.assertLess(cap.height(), 30)

    def test_keyed_button_keeps_its_label_as_text_and_exposes_the_cap(self):
        button = widgets.keyed_button("Send URL", "U")
        self.assertEqual(button.text(), "Send URL")
        self.assertEqual(button.key(), "U")
        self.assertIs(button.keycap.parentWidget(), button)

    def test_an_empty_key_hides_the_cap_and_gives_the_room_back(self):
        button = widgets.keyed_button("Send URL", "Space")
        with_cap = button.sizeHint().width()
        button.set_key("")
        self.assertTrue(button.keycap.isHidden())
        self.assertLess(button.sizeHint().width(), with_cap)
        button.set_key("U")
        self.assertFalse(button.keycap.isHidden())
        self.assertEqual(button.sizeHint().width(), widgets.keyed_button("Send URL", "U").sizeHint().width())


class TestKeysInsideTheButtons(_ReviewCase):
    def test_every_button_that_has_a_key_shows_it_as_a_cap(self):
        win = self._review_window()
        for name, button in self._keyed(win).items():
            with self.subTest(button=name):
                self.assertEqual(button.key(), DEFAULT_KEYS[name])
                self.assertTrue(button.keycap.isVisible())
        # the old "(U)" suffix is gone from every label
        for button in self._keyed(win).values():
            self.assertNotIn("(", button.text())

    def test_each_cap_sits_inside_its_button_clear_of_the_label(self):
        for mode in MODES:
            win = self._review_window(mode)
            for name, button in self._keyed(win).items():
                with self.subTest(mode=mode, button=name):
                    cap = button.keycap
                    self.assertTrue(button.rect().contains(cap.geometry()),
                                    f"{name}: cap {cap.geometry()} is outside {button.rect()}")
                    # Where the label's text ends: the font's advance for it, which
                    # includes the mono-caps tracking the stylesheet cannot express.
                    advance = QFontMetrics(button.font()).horizontalAdvance(button.text().upper())
                    text_left = button.style().pixelMetric(button.style().PixelMetric.PM_ButtonMargin)
                    self.assertGreaterEqual(
                        cap.x(), text_left + advance,
                        f"{name}: the cap starts at {cap.x()} but the label runs to "
                        f"{text_left + advance}")
                    self.assertGreaterEqual(button.width(), button.sizeHint().width())

    def test_key_names_read_against_the_surface_they_sit_on(self):
        """The Send button's cap was ink-on-ink - invisible - until it took the
        stamp's text colour. Measured off the pixels, both themes."""
        for mode in MODES:
            win = self._review_window(mode)
            image = win.grab().toImage()
            for name, button in self._keyed(win).items():
                with self.subTest(mode=mode, button=name):
                    cap = button.keycap
                    top_left = cap.mapTo(win, cap.rect().topLeft())
                    inner = cap.rect().adjusted(3, 3, -3, -3)
                    inner.translate(top_left)
                    # the fill is whatever the button paints just left of the cap
                    probe = button.mapTo(win, button.rect().center())
                    fill_x = top_left.x() - 6
                    fill = _rgb(image.pixelColor(fill_x, probe.y()))
                    if button.isEnabled():
                        self.assertGreaterEqual(
                            _strongest_contrast(image, inner, fill), MIN_TEXT_CONTRAST,
                            f"{name} ({mode}): the key name does not read on its button")

    def test_the_mark_label_flip_still_fits_at_the_cards_declared_minimum(self):
        """'Unmark reviewed' is the wider label. The card's floor used to be
        measured with 'Mark reviewed' showing, so the first reviewed row
        clipped it (the leading letters were cut off) - and now its cap too."""
        win = self._review_window(reviewed=True)
        self.assertEqual(win.review_mark_btn.text(), "Unmark reviewed")
        card = win.review_mark_btn.parentWidget()
        while card is not None and card.objectName() != "Card":
            card = card.parentWidget()
        width = card.minimumWidth()
        card.setMaximumWidth(width)
        card.resize(width, card.height())
        QApplication.processEvents()
        QApplication.processEvents()
        for button in (win.review_mark_btn, win.review_open_btn):
            with self.subTest(button=button.text()):
                self.assertGreaterEqual(button.width(), button.sizeHint().width())
        self.assertTrue(win.review_mark_btn.rect().contains(win.review_mark_btn.keycap.geometry()))

    def test_rebinding_a_key_changes_the_cap_and_unbinding_hides_it(self):
        win = self._review_window()
        win.settings.review_shortcuts = {"send_url": "F7", "open_match": ""}
        win._refresh_review_strip()
        self.assertEqual(win.review_url_btn.key(), "F7")
        self.assertTrue(win.review_open_btn.keycap.isHidden())
        win.settings.review_shortcuts = {}
        win._refresh_review_strip()
        self.assertEqual(win.review_url_btn.key(), "U")
        self.assertFalse(win.review_open_btn.keycap.isHidden())


class TestMatchStepperKeys(_ReviewCase):
    def test_bracket_keys_follow_their_steppers_and_dim_with_them(self):
        win = self._review_window()
        self.assertEqual(win.review_prev_match_key.text(), "[")
        self.assertEqual(win.review_next_match_key.text(), "]")
        nav = win.review_match_nav
        self.assertIs(win.review_prev_match_key.parentWidget(), nav)
        # at the first of three: [ is disabled with its stepper, ] is live
        self.assertFalse(win.review_prev_match_key.isEnabled())
        self.assertTrue(win.review_next_match_key.isEnabled())
        win.review_next_match_btn.click()
        QApplication.processEvents()
        self.assertTrue(win.review_prev_match_key.isEnabled())

    def test_a_cap_sits_immediately_after_its_stepper(self):
        win = self._review_window()
        for button, cap in ((win.review_prev_match_btn, win.review_prev_match_key),
                            (win.review_next_match_btn, win.review_next_match_key)):
            gap = cap.x() - (button.x() + button.width())
            self.assertGreaterEqual(gap, 0)
            self.assertLessEqual(gap, 16)


class TestHintBar(_ReviewCase):
    def test_default_bar_reads_like_the_mockups(self):
        win = self._review_window(rows=3)
        bar = win.review_hint_bar
        self.assertEqual(
            bar.plain_text(),
            "1 of 3 · 3 still to review · J / K move · N next unreviewed "
            "· Space marks · C compares")
        caps = [c.text() for c in bar.findChildren(widgets.KeyCap)]
        self.assertEqual(caps, ["J", "K", "N", "Space", "C"])

    def test_the_figures_are_strong_and_the_words_are_not(self):
        win = self._review_window(rows=3)
        strong = [label.text() for label in win.review_hint_bar.findChildren(widgets.QLabel)
                  if label.objectName() == "HintBarText" and label.property("strong")]
        self.assertEqual(strong, ["1 of 3", "3"])

    def test_the_bar_carries_the_mockups_1px_rule_across_the_page(self):
        win = self._review_window()
        rule = next(f for f in win.review_page.findChildren(QFrame)
                    if f.objectName() == "FooterRule" and f.parentWidget() is win.review_page)
        self.assertEqual(rule.height(), 1)
        self.assertEqual(rule.width(), win.review_page.width())
        strip = win.review_hint_bar.parentWidget()
        self.assertGreater(strip.mapTo(win.review_page, strip.rect().topLeft()).y(), rule.y())
        # and nothing sits between the rule and the footer row
        self.assertEqual(rule.y() + 1 + widgets.PAGE_SPACING,
                         strip.mapTo(win.review_page, strip.rect().topLeft()).y())

    def test_the_bar_is_centred_between_the_previous_and_next_buttons(self):
        win = self._review_window(rows=3)
        bar, prev, nxt = win.review_hint_bar, win.review_prev_btn, win.review_next_btn
        self.assertGreaterEqual(bar.x(), prev.x() + prev.width())
        self.assertLessEqual(bar.x() + bar.width(), win.review_next_unreviewed_btn.x())
        shown = [w for w in bar.findChildren(widgets.KeyCap) if w.isVisible()]
        left = min(w.mapTo(bar, w.rect().topLeft()).x() for w in shown)
        self.assertGreater(left, 0)
        self.assertIsNotNone(nxt)

    def test_a_narrow_bar_drops_its_tail_and_keeps_the_position(self):
        win = self._review_window(rows=3)
        bar = win.review_hint_bar
        self.assertEqual(bar.visible_group_count(), 6)
        bar.setFixedWidth(260)
        QApplication.processEvents()
        kept = bar.visible_group_count()
        self.assertGreaterEqual(kept, 1)
        self.assertLess(kept, 6)
        # the first group is always the position, whatever else is cut
        first = bar._parts[0][1]
        self.assertFalse(first.isHidden())
        last = bar._parts[-1][1]
        self.assertTrue(last.isHidden())
        # what is shown fits: nothing is clipped at the right edge
        shown_right = max(group.mapTo(bar, group.rect().bottomRight()).x()
                          for _, group in bar._parts if not group.isHidden())
        self.assertLessEqual(shown_right, bar.width())
        bar.setFixedWidth(1200)
        QApplication.processEvents()
        self.assertEqual(bar.visible_group_count(), 6)

    def test_a_rebound_key_is_what_the_bar_shows(self):
        win = self._review_window(rows=3)
        win.settings.review_shortcuts = {"next_row": "F7", "compare": ""}
        win._refresh_review_strip()
        caps = [c.text() for c in win.review_hint_bar.findChildren(widgets.KeyCap)]
        self.assertIn("F7", caps)
        self.assertNotIn("C", caps)
        self.assertNotIn("compares", win.review_hint_bar.plain_text())

    def test_the_bar_updates_as_the_selection_moves(self):
        win = self._review_window(rows=3)
        win.select_table_row(1)
        QApplication.processEvents()
        self.assertTrue(win.review_hint_bar.plain_text().startswith("2 of 3"))


if __name__ == "__main__":
    unittest.main()
