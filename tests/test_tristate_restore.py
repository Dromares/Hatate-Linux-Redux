"""DAN-487: an empty Queue on launch used to mean one thing - nothing was
ever saved. After an unclean shutdown it can also mean a session existed
but came back empty (missing/corrupt/unreadable), which calls for a
different message than the ordinary empty-queue silence. These patch
MainWindow's module-level session functions directly rather than touching
real files - the storage-level behaviour of has_saved_session() itself is
covered by tests/test_session.py's TestSessionRoundTrip.
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


class TestTristateRestoreMessage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def _make_window(self, *, unclean_shutdown, had_session_file, entries):
        from gui import main_window as mw
        # Also stub the (unrelated) SauceNAO quota-pause banner: it reads
        # real on-disk state, and some other test module in the suite can
        # leave a stale pause file behind on the process-wide CONFIG_DIR,
        # which would otherwise clobber status_label.text() here too.
        with patch.object(mw, "has_saved_session", return_value=had_session_file), \
             patch.object(mw, "load_session", return_value=entries), \
             patch.object(mw, "get_quota_pause_state", return_value=None):
            win = mw.MainWindow(unclean_shutdown=unclean_shutdown)
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        return win

    def test_clean_start_with_nothing_ever_saved_is_silent(self):
        """The ordinary empty-queue case: no message, no banner."""
        win = self._make_window(unclean_shutdown=False, had_session_file=False, entries=[])
        self.assertEqual(win.status_label.text(), "Ready")

    def test_unclean_shutdown_but_no_session_file_is_still_silent(self):
        """An unclean shutdown with nothing ever saved is not a failed
        restore - there was nothing to restore in the first place."""
        win = self._make_window(unclean_shutdown=True, had_session_file=False, entries=[])
        self.assertEqual(win.status_label.text(), "Ready")

    def test_clean_shutdown_with_an_empty_session_file_is_silent(self):
        """Only an UNCLEAN shutdown turns an empty restore into a message -
        a session file that is simply empty after a normal quit is not a
        failure worth announcing."""
        win = self._make_window(unclean_shutdown=False, had_session_file=True, entries=[])
        self.assertEqual(win.status_label.text(), "Ready")

    def test_unclean_shutdown_with_a_session_file_but_nothing_restored(self):
        """REGRESSION target: the specific case DAN-487 exists for - a
        session existed (so something was presumably being worked on) but
        load_session() came back empty, after a shutdown that wasn't
        clean. That must route to its own message, not the ordinary
        silent empty state."""
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[])
        self.assertIn("nothing survived", win.status_label.text())

    def test_entries_do_restore_even_when_unclean(self):
        """Sanity check that this change didn't disturb the ordinary
        (non-empty) restore path. The message itself moved to the
        run-banner (DAN-660) rather than the status bar - see
        tests/test_run_banner.py - so this only checks the entries
        themselves still come back and the status bar is left alone."""
        from core.models import ImageEntry
        entry = ImageEntry(path="/tmp/does-not-need-to-exist.png")
        win = self._make_window(unclean_shutdown=True, had_session_file=True, entries=[entry])
        self.assertEqual(win.status_label.text(), "Ready")
        self.assertIn(entry, win.entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
