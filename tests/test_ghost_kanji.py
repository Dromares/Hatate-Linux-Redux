"""Ghost kanji watermark and corner brackets (DAN-1163, gap rows G-07 and
the bracket halves of V-03 and E-02).

Both are custom paint, so a stylesheet assertion proves nothing: these
tests measure the pixels of a real, themed MainWindow in both modes. The
ghost only shows through surfaces that paint nothing, so "it works" is
sampled on every page, not asserted once on the page it was built on.
"""
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QApplication, QComboBox, QFrame,
    QLineEdit, QProgressBar, QWidget,
)

from core.models import ImageEntry, MatchStatus
from gui import shell, theme, widgets
from tests.test_flat_surfaces import MODES, _hairline, _near, _rgb, _WindowCase
from tests.test_theme import (
    _composite_over, _contrast_ratio, _parse_hex, _parse_rgba,
)

# A stroke pixel of the ghost pixmap is ink at ~5-8% alpha; this is "has
# ink" with room for antialiased edges to be skipped.
STROKE_ALPHA = 8


def _ink_over(mode, tier, over="page"):
    p = theme.palette(mode)
    rgb, alpha = _parse_rgba(p[tier])
    return _composite_over(rgb, alpha, _parse_hex(p[over]))


class TestGlyphPerMode(_WindowCase):
    def test_each_mode_has_its_own_glyph(self):
        win = self._window("dark")
        for key, glyph in (("queue", "力"), ("review", "鏡"), ("activity", "動")):
            win.set_mode(key)
            self.assertEqual(win.ghost.glyph(), glyph, key)

    def test_the_empty_glyph_is_in_the_bundled_subset(self):
        self.assertEqual(shell.EMPTY_GHOST_GLYPH, "空")
        for glyph in (*shell.GHOST_GLYPHS.values(), shell.EMPTY_GHOST_GLYPH):
            self.assertIn(glyph, "力鏡動空")

    def test_set_ghost_changes_the_glyph(self):
        win = self._window("dark")
        win.set_ghost(shell.EMPTY_GHOST_GLYPH)
        self.assertEqual(win.ghost.glyph(), "空")

    def test_ink_tier_is_about_six_percent(self):
        """tokens.css: 5% bone ink on charcoal, 8% dark ink on paper. The
        brief's "about 6%" is the midpoint; neither mode strays from it."""
        for mode, expected in (("dark", 0.05), ("light", 0.08)):
            _rgb_, alpha = _parse_rgba(theme.palette(mode)["ink_05"])
            self.assertAlmostEqual(alpha, expected, places=3, msg=mode)
            self.assertLessEqual(abs(alpha - 0.06), 0.025)


class TestGhostShowsOnEveryPage(_WindowCase):
    """The ghost is hidden by any filled surface over it. Sample the
    glyph's own strokes on each page: wherever the thing under the point is
    a plain container or a label, the painted pixel must be the page with
    the ghost ink on it."""

    # Controls that carry their own fill by design: they are allowed to
    # hide the glyph, every other surface is not.
    OWN_FILL = (QAbstractButton, QAbstractScrollArea, QComboBox, QLineEdit,
                QProgressBar, widgets.ScaledImageLabel)

    def _eligible(self, widget):
        while widget is not None and widget is not self._ghost:
            if isinstance(widget, self.OWN_FILL):
                return False
            if isinstance(widget.parentWidget(), QAbstractScrollArea):
                return False
            if widget.objectName() in ("DropZone", "TagList"):
                return False
            widget = widget.parentWidget()
        return True

    def _strokes(self, ghost):
        image = ghost._pixmap().toImage().convertToFormat(
            QImage.Format.Format_ARGB32)
        ratio = ghost.devicePixelRatioF()
        box = ghost.ghost_rect()
        points = []
        for y in range(0, image.height(), 6):
            for x in range(0, image.width(), 6):
                if image.pixelColor(x, y).alpha() < STROKE_ALPHA:
                    continue
                pt = QPoint(box.x() + round(x / ratio), box.y() + round(y / ratio))
                if ghost.rect().contains(pt):
                    points.append(pt)
        return points

    def test_the_glyph_shows_wherever_nothing_sits_on_it(self):
        for mode in MODES:
            win = self._window(mode)
            self._ghost = win.ghost
            expected = _ink_over(mode, "ink_05")
            bare = _parse_hex(theme.palette(mode)["page"])
            fills = [bare] + [_parse_hex(theme.palette(mode)[k])
                              for k in ("card", "card_alt")]
            self.assertFalse(_near(expected, bare, 2), "ghost would be invisible")
            for key in ("queue", "review", "activity"):
                with self.subTest(mode=mode, page=key):
                    win.set_mode(key)
                    img = self._shot(win)
                    ghost = win.ghost
                    seen = missing = 0
                    culprits = set()
                    for pt in self._strokes(ghost):
                        under = ghost.childAt(pt)
                        if under is not None and not self._eligible(under):
                            continue
                        win_pt = ghost.mapTo(win, pt)
                        got = _rgb(img, win_pt.x(), win_pt.y())
                        if _near(got, expected):
                            seen += 1
                        elif any(_near(got, flat) for flat in fills):
                            # A flat fill painted over the glyph: hidden.
                            missing += 1
                            culprits.add(type(under).__name__ + ":" +
                                         (under.objectName() if under else ""))
                        # Anything else is text or a hairline crossing a
                        # stroke - not a fill, so not hiding the glyph.
                    self.assertGreater(seen + missing, 150,
                                       "too few open stroke points to mean anything")
                    # A glyph under a filled container would sit on the
                    # fill at nearly every point; allow a sliver for a
                    # text edge that antialiases to a fill colour.
                    self.assertLess(
                        missing, 0.02 * (seen + missing),
                        f"{mode}/{key}: ghost hidden at {missing} of "
                        f"{seen + missing} stroke points; by {sorted(culprits)}")


