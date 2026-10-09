"""Flat surfaces and numbered section labels (DAN-1160, gap rows G-08, G-11,
Y-01, Q-01 label, V-02 label).

The mockup's page is flat: regions are 1px hairlines, not filled cards, and
every section carries a mono, tracked, dim `NN // SECTION` label. Nothing
asserted either, and production drifted to a filled card nested three deep
(page card > table card > filter band). These tests measure the *painted*
pixels of a real, themed MainWindow in both modes, not the stylesheet text,
so a later rule that quietly repaints a card still fails here.
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QFont, QImage
from PyQt6.QtWidgets import QFrame, QLabel

from core.models import ImageEntry, MatchStatus
from gui import theme
from tests.test_gui_harness import make_themed_window
from tests.test_gui_smoke import GuiTestCase
from tests.test_theme import (
    _composite_over, _contrast_ratio, _parse_hex, _parse_rgba,
)

MODES = ("dark", "light")
# A painted pixel is compared to a composited ink tier; Qt rounds the
# blend differently from the test's own arithmetic by a step or two.
TOLERANCE = 3
# WCAG 1.4.3: a section label is text, so it clears the body-text floor.
MIN_TEXT_CONTRAST = 4.5


def _rgb(image, x, y):
    c = image.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def _near(a, b, tol=TOLERANCE):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def _hairline(mode, over="page"):
    """What `ink_18` paints as over `over` (a palette key) - the region
    border colour. A box with its own fill, like the table, draws its
    border over that fill rather than over the page."""
    p = theme.palette(mode)
    rgb, alpha = _parse_rgba(p["ink_18"])
    return _composite_over(rgb, alpha, _parse_hex(p[over]))


class _WindowCase(GuiTestCase):
    def _window(self, mode):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # Real files: a list pointing at nothing makes the window raise a
        # modal "files are missing" box, which blocks an offscreen run.
        entries = []
        for i in range(3):
            path = os.path.join(tmp.name, f"img_{i}.png")
            image = QImage(8, 8, QImage.Format.Format_RGB32)
            image.fill(0x336699)
            image.save(path)
            entries.append(ImageEntry(path=path, status=MatchStatus.NOT_SEARCHED))
        win.entries[:] = entries
        win.table_model.refresh_all(win.entries)
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        return win

    @staticmethod
    def _shot(win):
        from PyQt6.QtWidgets import QApplication
        for _ in range(3):
            QApplication.processEvents()
        return win.grab().toImage()

    @staticmethod
    def _at(win, widget, x, y):
        """The window-space point for (x, y) inside `widget`."""
        return widget.mapTo(win, QPoint(x, y))

    @staticmethod
    def _card_of(widget):
        while widget is not None and widget.objectName() != "Card":
            widget = widget.parentWidget()
        return widget


class TestRegionsAreFlatHairlines(_WindowCase):
    """G-08: no filled card behind the page, table or filter band."""

    def test_queue_page_card_paints_the_page_not_a_fill(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                page = _parse_hex(theme.palette(mode)["page"])
                card = self._card_of(win.table)
                self.assertIsNotNone(card, "the Queue table lost its Card")
                img = self._shot(win)
                # The top-right corner of the card holds no widget of its own.
                for x, y in ((card.width() - 2, 1), (0, 0)):
                    p = self._at(win, card, x, y)
                    self.assertTrue(
                        _near(_rgb(img, p.x(), p.y()), page),
                        f"{mode}: Queue card paints {_rgb(img, p.x(), p.y())} "
                        f"at ({x},{y}), expected the page {page}",
                    )

    def test_filter_band_is_a_transparent_hairline(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                page = _parse_hex(theme.palette(mode)["page"])
                band = win.filter_bar
                img = self._shot(win)
                mid = band.height() // 2
                inside = self._at(win, band, 6, mid)
                edge = self._at(win, band, 0, mid)
                self.assertTrue(
                    _near(_rgb(img, inside.x(), inside.y()), page),
                    f"{mode}: the filter band is filled "
                    f"({_rgb(img, inside.x(), inside.y())}), not the page {page}",
                )
                self.assertTrue(
                    _near(_rgb(img, edge.x(), edge.y()), _hairline(mode)),
                    f"{mode}: the filter band edge paints "
                    f"{_rgb(img, edge.x(), edge.y())}, expected the ink_18 "
                    f"hairline {_hairline(mode)}",
                )

    def test_table_keeps_its_own_fill_inside_a_hairline(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                palette = theme.palette(mode)
                fill = _parse_hex(palette["card"])
                img = self._shot(win)
                viewport = win.table.viewport()
                # Below the three rows: nothing but the viewport's own fill.
                p = self._at(win, viewport, viewport.width() - 6, viewport.height() - 6)
                self.assertTrue(
                    _near(_rgb(img, p.x(), p.y()), fill),
                    f"{mode}: table viewport paints {_rgb(img, p.x(), p.y())}, "
                    f"expected its own fill {fill}",
                )
                edge = self._at(win, win.table, 0, win.table.height() // 2)
                self.assertTrue(
                    _near(_rgb(img, edge.x(), edge.y()), _hairline(mode, "card")),
                    f"{mode}: the table edge paints {_rgb(img, edge.x(), edge.y())}, "
                    f"expected the ink_18 hairline {_hairline(mode, 'card')}",
                )

    def test_run_strip_is_a_hairline_on_the_flat_page(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                page = _parse_hex(theme.palette(mode)["page"])
                strip = win.run_strip
                img = self._shot(win)
                mid = strip.height() // 2
                inside = self._at(win, strip, 6, mid)
                edge = self._at(win, strip, 0, mid)
                self.assertTrue(_near(_rgb(img, inside.x(), inside.y()), page))
                self.assertTrue(_near(_rgb(img, edge.x(), edge.y()), _hairline(mode)))

    def test_review_columns_paint_the_page(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                win.set_mode("review")
                page = _parse_hex(theme.palette(mode)["page"])
                img = self._shot(win)

                compare = self._card_of(win.review_view_switch)
                decision = self._card_of(win.review_mark_btn)
                self.assertIsNot(compare, decision)
                self.assertFalse(compare.property("framed"))
                self.assertFalse(decision.property("framed"))

                # Bottom-left of the comparison column; and the gap between
                # the match header and the candidate combo in the rail.
                gap = win.candidate_combo.geometry().top() - 3
                for card, x, y in (
                    (compare, 0, compare.height() - 1),
                    (decision, decision.width() // 2, gap),
                ):
                    p = self._at(win, card, x, y)
                    self.assertTrue(
                        _near(_rgb(img, p.x(), p.y()), page),
                        f"{mode}: a Review column paints {_rgb(img, p.x(), p.y())} "
                        f"at ({x},{y}), expected the page {page}",
                    )

    def test_activity_sections_paint_the_page(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                win.set_mode("activity")
                page = _parse_hex(theme.palette(mode)["page"])
                img = self._shot(win)
                for anchor in (win.engine_pipeline, win.log_view):
                    card = self._card_of(anchor)
                    self.assertIsNotNone(card)
                    self.assertFalse(card.property("framed"))
                    p = self._at(win, card, card.width() - 2, 1)
                    self.assertTrue(
                        _near(_rgb(img, p.x(), p.y()), page),
                        f"{mode}: an Activity section paints {_rgb(img, p.x(), p.y())}, "
                        f"expected the page {page}",
                    )

    def test_section_rule_is_one_pixel_of_hairline(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                win.set_mode("activity")
                img = self._shot(win)
                rules = [f for f in win.findChildren(QFrame)
                         if f.objectName() == "SectionRule"]
                self.assertEqual(len(rules), 2, "Engines and Log each carry a rule")
                for rule in rules:
                    self.assertEqual(rule.height(), 1)
                    p = self._at(win, rule, rule.width() // 2, 0)
                    self.assertTrue(
                        _near(_rgb(img, p.x(), p.y()), _hairline(mode)),
                        f"{mode}: a section rule paints {_rgb(img, p.x(), p.y())}, "
                        f"expected {_hairline(mode)}",
                    )


class TestNumberedSectionLabels(_WindowCase):
    """G-11 / Y-01 / Q-01 / V-02: `NN // SECTION`, mono, tracked, dim."""

    @staticmethod
    def _labels(root):
        return sorted(
            lbl.text() for lbl in root.findChildren(QLabel)
            if lbl.objectName() == "SectionLabel"
        )

    def test_every_labelled_section_carries_its_number(self):
        win = self._window("dark")
        self.assertEqual(
            self._labels(win.queue_stack.parentWidget()),
            ["00 // Queue", "01 // Filter"],
        )
        self.assertEqual(self._labels(win.review_page), ["02 // Compare", "03 // Tags"])
        activity = self._card_of(win.engine_pipeline).parentWidget()
        self.assertEqual(self._labels(activity), ["01 // Engines", "02 // Log"])

    def test_the_serif_headings_and_filter_colon_are_gone(self):
        win = self._window("dark")
        texts = {lbl.text() for lbl in win.findChildren(QLabel)
                 if lbl.objectName() == "Heading"}
        for replaced in ("Engines", "Log", "Tags"):
            self.assertNotIn(replaced, texts, f"'{replaced}' is still a serif heading")
        self.assertNotIn("Filter:", {lbl.text() for lbl in win.findChildren(QLabel)})

    def test_uppercase_comes_from_the_font_not_the_string(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                labels = [lbl for lbl in win.findChildren(QLabel)
                          if lbl.objectName() == "SectionLabel"]
                self.assertGreaterEqual(len(labels), 5)
                for lbl in labels:
                    self.assertEqual(
                        lbl.font().capitalization(), QFont.Capitalization.AllUppercase)
                    self.assertNotEqual(
                        lbl.text(), lbl.text().upper(),
                        f"{lbl.text()!r} was upper-cased in the string",
                    )
                    # Tracked: a percentage spacing above 100.
                    self.assertGreater(lbl.font().letterSpacing(), 100)

    def test_capitalisation_actually_paints_uppercase(self):
        """The font flag has to survive the stylesheet and reach the pixels:
        the label must paint identically to one whose string is already
        upper-case."""
        win = self._window("dark")
        lbl = win.filter_bar.filter_label
        reference = QLabel(lbl.text().upper())
        reference.setObjectName("SectionLabel")
        font = QFont(lbl.font())
        font.setCapitalization(QFont.Capitalization.MixedCase)
        reference.setFont(font)
        self.addCleanup(reference.deleteLater)
        reference.resize(lbl.size())
        reference.ensurePolished()
        self.assertEqual(
            lbl.grab().toImage(), reference.grab().toImage(),
            "the section label does not paint as upper-case",
        )

    def test_section_label_text_clears_body_contrast_on_the_page(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                p = theme.palette(mode)
                page = _parse_hex(p["page"])
                rgb, alpha = _parse_rgba(p["ink_65"])
                ratio = _contrast_ratio(_composite_over(rgb, alpha, page), page)
                self.assertGreaterEqual(
                    ratio, MIN_TEXT_CONTRAST,
                    f"{mode}: dim section label is {ratio:.2f}:1 on the page")


if __name__ == "__main__":
    unittest.main()
