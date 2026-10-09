"""The irreversible-action caption under Send to Hydrus (DAN-1168, V-09).

Send is the one Review action that is not easily taken back, so the
mockup captions it: `Uploads file + tags to Hydrus - not easily undone`,
directly beneath the button. The caption is ink_65 mono at 10px, which is
small text, so it gets no large-text relaxation: it must clear the 4.5:1
body-text floor in both modes.

Measured from the painted label, not from the palette: the worst pixel
in the caption's rect against the background pixel actually behind it.
"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtWidgets import QPushButton

from core.models import ImageEntry, MatchStatus
from tests.test_gui_harness import make_themed_window
from tests.test_theme import _contrast_ratio

CAPTION = "Uploads file + tags to Hydrus — not easily undone"
MIN_CONTRAST = 4.5


class TestSendCaption(unittest.TestCase):
    def _review(self, mode):
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        entry = ImageEntry(path=os.path.join(tmp.name, "a.png"), status=MatchStatus.GOOD)
        win.entries.append(entry)
        win._register_new_entries([entry])
        win._refresh_table()
        win.resize(1440, 900)
        win.show()
        self.addCleanup(win.close)
        win.set_mode("review")
        win.review_page.repaint()
        return win

    def _send_button(self, win):
        return next(b for b in win.review_page.findChildren(QPushButton)
                    if b.text().startswith("Send to Hydrus"))

    def test_caption_text_and_position_beneath_send(self):
        win = self._review("dark")
        send, cap = self._send_button(win), win.send_caption
        self.assertEqual(cap.text(), CAPTION)
        self.assertTrue(cap.isVisible())
        self.assertGreaterEqual(cap.mapTo(win, cap.rect().topLeft()).y(),
                                send.mapTo(win, send.rect().bottomLeft()).y())
        # directly beneath: the caption is the Send button's next sibling in the column
        layout = send.parentWidget().layout()
        self.assertEqual(layout.itemAt(layout.indexOf(send) + 1).widget(), cap)

    def test_send_is_unchanged(self):
        win = self._review("dark")
        send = self._send_button(win)
        self.assertEqual(send.objectName(), "Primary")
        self.assertIn("(Return)", send.text())

    def _check_contrast(self, mode):
        win = self._review(mode)
        cap = win.send_caption
        self.assertGreater(cap.width(), 0)
        # The label paints transparent, so grab the window and crop to it:
        # that is what the user sees, background included.
        top_left = cap.mapTo(win, cap.rect().topLeft())
        image = win.grab().toImage().copy(top_left.x(), top_left.y(), cap.width(), cap.height())
        bg = image.pixelColor(0, 0)
        bg_rgb = (bg.red(), bg.green(), bg.blue())
        worst = 1.0
        for y in range(image.height()):
            for x in range(image.width()):
                c = image.pixelColor(x, y)
                worst = max(worst, _contrast_ratio((c.red(), c.green(), c.blue()), bg_rgb))
        self.assertGreaterEqual(worst, MIN_CONTRAST,
                                f"{mode} caption strongest ink is {worst:.2f}:1 on its background")
        return worst

    def test_contrast_dark(self):
        self._check_contrast("dark")

    def test_contrast_light(self):
        self._check_contrast("light")


if __name__ == "__main__":
    unittest.main()
