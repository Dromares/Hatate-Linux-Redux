"""The two remaining sub-60% QThread wrappers (DAN-74).

Measured at 4c1acd9:

    workers/missing_file_worker.py   44%   missing 27-29, 32, 35-43
    workers/hydrus_lookup_worker.py  55%   missing 24-26, 29-35

Both are thin - a constructor, `run()`, one signal - and in both cases
`run()` was never entered. That is worse for a QThread than for a plain
function, for the reason tests/test_workers_background.py gives: an
exception escaping `run()` does not surface as a failed call. It prints on
a background thread and the GUI simply waits forever for a signal that
never arrives.

Which makes the emit-exactly-once property the thing worth asserting, on
every path including the cancelled and failed ones. `run()` is called
directly on the test's own thread so the emissions are synchronous.

The work each wrapper delegates to (core.missing_files.find_missing_paths,
core.hydrus_tag_lookup.apply_existing_hydrus_tags) has its own tests; here
it is patched, because what is under test is the wrapper's contract with
the GUI, not the logic it wraps. The one exception is the missing-file
worker's real filesystem pass, which uses a real temp directory - the
stop-check plumbing between the worker and the scan is the only part of
that contract a mock cannot show.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QThread  # noqa: F401 - import probe only
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


def _entry(path):
    from core.models import ImageEntry
    return ImageEntry(path=path)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestMissingFileWorker(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="hatate-dan74-missing-")
        self.addCleanup(self._dir.cleanup)

    def _worker(self, entries):
        from workers.missing_file_worker import MissingFileWorker
        return MissingFileWorker(entries)

    def _touch(self, name):
        path = os.path.join(self._dir.name, name)
        with open(path, "wb") as fh:
            fh.write(b"x")
        return path

    def _run(self, worker):
        results = []
        worker.finished_checking.connect(results.append)
        worker.run()
        return results

    def test_the_missing_paths_are_reported_and_the_present_ones_are_not(self):
        present = self._touch("here.png")
        absent = os.path.join(self._dir.name, "gone.png")

        results = self._run(self._worker([_entry(present), _entry(absent)]))

        self.assertEqual(results, [{absent}])

    def test_an_empty_session_still_emits_once(self):
        # Session load waits on this signal to clear the "checking…"
        # state. Returning early without emitting would leave it there.
        self.assertEqual(self._run(self._worker([])), [set()])

    def test_nothing_missing_emits_an_empty_set_rather_than_None(self):
        results = self._run(self._worker([_entry(self._touch("here.png"))]))
        self.assertEqual(results, [set()])

    def test_stop_sets_the_flag(self):
        worker = self._worker([])
        self.assertFalse(worker._stop_requested)
        worker.stop()
        self.assertTrue(worker._stop_requested)

    def test_a_cancelled_check_reports_nothing_missing_rather_than_a_partial_answer(self):
        # The important one, and the reason this is not just "emit what we
        # have". A partial result here would be indistinguishable from a
        # complete one, and the GUI would flag rows as missing on the
        # strength of a scan that was abandoned half way - during app
        # shutdown, which is exactly when this gets cancelled.
        absent = os.path.join(self._dir.name, "gone.png")
        worker = self._worker([_entry(absent)])
        worker.stop()

        self.assertEqual(self._run(worker), [set()])

    def test_the_stop_flag_is_handed_to_the_scan_itself(self):
        # Not just checked afterwards: the scan polls it between
        # directories, which is what makes cancelling a slow network scan
        # actually fast.
        worker = self._worker([_entry("/nowhere/a.png")])
        with patch("workers.missing_file_worker.find_missing_paths",
                   return_value=set()) as scan:
            self._run(worker)

        stop_check = scan.call_args.kwargs["stop_check"]
        self.assertFalse(stop_check())
        worker.stop()
        self.assertTrue(stop_check())

    def test_paths_are_passed_through_in_order(self):
        entries = [_entry("/a/1.png"), _entry("/b/2.png")]
        with patch("workers.missing_file_worker.find_missing_paths",
                   return_value=set()) as scan:
            self._run(self._worker(entries))

        self.assertEqual(scan.call_args.args[0], ["/a/1.png", "/b/2.png"])

    def test_an_unreadable_directory_counts_all_of_its_files_as_missing(self):
        # Unmounted share, deleted Hydrus store: every file under it is
        # unreachable, which is what the caller needs to know.
        gone = os.path.join(self._dir.name, "unmounted")
        entries = [_entry(os.path.join(gone, "a.png")), _entry(os.path.join(gone, "b.png"))]

        results = self._run(self._worker(entries))

        self.assertEqual(results, [{e.path for e in entries}])


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestHydrusTagLookupWorker(unittest.TestCase):
    def _worker(self, entries):
        from core.config import Settings
        from workers.hydrus_lookup_worker import HydrusTagLookupWorker
        return HydrusTagLookupWorker(entries, Settings())

    def _run(self, worker):
        results = []
        worker.done.connect(results.append)
        worker.run()
        return results

    def test_the_count_of_newly_tagged_entries_is_emitted(self):
        with patch("workers.hydrus_lookup_worker.apply_existing_hydrus_tags",
                   return_value=3):
            self.assertEqual(self._run(self._worker([_entry("/a.png")])), [3])

    def test_the_entries_and_settings_are_passed_straight_through(self):
        entries = [_entry("/a.png"), _entry("/b.png")]
        worker = self._worker(entries)
        with patch("workers.hydrus_lookup_worker.apply_existing_hydrus_tags",
                   return_value=0) as apply:
            self._run(worker)

        self.assertEqual(apply.call_args.args, (entries, worker.settings))

    def test_an_empty_batch_still_emits_zero(self):
        with patch("workers.hydrus_lookup_worker.apply_existing_hydrus_tags",
                   return_value=0) as apply:
            self.assertEqual(self._run(self._worker([])), [0])
        apply.assert_called_once()

    def test_a_failure_emits_zero_rather_than_leaving_the_gui_waiting(self):
        # The blanket except is the point of the wrapper. Hydrus being
        # down, or a bug in the lookup, must not strand the caller on a
        # signal that never comes - this runs right after files are added,
        # while the window is showing progress for it. DAN-95: the emitted
        # result also carries the error so it isn't mistaken for a
        # successful lookup that just found nothing.
        with patch("workers.hydrus_lookup_worker.apply_existing_hydrus_tags",
                   side_effect=RuntimeError("Hydrus said no")):
            results = self._run(self._worker([_entry("/a.png")]))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tagged_count, 0)
        self.assertIn("Hydrus said no", results[0].error)

    def test_even_a_bare_exception_is_caught(self):
        with patch("workers.hydrus_lookup_worker.apply_existing_hydrus_tags",
                   side_effect=KeyError("unexpected response shape")):
            results = self._run(self._worker([_entry("/a.png")]))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tagged_count, 0)
        self.assertIsNotNone(results[0].error)


if __name__ == "__main__":
    unittest.main()
