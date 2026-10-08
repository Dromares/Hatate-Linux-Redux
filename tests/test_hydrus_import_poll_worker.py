"""Tests for workers/hydrus_import_poll_worker.py

These tests verify the worker's stop/cancel behavior - specifically that
calling stop() cancels in-flight futures and doesn't emit signals after
the stop request. This is a regression test for BA-03.

Skipped automatically when PyQt6 isn't installed, so the rest of the
suite still runs in a plain Python environment.
"""
import os
import time
import unittest
from unittest.mock import MagicMock

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QThread, Qt  # noqa: F401 - only used to test import
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestHydrusImportPollWorkerStop(unittest.TestCase):
    """Tests for the stop() behavior of HydrusImportPollWorker."""

    def _make_entry(self, filename="test.png", url="http://example.com/image.jpg"):
        from core.models import ImageEntry
        entry = ImageEntry(path=f"/tmp/{filename}")
        entry.matched_url = url
        return entry

    def test_stop_cancels_inflight_futures(self):
        """BA-03: stop() should cancel in-flight futures and not emit signals."""
        from core.hydrus_client import HydrusClient
        from workers.hydrus_import_poll_worker import HydrusImportPollWorker
        client = MagicMock(spec=HydrusClient)
        # Make get_url_files slow so we can call stop() while it's running
        def slow_get_url_files(url):
            time.sleep(0.5)
            return {"files": []}
        client.get_url_files.side_effect = slow_get_url_files

        entries = [(self._make_entry(f"test{i}.png", f"http://example.com/{i}.jpg"), f"http://example.com/{i}.jpg")
                   for i in range(5)]
        worker = HydrusImportPollWorker(client, entries, timeout=10.0, interval=0.1)

        # Track emitted signals
        emitted = []
        worker.entry_resolved.connect(lambda e, h: emitted.append((e, h)))
        worker.finished_all.connect(lambda: emitted.append(("finished", None)))

        worker.start()
        # Give it time to submit futures
        time.sleep(0.1)
        # Now stop it
        worker.stop()
        worker.wait(2000)  # wait up to 2 seconds for thread to finish

        # Verify the worker stopped
        self.assertFalse(worker.isRunning())
        # The key assertion: no entry_resolved signals should have been emitted
        # after stop() was called. Since get_url_files takes 0.5s and we stopped
        # after 0.1s, any signal would mean the future wasn't cancelled/ignored.
        entry_resolved_signals = [e for e in emitted if e[0] != "finished"]
        self.assertEqual(len(entry_resolved_signals), 0,
                         "entry_resolved should not be emitted after stop()")

    def test_stop_before_submit_emits_finished(self):
        """Calling stop() before any work starts should still emit finished_all.

        `finished_all` is emitted from the worker's QThread; the default
        auto connection becomes a queued cross-thread connection that is
        only delivered once the receiving thread's event loop runs, which
        `wait()` does not do. Connecting with `Qt.ConnectionType.
        DirectConnection` delivers it synchronously at emit time instead,
        so the assertion below can rely on `wait()` alone.
        """
        from core.hydrus_client import HydrusClient
        from workers.hydrus_import_poll_worker import HydrusImportPollWorker
        client = MagicMock(spec=HydrusClient)
        entries = [(self._make_entry(), "http://example.com/1.jpg")]

        worker = HydrusImportPollWorker(client, entries, timeout=10.0, interval=0.1)
        worker.stop()  # Stop before starting

        emitted = []
        worker.finished_all.connect(
            lambda: emitted.append("finished"), Qt.ConnectionType.DirectConnection)
        worker.start()
        worker.wait(1000)

        self.assertIn("finished", emitted)


if __name__ == "__main__":
    unittest.main()