class TestNoHitTestInterference(_WindowCase):
    def test_a_button_under_the_glyph_still_takes_the_click(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._window(mode)
                button = win.search_toggle_btn
                centre = button.mapTo(win.ghost, button.rect().center())
                self.assertTrue(win.ghost.ghost_rect().contains(centre),
                                "the button is not under the glyph")
                self.assertIs(win.ghost.childAt(centre), button)
                hits = []
                button.clicked.connect(lambda *_, h=hits: h.append(1))
                # A real press and release, not button.click(): it goes
                # through the same event delivery a mouse does.
                QTest.mouseClick(button, Qt.MouseButton.LeftButton,
                                 pos=button.rect().center())
                self.assertEqual(hits, [1])

    def test_the_ghost_owns_no_child_to_intercept_anything(self):
        win = self._window("dark")
        self.assertIsNone(win.ghost.childAt(QPoint(win.ghost.width() - 60, 4)))


class TestBrackets(_WindowCase):
    def _framed(self, mode):
        # A bare host on the real sheet: over the window the frame would be
        # drawn on top of the table, whose rows show through a transparent card.
        self._window(mode)
        host = QWidget()
        host.resize(500, 400)
        self.addCleanup(host.close)
        frame = QFrame(host)
        frame.setObjectName("Card")
        frame.setGeometry(100, 200, 300, 160)
        host.show()
        brackets = widgets.Brackets(frame)
        QApplication.processEvents()
        return host, frame, brackets

    def test_corners_are_painted_in_ink_65_and_the_interior_is_not(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win, frame, brackets = self._framed(mode)
                img = self._shot(win)
                ink = _ink_over(mode, "ink_65")
                x0, y0 = frame.x(), frame.y()
                # Inside the frame's hairline the marks sit on the page.
                for dx, dy in ((1, 1), (11, 1), (1, 11),
                               (298, 1), (288, 1), (298, 11),
                               (1, 158), (11, 158), (1, 148),
                               (298, 158), (288, 158), (298, 148)):
                    self.assertTrue(
                        _near(_rgb(img, x0 + dx, y0 + dy), ink),
                        f"{mode}: ({dx},{dy}) paints {_rgb(img, x0 + dx, y0 + dy)}, "
                        f"expected ink_65 {ink}")
                # On the hairline itself they sit on that, as in the mockup,
                # where the corner element is laid over the frame's border.
                line = _composite_over(*_parse_rgba(theme.palette(mode)["ink_65"]),
                                       _hairline(mode))
                for dx, dy in ((5, 0), (0, 5), (299, 5), (5, 159)):
                    self.assertTrue(
                        _near(_rgb(img, x0 + dx, y0 + dy), line),
                        f"{mode}: ({dx},{dy}) paints {_rgb(img, x0 + dx, y0 + dy)}, "
                        f"expected bracket over hairline {line}")
                # Past the arm, and inside the arm's thickness, is the frame.
                for dx, dy in ((12, 0), (0, 12), (2, 2), (150, 80), (150, 1)):
                    self.assertFalse(
                        _near(_rgb(img, x0 + dx, y0 + dy), ink, 1),
                        f"{mode}: ({dx},{dy}) is bracket ink but not a bracket")

    def test_arms_are_twelve_long_and_two_thick(self):
        win, frame, brackets = self._framed("dark")
        rects = brackets.corner_rects()
        self.assertEqual(len(rects), 8)
        sizes = sorted((r.width(), r.height()) for r in rects)
        self.assertEqual(sizes, [(2, 10)] * 4 + [(12, 2)] * 4)

    def test_brackets_clear_the_non_text_contrast_floor(self):
        for mode in MODES:
            ratio = _contrast_ratio(_ink_over(mode, "ink_65"),
                                    _parse_hex(theme.palette(mode)["page"]))
            self.assertGreaterEqual(ratio, 3.0, f"{mode}: {ratio:.2f}:1")

    def test_overlay_is_masked_to_the_marks_and_passes_the_mouse_through(self):
        win, frame, brackets = self._framed("dark")
        self.assertTrue(brackets.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        mask = brackets.mask()
        self.assertTrue(mask.contains(QPoint(0, 0)))
        self.assertFalse(mask.contains(QPoint(150, 80)))
        self.assertFalse(mask.contains(QPoint(12, 12)))
        # Area is the marks and nothing more: 4 L's of 12x2 + 2x10.
        self.assertLessEqual(brackets.mask().boundingRect().width(), 300)
        box = mask.boundingRect()
        area = sum(1 for x in range(box.width()) for y in range(box.height())
                   if mask.contains(QPoint(x, y)))
        self.assertEqual(area, 4 * (12 * 2 + 2 * 10))

    def test_a_child_behind_a_corner_still_takes_the_click(self):
        from PyQt6.QtWidgets import QPushButton
        win, frame, brackets = self._framed("dark")
        button = QPushButton("x", frame)
        button.setGeometry(0, 0, 40, 40)
        button.show()
        brackets.raise_()
        hits = []
        button.clicked.connect(lambda *_: hits.append(1))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(1, 1))
        self.assertEqual(hits, [1])

    def test_overlay_follows_the_frame_and_stays_on_top(self):
        from PyQt6.QtWidgets import QLabel
        win, frame, brackets = self._framed("dark")
        frame.resize(420, 200)
        QApplication.processEvents()
        self.assertEqual((brackets.width(), brackets.height()), (420, 200))
        late = QLabel("added later", frame)
        late.show()
        QApplication.processEvents()
        self.assertIs(frame.children()[-1], brackets)


class TestScrollingStaysCheap(_WindowCase):
    """"No repaint cost while scrolling 1000 rows" is a claim about the
    cost of the *ghost*, so it is measured as the ghost's own work: how
    often the glyph is re-rasterised, and how long its paint takes."""

    def _thousand_rows(self, mode):
        win = self._window(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "row.png")
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        image.save(path)
        win.entries[:] = [ImageEntry(path=path, status=MatchStatus.NOT_SEARCHED)
                          for _ in range(1000)]
        win.table_model.refresh_all(win.entries)
        QApplication.processEvents()
        return win

    def _scroll(self, win, steps=100):
        bar = win.table.verticalScrollBar()
        span = bar.maximum() - bar.minimum()
        for i in range(steps):
            bar.setValue(bar.minimum() + span * i // steps)
            QApplication.processEvents()

    def test_the_glyph_is_rasterised_once_and_blits_are_microseconds(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._thousand_rows(mode)
                ghost = win.ghost
                self._shot(win)
                renders = ghost.renders
                self.assertGreaterEqual(renders, 1)

                cls = type(ghost)
                original = cls.paintEvent
                spent = []

                def timed(self_, event, _orig=original, _spent=spent):
                    t0 = time.perf_counter()
                    _orig(self_, event)
                    _spent.append(time.perf_counter() - t0)

                cls.paintEvent = timed
                try:
                    self._scroll(win)
                finally:
                    cls.paintEvent = original
                self.assertEqual(ghost.renders, renders,
                                 "scrolling re-rasterised the glyph")
                # Each paint is one cached blit. 5 ms is >10x what it
                # measures on this host; it exists to catch a regression
                # to per-paint text shaping (~tens of ms), not to rank
                # machines.
                if spent:
                    self.assertLess(max(spent), 0.005, f"{mode}: {max(spent):.4f}s")
                print(f"\n[{mode}] 1000-row scroll: ghost paintEvents={len(spent)} "
                      f"total={sum(spent) * 1e3:.2f}ms "
                      f"max={max(spent, default=0) * 1e3:.3f}ms "
                      f"rasterisations={ghost.renders - renders}")

    def test_brackets_on_a_scrolling_table_cost_microseconds(self):
        """The frame round the Queue table has the table's own corners
        under it, so its bottom marks are repainted as the rows scroll.
        What is claimed is that each repaint is eight small rectangles."""
        win = self._thousand_rows("dark")
        card = win.table
        while card is not None and card.objectName() != "Card":
            card = card.parentWidget()
        brackets = widgets.Brackets(card)
        QApplication.processEvents()
        spent = []
        cls = type(brackets)
        original = cls.paintEvent

        def timed(self_, event, _orig=original):
            t0 = time.perf_counter()
            _orig(self_, event)
            spent.append(time.perf_counter() - t0)

        cls.paintEvent = timed
        try:
            self._scroll(win)
        finally:
            cls.paintEvent = original
        self.assertLess(max(spent, default=0), 0.002)
        print(f"\n[brackets] 1000-row scroll: paintEvents={len(spent)} "
              f"total={sum(spent) * 1e3:.2f}ms max={max(spent, default=0) * 1e3:.3f}ms")


if __name__ == "__main__":
    unittest.main()
