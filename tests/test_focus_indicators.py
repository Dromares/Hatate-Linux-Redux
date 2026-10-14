"""Keyboard focus must be visible on every Tab stop (DAN-1464).

Found on DAN-1460 with real Tab key events on a real display: the Queue's
filter combos and table, and Review's candidate picker and tag list, drew
exactly the same pixels focused and unfocused, and Review's page container
was a Tab stop that drew nothing at all.

Measured from the painted window, not from the stylesheet text: each stop
is grabbed with and without focus, and the two grabs must differ inside
(or on the border of) the widget's own rect.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from core.models import ImageEntry, MatchStatus
from gui import main_window as mw
from tests.test_gui_harness import make_themed_window

MARGIN = 6   # a focus ring is drawn on the widget's border; allow a few px of bleed


def _tab_stops(win):
    """Every visible widget a Tab key can land on, in the window.

    QPushButtons are left out: their focus tint is the platform style's own
    (it needs a window-manager-active window, which the offscreen platform
    never has), so it only shows on the xcb pass of the DAN-1460 driver. The
    widgets here take theirs from the stylesheet, which offscreen does paint.
    """
    stops = []
    for w in win.findChildren(QWidget):
        if isinstance(w, QPushButton):
            continue
        if w.isVisible() and bool(w.focusPolicy() & Qt.FocusPolicy.TabFocus):
            stops.append(w)
    return stops


def _changed_inside(a, b, widget, win):
    """True when images `a` and `b` of `win` differ within `widget`'s rect."""
    tl = widget.mapTo(win, QPoint(0, 0))
    x0, y0 = max(tl.x() - MARGIN, 0), max(tl.y() - MARGIN, 0)
    w = min(tl.x() + widget.width() + MARGIN, a.width()) - x0
    h = min(tl.y() + widget.height() + MARGIN, a.height()) - y0
    return a.copy(x0, y0, w, h) != b.copy(x0, y0, w, h)


class TestFocusIsVisible(unittest.TestCase):
    def _window(self, mode, page):
        # the entry's file does not exist: without this the missing-file
        # check opens a modal and the test hangs
        started = patch.object(mw.MainWindow, "_start_missing_file_check")
        started.start()
        self.addCleanup(started.stop)
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
        win.set_mode(page)
        win.raise_()
        win.activateWindow()
        for _ in range(5):
            QApplication.processEvents()
        return win

    def _check(self, mode, page):
        win = self._window(mode, page)
        stops = _tab_stops(win)
        self.assertTrue(stops, "no Tab stops found - the test would pass vacuously")
        invisible = []
        for w in stops:
            w.clearFocus()
            QApplication.processEvents()
            plain = win.grab().toImage()
            w.setFocus(Qt.FocusReason.TabFocusReason)
            QApplication.processEvents()
            focused = win.grab().toImage()
            if QApplication.focusWidget() is not w:
                continue    # the platform would not give it focus; nothing to compare
            if not _changed_inside(plain, focused, w, win):
                invisible.append(f"{type(w).__name__}[{w.objectName() or '-'}]")
        self.assertEqual(invisible, [], f"Tab stops with no visible focus ({mode}/{page})")

    def test_queue_dark(self):
        self._check("dark", "queue")

    def test_queue_light(self):
        self._check("light", "queue")

    def test_review_dark(self):
        self._check("dark", "review")

    def test_review_light(self):
        self._check("light", "review")

    def test_review_page_container_is_not_a_tab_stop(self):
        win = self._window("dark", "review")
        self.assertFalse(bool(win.review_page.focusPolicy() & Qt.FocusPolicy.TabFocus))
        # ...but the review keys still need it to be focusable by code
        win.review_page.setFocus()
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), win.review_page)


if __name__ == "__main__":
    unittest.main()
