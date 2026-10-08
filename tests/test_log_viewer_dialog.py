"""Files > Help > View Logs only ever opened app.log - crash.log (written
by core/crashlog.py on an unhandled exception, Qt fatal, or hard crash) had
nowhere a user could read it from. These exercise the toggle that was
added to point the dialog at crash.log too (DAN-485).
"""
import os
import tempfile
import unittest

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-log-viewer-dialog-")

try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

_app = None


def setUpModule():
    """One QApplication for the whole module - Qt allows only one."""
    global _app
    if HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestCrashLogToggle(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from pathlib import Path
        from core import crashlog
        from gui.log_viewer_dialog import LogViewerDialog
        self.fault_file = Path(tempfile.mkdtemp(prefix="hatate-crash-log-")) / "crash.log"
        self._fault_patch = patch.object(crashlog, "FAULT_FILE", self.fault_file)
        self._fault_patch.start()
        self.addCleanup(self._fault_patch.stop)
        self.dialog = LogViewerDialog()
        self.addCleanup(self.dialog.deleteLater)

    def test_starts_on_the_app_log(self):
        self.assertFalse(self.dialog._showing_crash_log)
        self.assertEqual(self.dialog.crash_log_btn.text(), "View Crash Log")

    def test_missing_crash_log_says_so_instead_of_showing_app_log_content(self):
        self.dialog._toggle_crash_log()
        self.assertIn("no crash log yet", self.dialog.text.toPlainText())

    def test_toggle_shows_crash_log_contents(self):
        self.fault_file.write_text("QThread: Destroyed while thread is still running\n",
                                    encoding="utf-8")
        self.dialog._toggle_crash_log()
        self.assertIn("still running", self.dialog.text.toPlainText())
        self.assertEqual(self.dialog.crash_log_btn.text(), "View App Log")

    def test_toggle_back_returns_to_the_app_log(self):
        self.dialog._toggle_crash_log()
        self.dialog._toggle_crash_log()
        self.assertFalse(self.dialog._showing_crash_log)
        self.assertEqual(self.dialog.crash_log_btn.text(), "View Crash Log")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
