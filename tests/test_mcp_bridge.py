"""gui/mcp_bridge.py: the cross-thread marshaling (MainThreadInvoker) and
the tool handlers built on top of it (McpToolHandlers) - gating, dry_run
and audit wiring, exercised against a minimal fake window rather than a
full MainWindow (DAN-707).

Qt is required here (MainThreadInvoker is a QObject), but no display:
QT_QPA_PLATFORM=offscreen, same as every other gui/ test.
"""
import contextlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import _path  # noqa: F401,E402

from PyQt6.QtCore import QObject, QThread  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from core.config import Settings  # noqa: E402
from core.models import ImageEntry  # noqa: E402
from gui.mcp_bridge import MainThreadInvoker, MainThreadTimeout, McpToolHandlers  # noqa: E402

_app = None


def setUpModule():
    """One QApplication for the whole module - Qt allows only one, and
    it must be a QApplication (not merely a QCoreApplication) in case a
    test file that needs real widgets ends up being the one that
    constructs the process-wide singleton first (tests/test_gui_harness.py
    does the same, for the same reason). This module never calls exec()
    on it - see _gui_thread() below for why."""
    global _app
    _app = QApplication.instance() or QApplication([])


class _GuiThread(QThread):
    """A dedicated Qt event-loop thread standing in for "the GUI thread"
    in production - i.e. the thread that owns a MainThreadInvoker and
    must be running an event loop for its BlockingQueuedConnection to
    ever get delivered.

    Deliberately NOT the shared QApplication singleton's own exec()
    loop: this module is one of ~170 test files sharing a single process
    (`run_tests.sh` runs `unittest discover`), and by the time this
    module's tests run, the suite has already constructed and torn down
    several hundred MainWindows (tests/test_gui_smoke.py) with their own
    QTimers and worker QThreads. Calling the GLOBAL app's exec() here
    then has to drain whatever of that backlog is still queued before
    it can ever reach this test's own queued call - observed to hang for
    minutes rather than the fraction of a second this test actually
    needs. A thread with its own private event loop sidesteps that
    backlog entirely: nothing but this test ever queues anything on it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.invoker: "MainThreadInvoker | None" = None
        self.python_thread_ident: "threading.Thread | None" = None
        self.ready = threading.Event()

    def run(self) -> None:
        # MainThreadInvoker must be built ON this thread - a QObject's
        # signal/slot thread affinity is set at construction, not by
        # which thread happens to call a method on it afterwards.
        self.invoker = MainThreadInvoker()
        self.python_thread_ident = threading.current_thread()
        self.ready.set()
        self.exec()


@contextlib.contextmanager
def _gui_thread():
    thread = _GuiThread()
    thread.start()
    if not thread.ready.wait(timeout=5):
        raise RuntimeError("the test's GUI thread did not start in time")
    try:
        yield thread
    finally:
        thread.quit()
        thread.wait(2000)


def _call_from_worker_thread(invoker, fn, timeout=None):
    """Calls invoker.call(fn) from a throwaway background thread (never
    from this test method's own thread, which is Python's real main
    thread and would otherwise take MainThreadInvoker's same-thread
    shortcut) and returns (result, error)."""
    box = {}

    def worker():
        try:
            kwargs = {} if timeout is None else {"timeout": timeout}
            box["result"] = invoker.call(fn, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported back to the test, not swallowed
            box["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    return box.get("result"), box.get("error")


class TestMainThreadInvoker(unittest.TestCase):
    def test_runs_the_callable_on_the_thread_that_constructed_it(self):
        with _gui_thread() as gui:
            result, error = _call_from_worker_thread(
                gui.invoker, lambda: threading.current_thread())
        self.assertIsNone(error)
        self.assertIs(result, gui.python_thread_ident)

    def test_returns_the_callables_result(self):
        with _gui_thread() as gui:
            result, error = _call_from_worker_thread(gui.invoker, lambda: 1 + 1)
        self.assertIsNone(error)
        self.assertEqual(result, 2)

    def test_an_exception_in_the_callable_is_raised_in_the_caller(self):
        def boom():
            raise ValueError("no good")

        with _gui_thread() as gui:
            _, error = _call_from_worker_thread(gui.invoker, boom)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("no good", str(error))

    def test_a_same_thread_call_runs_directly(self):
        invoker = MainThreadInvoker()
        self.assertEqual(invoker.call(lambda: "direct"), "direct")

    def test_times_out_if_the_callable_runs_longer_than_the_timeout(self):
        def slow():
            time.sleep(0.5)
            return "done"

        with _gui_thread() as gui:
            _, error = _call_from_worker_thread(gui.invoker, slow, timeout=0.05)
        self.assertIsInstance(error, MainThreadTimeout)


class FakeWindow(QObject):
    """Just enough of MainWindow's surface for McpToolHandlers to drive -
    see gui/review_shortcuts.py for the real methods these mirror."""

    def __init__(self, entries):
        super().__init__()
        self.entries = entries
        self.settings = Settings()
        self.worker = None
        self.calls = []

    def _refresh_entry_row(self, entry):
        self.calls.append(("refresh_entry_row", entry))

    def _refresh_table(self):
        self.calls.append(("refresh_table",))

    def _on_selection_changed(self):
        self.calls.append(("on_selection_changed",))

    def _current_entry(self):
        return None

    def _set_rows_reviewed(self, entries, reviewed):
        for entry in entries:
            entry.reviewed = reviewed
        self.calls.append(("set_rows_reviewed", list(entries), reviewed))

    def _remove_rows(self, entries):
        ids = {id(e) for e in entries}
        self.entries[:] = [e for e in self.entries if id(e) not in ids]
        self.calls.append(("remove_rows", list(entries)))


def _entry(filename="a.jpg"):
    return ImageEntry(path=f"/tmp/{filename}")


class McpBridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-bridge-")
        self.audit_path = Path(self.tmp.name) / "mcp_audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _handlers(self, entries):
        window = FakeWindow(entries)
        handlers = McpToolHandlers(window)
        handlers.audit_path = self.audit_path
        return window, handlers

    def _audit_entries(self):
        import json
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]


class TestReadTools(McpBridgeTestCase):
    def test_list_queue_reports_every_row_by_default(self):
        entries = [_entry("a.jpg"), _entry("b.jpg")]
        _, handlers = self._handlers(entries)
        result = handlers.list_queue()
        self.assertTrue(result["allowed"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0]["filename"], "a.jpg")

    def test_list_queue_respects_offset_and_limit(self):
        entries = [_entry(f"{i}.jpg") for i in range(5)]
        _, handlers = self._handlers(entries)
        result = handlers.list_queue(offset=2, limit=2)
        self.assertEqual([r["filename"] for r in result["rows"]], ["2.jpg", "3.jpg"])

    def test_list_queue_filter_keyword_unreviewed(self):
        reviewed = _entry("reviewed.jpg")
        reviewed.reviewed = True
        entries = [reviewed, _entry("pending.jpg")]
        _, handlers = self._handlers(entries)
        result = handlers.list_queue(filter_text="unreviewed")
        self.assertEqual([r["filename"] for r in result["rows"]], ["pending.jpg"])

    def test_get_entry_reports_the_requested_row(self):
        _, handlers = self._handlers([_entry("a.jpg")])
        result = handlers.get_entry(0)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["filename"], "a.jpg")

    def test_get_entry_out_of_range_reports_an_error_not_a_crash(self):
        _, handlers = self._handlers([_entry("a.jpg")])
        result = handlers.get_entry(5)
        self.assertTrue(result["allowed"])  # the TOOL was permitted; the ROW was invalid
        self.assertIn("error", result)

    def test_list_actions_reports_every_tool_with_its_gate_state(self):
        _, handlers = self._handlers([])
        result = handlers.list_actions()
        names = {t["tool"] for t in result["tools"]}
        self.assertIn("research", names)
        research = next(t for t in result["tools"] if t["tool"] == "research")
        self.assertFalse(research["enabled"])  # allow_research is off by default
        self.assertIsNotNone(research["refusal_reason"])

    def test_list_actions_reports_hydrus_writes_enabled_by_default(self):
        """allow_hydrus_writes defaults on (DAN-702 board decision) - the
        whole point of this server is unattended Hydrus writes, so
        list_actions must report send_upload as enabled on a fresh
        config, not refused."""
        _, handlers = self._handlers([])
        result = handlers.list_actions()
        send_upload = next(t for t in result["tools"] if t["tool"] == "send_upload")
        self.assertTrue(send_upload["enabled"])
        self.assertIsNone(send_upload["refusal_reason"])


class TestLocalWrites(McpBridgeTestCase):
    def test_select_candidate_switches_the_active_match(self):
        from core.models import MatchCandidate
        entry = _entry("a.jpg")
        entry.candidates = [MatchCandidate(url="https://a"), MatchCandidate(url="https://b")]
        window, handlers = self._handlers([entry])
        result = handlers.select_candidate(0, 1)
        self.assertTrue(result["allowed"])
        self.assertEqual(entry.selected_candidate_index, 1)
        self.assertEqual(entry.matched_url, "https://b")
        self.assertIn(("refresh_entry_row", entry), window.calls)

    def test_toggle_reviewed_sets_the_flag(self):
        entry = _entry("a.jpg")
        _, handlers = self._handlers([entry])
        result = handlers.toggle_reviewed(0, True)
        self.assertTrue(result["allowed"])
        self.assertTrue(entry.reviewed)

    def test_local_writes_are_never_gated(self):
        entry = _entry("a.jpg")
        _, handlers = self._handlers([entry])
        result = handlers.toggle_reviewed(0, True)
        self.assertNotIn("reason", result)


class TestGatingAndAudit(McpBridgeTestCase):
    def test_a_disabled_tier_is_refused_with_the_setting_named(self):
        entry = _entry("a.jpg")
        _, handlers = self._handlers([entry])
        result = handlers.remove_row(0)
        self.assertFalse(result["allowed"])
        self.assertIn('"Let it remove rows and reset results"', result["reason"])
        # the entry must survive - a refusal must never fall through to
        # doing the thing it refused
        self.assertEqual(len([e for e in handlers.window.entries if e is entry]), 1)

    def test_a_hydrus_write_is_refused_without_calling_hydrus_at_all(self):
        """No HydrusClient is ever constructed for a refused call - the
        gate check happens before perform() runs, not inside it.
        allow_hydrus_writes defaults on (DAN-702 board decision), so this
        exercises the user explicitly turning it back off."""
        window, handlers = self._handlers([_entry("a.jpg")])
        window.settings.mcp.allow_hydrus_writes = False
        result = handlers.send_upload(0)
        self.assertFalse(result["allowed"])
        self.assertIn('"Let it send files and tags to Hydrus"', result["reason"])

    def test_a_refusal_is_still_recorded_in_the_audit_log(self):
        _, handlers = self._handlers([_entry("a.jpg")])
        handlers.remove_row(0)
        entries = self._audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["allowed"])
        self.assertEqual(entries[0]["tool"], "remove_row")

    def test_an_allowed_call_is_recorded_too(self):
        _, handlers = self._handlers([_entry("a.jpg")])
        handlers.get_entry(0)
        entries = self._audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["allowed"])


class TestDryRun(McpBridgeTestCase):
    def test_dry_run_changes_nothing_and_says_so(self):
        entry = _entry("a.jpg")
        window, handlers = self._handlers([entry])
        window.settings.mcp.allow_destructive = True
        window.settings.mcp.dry_run = True

        result = handlers.remove_row(0)

        self.assertTrue(result["allowed"])
        self.assertTrue(result["dry_run"])
        self.assertIn(entry, window.entries)   # nothing was actually removed
        self.assertFalse(any(call[0] == "remove_rows" for call in window.calls))

    def test_dry_run_is_still_audited(self):
        window, handlers = self._handlers([_entry("a.jpg")])
        window.settings.mcp.allow_destructive = True
        window.settings.mcp.dry_run = True
        handlers.remove_row(0)
        entries = self._audit_entries()
        self.assertTrue(entries[0]["dry_run"])

    def test_turning_dry_run_off_performs_the_real_action(self):
        entry = _entry("a.jpg")
        window, handlers = self._handlers([entry])
        window.settings.mcp.allow_destructive = True
        window.settings.mcp.dry_run = False

        result = handlers.remove_row(0)

        self.assertTrue(result["allowed"])
        self.assertFalse(result["dry_run"])
        self.assertNotIn(entry, window.entries)

    def test_read_tools_ignore_dry_run(self):
        """dry_run only applies to write tiers - a read must still read."""
        window, handlers = self._handlers([_entry("a.jpg")])
        window.settings.mcp.dry_run = True
        result = handlers.get_entry(0)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["filename"], "a.jpg")
        self.assertNotIn("would_call", result)


if __name__ == "__main__":
    unittest.main()
