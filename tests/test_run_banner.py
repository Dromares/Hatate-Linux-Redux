"""DAN-660: the Crashed/partial-results run-banner. Covers the one thing
the status-bar string couldn't - that the notice survives being the only
thing on screen worth looking at - plus the three actions it hands off to
existing app behaviour rather than reimplementing. Follows
test_tristate_restore.py's window-construction pattern (DAN-487), which
this banner directly replaces the status-bar half of.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import _path  # noqa: F401

from PyQt6.QtWidgets import QApplication

from core.models import ImageEntry, MatchStatus

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestRunBanner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def _make_window(self, *, unclean_shutdown, had_session_file, entries):
        from gui import main_window as mw
        with patch.object(mw, "has_saved_session", return_value=had_session_file), \
             patch.object(mw, "load_session", return_value=entries), \
             patch.object(mw, "get_quota_pause_state", return_value=None):
            win = mw.MainWindow(unclean_shutdown=unclean_shutdown)
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        return win

    def test_hidden_on_clean_restore(self):
        """Acceptance criterion 3: a clean restore gets no banner at all.

        setVisible() is explicit on the widget itself regardless of
        whether the top-level window is ever shown, so this checks it
        directly rather than isVisible() - which answers a different
        question (is the whole ancestor chain on screen) that a
        never-shown offscreen MainWindow would always fail."""
        entry = ImageEntry(path="/tmp/dan660-clean.png")
        win = self._make_window(unclean_shutdown=False, had_session_file=True, entries=[entry])
        self.assertFalse(win.run_banner.isVisibleTo(win.run_banner.parentWidget()))

    def test_hidden_when_nothing_survived(self):
        """The DAN-487 empty-restore case never reaches _show_run_banner -
        there are no entries for it to describe."""
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[])
        self.assertFalse(win.run_banner.isVisibleTo(win.run_banner.parentWidget()))

    def test_shown_on_unclean_restore_with_entries(self):
        entry = ImageEntry(path="/tmp/dan660-crashed.png")
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
        self.assertTrue(win.run_banner.isVisibleTo(win.run_banner.parentWidget()))
        self.assertEqual(win._run_banner_body.text(), "0 sent, 0 still queued, 1 not yet searched")

    def test_body_counts_per_category(self):
        """Reads the same per-row status the table already tracks - no
        new counting logic, matching _refresh_sent_count_label's own
        sent/queued split plus action_start_search's unsearched count."""
        entries = [
            ImageEntry(path="/tmp/dan660-1.png", status=MatchStatus.NOT_SEARCHED),
            ImageEntry(path="/tmp/dan660-2.png", status=MatchStatus.NOT_SEARCHED),
            ImageEntry(path="/tmp/dan660-3.png", status=MatchStatus.GOOD,
                       sent_to_hydrus=True, hydrus_import_confirmed=True),
            ImageEntry(path="/tmp/dan660-4.png", status=MatchStatus.GOOD,
                       sent_to_hydrus=True, hydrus_import_confirmed=False),
        ]
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=entries)
        self.assertEqual(win._run_banner_body.text(), "1 sent, 1 still queued, 2 not yet searched")

    def test_resume_button_routes_to_action_start_search(self):
        """The button's clicked signal is wired at _build_queue_page time,
        inside MainWindow.__init__ - patching the instance attribute
        AFTER construction does not retarget an already-made Qt
        connection, so the method has to be patched on the class before
        the window (and its connect() call) exists."""
        from gui import main_window as mw
        from gui import widgets
        entry = ImageEntry(path="/tmp/dan660-resume.png")
        with patch.object(mw.MainWindow, "action_start_search") as action:
            win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
            buttons = win.run_banner.findChildren(widgets.QPushButton)
            resume = next(b for b in buttons if b.text() == "Resume Queue")
            resume.click()
        action.assert_called_once()

    def test_crash_log_button_routes_to_open_crash_log(self):
        from gui import main_window as mw
        from gui import widgets
        entry = ImageEntry(path="/tmp/dan660-crashlog.png")
        with patch.object(mw.MainWindow, "_open_crash_log") as action:
            win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
            buttons = win.run_banner.findChildren(widgets.QPushButton)
            crash_log = next(b for b in buttons if b.text() == "View Crash Log")
            crash_log.click()
        action.assert_called_once()

    def test_crash_log_action_opens_dialog_preswitched(self):
        entry = ImageEntry(path="/tmp/dan660-crashlog2.png")
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
        with patch.object(win, "_run_dialog") as run_dialog:
            win._open_crash_log()
        dialog = run_dialog.call_args[0][0]
        self.assertTrue(dialog._showing_crash_log)

    def test_discard_label_routes_to_action_clear_session(self):
        from gui import main_window as mw
        from gui import widgets
        entry = ImageEntry(path="/tmp/dan660-discard.png")
        with patch.object(mw.MainWindow, "action_clear_session") as action:
            win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
            discard = win.run_banner.findChild(widgets.LinkLabel)
            discard.clicked.emit()
        action.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
