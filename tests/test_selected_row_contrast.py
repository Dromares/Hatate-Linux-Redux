"""The selected Queue row must stay legible (DAN-1151).

The selection fill is `stamp_bg`, which in both themes is the very same
colour as `ink_100` (the table's ordinary text). ChipDelegate and the
model's ForegroundRole both resolved their colour through `ink_color`
regardless of selection, so on the selected row Status, Sent and the
estimate-similarity chip painted ink on ink - 1.00:1, blank cells.

tests/test_theme.py guards the QSS sheet; nothing asserted the *painted*
selected state, which is what this does: it renders a real, themed
MainWindow's table with a row selected and measures the pixels.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtCore import QRect

from core.models import ImageEntry, MatchStatus
from gui import theme
from gui.image_table_model import (
    COL_REVIEWED, COL_SENT, COL_SIMILARITY, COL_STATUS,
)
from tests.test_gui_harness import make_themed_window
from tests.test_theme import _contrast_ratio

# WCAG 1.4.3 body-text floor. The cells are painted at 14px regular.
MIN_CONTRAST = 4.5


def _entries(tmpdir):
    out = []

    def add(**kw):
        path = os.path.join(tmpdir, f"img_{len(out)}.png")
        out.append(ImageEntry(path=path, **kw))

    add(status=MatchStatus.GOOD, sent_to_hydrus=True, hydrus_import_confirmed=True,
        reviewed=True)
    add(status=MatchStatus.POOR, similarity=81.0, similarity_measured=False,
        sent_to_hydrus=True, hydrus_import_confirmed=False)
    add(status=MatchStatus.NOT_FOUND)
    add(status=MatchStatus.ERROR)
    add(status=MatchStatus.NOT_SEARCHED)
    return out


def _worst_contrast(image, rect, fill):
    """Contrast of the pixel in `rect` furthest from `fill` - i.e. how
    legible the strongest ink in the cell is. A blank cell scores 1.0."""
    best = 1.0
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            c = image.pixelColor(x, y)
            best = max(best, _contrast_ratio((c.red(), c.green(), c.blue()), fill))
    return best


class TestSelectedRowStaysLegible(unittest.TestCase):
    def _check(self, mode):
        import tempfile
        _app, win = make_themed_window(self, mode)
        win.action_set_theme(mode)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        win.entries[:] = _entries(tmp.name)
        win.table_model.refresh_all(win.entries)
        win.resize(1400, 700)
        win.show()
        self.addCleanup(win.close)
        win.table.clearSelection()

        fill_hex = theme.palette(mode)["stamp_bg"].lstrip("#")
        fill = tuple(int(fill_hex[i:i + 2], 16) for i in (0, 2, 4))

        for row, columns in (
            (0, (COL_STATUS, COL_SENT)),       # good + sent chip + reviewed tick
            (1, (COL_STATUS, COL_SENT, COL_SIMILARITY)),  # poor + queued + ~estimate
        ):
            win.select_table_row(row)
            win.table.viewport().repaint()
            image = win.table.viewport().grab().toImage()
            for col in columns + ((COL_REVIEWED,) if row == 0 else ()):
                idx = win.table_model.index(row, col)
                rect = win.table.visualRect(idx).adjusted(2, 2, -2, -2)
                rect = rect.intersected(QRect(0, 0, image.width(), image.height()))
                with self.subTest(mode=mode, row=row, col=col):
                    self.assertFalse(rect.isEmpty(), "cell is not on screen")
                    self.assertGreaterEqual(
                        _worst_contrast(image, rect, fill), MIN_CONTRAST,
                        f"selected {mode} row {row} col {col} is unreadable "
                        "against the selection fill",
                    )

    def test_dark(self):
        self._check("dark")

    def test_light(self):
        self._check("light")


if __name__ == "__main__":
    unittest.main()
