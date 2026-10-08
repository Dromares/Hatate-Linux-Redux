"""Tests for workers/file_hash_worker.py

These tests verify the worker's stop behavior - specifically that
calling stop() does NOT emit `finished_hashing` with partial results.
The `stopped` signal from BA-04 was removed because WorkerLifecycle.retire()
disconnects all signals before calling stop(), so a `stopped` signal would
never be delivered. The lifecycle already prevents stale results from reaching
the GUI.

Skipped automatically when PyQt6 isn't installed, so the rest of the
suite still runs in a plain Python environment.
"""
import os
import time
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QThread, Qt  # noqa: F401 - only used to test import
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestFileHashWorkerStop(unittest.TestCase):
    """Tests for the stop() behavior of FileHashWorker.

    `finished_hashing` is emitted from the worker's QThread. The default
    auto connection becomes a *queued* cross-thread connection, which is
    only delivered once the receiving thread's event loop runs - `wait()`
    blocks until the thread exits but never pumps that loop, so a plain
    connection here would need every test to also drive
    `QApplication.processEvents()` afterwards. There is no live
    QApplication event loop in this test module (and creating one here
    would flush unrelated `deleteLater()` backlog left over from other
    test modules that share the process's single QApplication instance),
    so tests instead connect with `Qt.ConnectionType.DirectConnection` to
    have the signal delivered synchronously, on the worker thread, at
    emit time - which is enough to observe it once `wait()` returns.
    """

    def test_stop_emits_no_signal_not_finished(self):
        """BA-04: stop() should NOT emit `finished_hashing` with partial results.
        
        The `stopped` signal was removed because WorkerLifecycle.retire()
        disconnects all signals before calling stop(). Since the worker is
        always stopped via the lifecycle, a separate `stopped` signal would
        never be delivered to any listener. This test verifies that no signal
        is emitted on stop (neither finished_hashing nor stopped).
        """
        from workers.file_hash_worker import FileHashWorker
        # Create a worker with many files to hash
        paths = [f"/tmp/test_{i}.dat" for i in range(100)]
        
        worker = FileHashWorker(paths, workers=4)
        
        # Mock hash_file to be slow so we can call stop() while it's running
        def slow_hash_file(path):
            time.sleep(0.1)
            return "a" * 64
        
        emitted_finished = []
        worker.finished_hashing.connect(
            lambda d: emitted_finished.append(d), Qt.ConnectionType.DirectConnection)

        with patch("workers.file_hash_worker.hash_file", side_effect=slow_hash_file):
            worker.start()
            # Give it time to start hashing
            time.sleep(0.2)
            # Now stop it
            worker.stop()
            worker.wait(5000)  # wait up to 5 seconds for thread to finish
        
        # Verify the worker stopped
        self.assertFalse(worker.isRunning())
        # The key assertion: finished_hashing should NOT be emitted on stop
        # (no stopped signal exists anymore - it was removed as redundant)
        self.assertEqual(len(emitted_finished), 0, "finished_hashing should NOT be emitted on stop")

    def test_normal_completion_emits_finished(self):
        """Normal completion should emit finished_hashing."""
        from workers.file_hash_worker import FileHashWorker
        paths = [f"/tmp/test_{i}.dat" for i in range(3)]
        
        worker = FileHashWorker(paths, workers=2)
        
        def fast_hash_file(path):
            return "a" * 64
        
        emitted_finished = []
        worker.finished_hashing.connect(
            lambda d: emitted_finished.append(d), Qt.ConnectionType.DirectConnection)

        with patch("workers.file_hash_worker.hash_file", side_effect=fast_hash_file):
            worker.start()
            worker.wait(5000)

        self.assertFalse(worker.isRunning())
        self.assertEqual(len(emitted_finished), 1, "finished_hashing should be emitted")
        self.assertEqual(len(emitted_finished[0]), 3, "all 3 files should be hashed")


if __name__ == "__main__":
    unittest.main()