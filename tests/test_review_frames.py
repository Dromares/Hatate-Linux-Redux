"""Review's image frames and viewport budget (DAN-1174, gap rows V-03, V-04).

V-03: the two pictures sit in equal hairline frames with corner brackets, and
the caption is a mono line *below* each frame (`LOCAL` ... `560 × 900 · PNG ·
10.1 KB`, `MATCH — DANBOORU` ... `1400 × 2200`), not two sentences above it.
Production's honest qualifier survives (B-5): an unmeasured score says
`ranking, not measured` in the caption.

V-04: the picture gets at least 55% of the window's height at 1440x900. The
number is read off the real, themed window (`make_themed_window`), in both
themes - a size measured on an unstyled window means nothing.
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PIL import Image
from PyQt6.QtWidgets import QApplication

from gui import widgets
from gui.preview_text import (
    local_frame_meta, matched_frame_label, matched_frame_meta,
)
from tests._qt_app import ensure_app
from tests.test_review_keycaps import _ReviewCase
from tests.test_type_voice import _is_caps_mono

MODES = ("dark", "light")
MIN_VIEWPORT_SHARE = 0.55      # gap-register P6b: image viewport / window height


class _Entry:
    """Just what the caption functions read."""
    def __init__(self, booru_name="Danbooru", similarity=96.0, measured=True):
        self.booru_name = booru_name
        self.similarity = similarity
        self.similarity_measured = measured


class _Candidate:
    def __init__(self, width=None, height=None, remote_format=None, remote_size_bytes=None):
        self.width, self.height = width, height
        self.remote_format, self.remote_size_bytes = remote_format, remote_size_bytes


class TestCaptionText(unittest.TestCase):
    def test_local_meta_is_the_mockups_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            Image.new("RGB", (560, 900), "#336699").save(path)
            text = local_frame_meta(path, Image.open)
        size = text.split(" · ")[-1]
        self.assertEqual(text, f"560 × 900 · PNG · {size}")
        self.assertRegex(size, r"^\d+(\.\d)? (B|KB)$")

    def test_local_meta_is_empty_for_an_unreadable_file(self):
        self.assertEqual(local_frame_meta("/nonexistent/x.png", Image.open), "")

    def test_a_measured_match_is_named_by_its_site_alone(self):
        # the score is the header's readout; repeating it here would say it twice
        self.assertEqual(matched_frame_label(_Entry()), "Match — Danbooru")

    def test_an_unmeasured_score_keeps_its_qualifier(self):  # B-5
        self.assertEqual(
            matched_frame_label(_Entry("Yande.re", 77.0, measured=False)),
            "Match — Yande.re · ~77% · ranking, not measured")

    def test_a_match_with_no_site_and_no_score_is_just_match(self):
        self.assertEqual(matched_frame_label(_Entry(None, None)), "Match")

    def test_match_meta_uses_the_caption_separators(self):
        self.assertEqual(
            matched_frame_meta(_Candidate(1400, 2200, "PNG", 2400000)),
            "1400 × 2200 · PNG · 2.3 MB")
        self.assertEqual(matched_frame_meta(_Candidate(784, 1260, "JPEG")), "784 × 1260 · JPEG")
        self.assertEqual(matched_frame_meta(_Candidate()), "")
        self.assertEqual(matched_frame_meta(None), "")


class _FrameCase(_ReviewCase):
    def _y(self, widget, win):
        return widget.mapTo(win, widget.rect().topLeft()).y()


class TestFramesAndCaptions(_FrameCase):
    def test_the_two_frames_are_equal_and_carry_four_bracket_corners(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                local, match = win.local_frame, win.matched_frame
                # equal columns of an odd total width differ by a pixel, no more
                self.assertLessEqual(abs(local.width() - match.width()), 1)
                self.assertEqual(local.height(), match.height())
                self.assertEqual(local.objectName(), "ImageFrame")
                for frame in (local, match):
                    marks = [c for c in frame.children()
                             if isinstance(c, widgets.Brackets)]
                    self.assertEqual(len(marks), 1)
                    self.assertEqual(len(marks[0].corner_rects()), 8)  # 4 L shapes
                    self.assertEqual(marks[0].size(), frame.size())
                # the picture fills its frame, inside the hairline
                self.assertEqual(win.local_preview.parentWidget(), local)
                self.assertLessEqual(local.width() - win.local_preview.width(), 2)

    def test_the_caption_sits_below_its_frame_and_nothing_sits_above(self):
        win = self._review_window()
        for frame, caption in ((win.local_frame, win.local_frame_caption),
                               (win.matched_frame, win.matched_frame_caption)):
            frame_bottom = self._y(frame, win) + frame.height()
            self.assertGreaterEqual(self._y(caption, win), frame_bottom)
            self.assertLess(self._y(caption, win) - frame_bottom, 16)
        self.assertEqual(
            self._y(win.local_frame, win),
            self._y(win.review_stack, win), "nothing between the header and the frames")
        texts = [label.text() for label in win.review_page.findChildren(widgets.QLabel)]
        self.assertFalse([t for t in texts if "Local image" in t or "Matched image" in t])

    def test_captions_are_mono_caps_and_say_what_the_mockup_says(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                entry = win.entries[0]
                candidate = entry.selected_candidate
                candidate.similarity_measured = True
                entry.select_candidate(0)
                candidate.width, candidate.height = 1400, 2200
                candidate.remote_format, candidate.remote_size_bytes = "PNG", 2400000
                win._update_preview(entry)
                QApplication.processEvents()
                local, match = win.local_frame_caption, win.matched_frame_caption
                for label in (local.left, local.right, match.left, match.right):
                    self.assertTrue(_is_caps_mono(label), label.text())
                self.assertEqual(local.left.text(), "Local")
                self.assertRegex(local.right.text(), r"^8 × 8 · PNG · \d")
                self.assertEqual(match.left.text(), "Match — Danbooru")
                self.assertEqual(match.right.text(), "1400 × 2200 · PNG · 2.3 MB")

    def test_an_unmeasured_match_keeps_its_ranking_qualifier_in_the_caption(self):  # B-5
        win = self._review_window()
        entry = win.entries[0]
        entry.candidates[0].similarity_measured = False
        entry.candidates[0].engine = "ascii2d"  # ordinal; IQDB's score counts as measured (P5)
        entry.select_candidate(0)
        win._update_preview(entry)
        self.assertIn("ranking, not measured", win.matched_caption.text())
        self.assertIn("~", win.matched_caption.text())

    def test_no_match_says_so_in_the_caption(self):
        win = self._review_window(candidates=0)
        self.assertEqual(win.matched_caption.text(), "Match — none")
        self.assertEqual(win.matched_info_label.text(), "—")

    def test_a_long_caption_elides_instead_of_widening_the_window(self):
        win = self._review_window()
        width_before = win.matched_frame_caption.minimumSizeHint().width()
        win.matched_caption.setText("Match — " + "Somebooru " * 40)
        QApplication.processEvents()
        self.assertEqual(win.matched_frame_caption.minimumSizeHint().width(), width_before)
        self.assertLessEqual(abs(win.matched_frame.width() - win.local_frame.width()), 1)

    def test_a_parsers_reason_gets_its_own_line_and_only_when_there_is_one(self):
        win = self._review_window()
        entry = win.entries[0]
        self.assertTrue(win.matched_note_label.isHidden())
        entry.selected_candidate.incomplete_reason = "hash miss"
        win._update_preview(entry)
        self.assertFalse(win.matched_note_label.isHidden())
        self.assertEqual(win.matched_note_label.text(), "hash miss")
        self.assertNotIn("hash miss", win.matched_info_label.text())
        entry.selected_candidate.incomplete_reason = None
        win._update_preview(entry)
        self.assertTrue(win.matched_note_label.isHidden())

    def test_the_wipe_view_is_framed_and_its_zoom_floats_inside_the_frame(self):  # R-3
        win = self._review_window()
        win._on_review_view_changed("wipe")
        QApplication.processEvents()
        frame = win.review_wipe_frame
        self.assertTrue(any(isinstance(c, widgets.Brackets) for c in frame.children()))
        self.assertIs(win.review_wipe.parentWidget(), frame)
        self.assertIs(win.review_zoom_tools.parentWidget(), win.review_wipe)
        self.assertTrue(win.review_wipe.rect().contains(
            win.review_zoom_tools.geometry().topLeft()))


class TestViewportBudget(_FrameCase):
    def test_the_picture_gets_at_least_55_percent_of_the_window_height(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                win = self._review_window(mode)
                self.assertEqual((win.width(), win.height()), (1440, 900))
                for name in ("local_preview", "matched_preview"):
                    viewport = getattr(win, name).height()
                    self.assertGreaterEqual(
                        viewport / win.height(), MIN_VIEWPORT_SHARE,
                        f"{name} is {viewport}px of {win.height()} "
                        f"({viewport / win.height():.1%})")

    def test_the_footer_still_fits_and_sits_where_the_page_ends(self):
        win = self._review_window()
        strip = win.review_hint_bar.parentWidget()
        self.assertLessEqual(self._y(strip, win) + strip.height(), win.height())
        for button in (win.review_prev_btn, win.review_next_btn,
                       win.review_next_unreviewed_btn):
            self.assertLessEqual(button.height(), widgets.FOOTER_BUTTON_HEIGHT + 2)
            self.assertGreaterEqual(button.height(), widgets.FOOTER_BUTTON_HEIGHT)
            # the label and its key are not clipped by the shorter button
            self.assertGreaterEqual(button.height(), button.keycap.height() + 2)


if __name__ == "__main__":
    ensure_app()
    unittest.main()
