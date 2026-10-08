"""DAN-485: a crash and a normal quit used to restore identically because
nothing recorded whether the previous run reached a clean shutdown. These
cover the MainWindow-level wiring - the sentinel logic itself lives in
core/crashlog.py and is covered by tests/test_core_utils.py's
TestUncleanShutdownMarker.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import _path  # noqa: F401

from PyQt6.QtWidgets import QApplication

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestUncleanShutdownFlagIsStored(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def test_defaults_to_clean(self):
        from gui.main_window import MainWindow
        win = MainWindow()
        try:
            self.assertFalse(win._unclean_shutdown)
        finally:
            win.close()
            win.deleteLater()

    def test_records_an_unclean_shutdown_when_told(self):
        from gui.main_window import MainWindow
        win = MainWindow(unclean_shutdown=True)
        try:
            self.assertTrue(win._unclean_shutdown)
        finally:
            win.close()
            win.deleteLater()


class TestCloseEventMarksCleanShutdown(unittest.TestCase):
    """REGRESSION: closing the window is the one place that proves THIS
    run exited cleanly for the NEXT run's check - forgetting to call it
    would make every launch look like it followed a crash."""

    @classmethod
    def setUpClass(cls):
        _app()

    def test_close_event_clears_the_running_marker(self):
        from gui import main_window as main_window_module
        win = main_window_module.MainWindow()
        with patch.object(main_window_module.crashlog, "mark_clean_shutdown") as marked:
            win.close()
        marked.assert_called_once()
        win.deleteLater()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
