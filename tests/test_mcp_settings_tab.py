"""The MCP tab in Settings (DAN-708): persistence, the six connection
states, the one-time enable summary's construction-safety, and the
Recent-tool-calls table's rendering off the real audit log format -
exercised against mcp-settings-spec.md's own acceptance criteria (§11).

Qt is required (the dialog is a QWidget tree), but no display:
QT_QPA_PLATFORM=offscreen, same as every other gui/ test.
"""
import os
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from . import _path  # noqa: F401,E402

from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from core import mcp_audit  # noqa: E402
from core.config import Settings  # noqa: E402
from core.mcp_server import McpServerController  # noqa: E402
from gui.settings_dialog import (  # noqa: E402
    SettingsDialog, _mcp_outcome_cell, _mcp_target_cell, _mcp_time_cell,
    _read_mcp_audit_entries,
)

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication([])


class _FakeHandlers:
    """Just enough of gui.mcp_bridge.McpToolHandlers for
    McpServerController.stop() (called by restart()) to not blow up."""

    class invoker:
        @staticmethod
        def shutdown():
            pass


class _FakeMainWindow(QWidget):
    """A stand-in for MainWindow: holds `settings` and `_mcp_server`, the
    two attributes the MCP tab actually reaches for (mcp-settings-
    spec.md §10 risk 1)."""

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._mcp_server = McpServerController(_FakeHandlers())
        self.view_logs_calls = 0

    def action_view_logs(self):
        self.view_logs_calls += 1


class TestConstructionWithNoMainWindow(unittest.TestCase):
    """Every existing gui-harness/smoke test builds SettingsDialog with no
    parent at all - the MCP tab must not crash or assume one exists."""

    def test_builds_without_a_parent(self):
        dialog = SettingsDialog(Settings())
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.mcp_connection_primary.text(), "Stopped.")

    def test_enabled_defaults_off_and_hydrus_writes_defaults_on(self):
        # mcp-settings-spec.md §11: allow_hydrus_writes on, the other two off.
        dialog = SettingsDialog(Settings())
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.mcp_enabled.isChecked())
        self.assertTrue(dialog.mcp_allow_hydrus_writes.isChecked())
        self.assertFalse(dialog.mcp_allow_research.isChecked())
        self.assertFalse(dialog.mcp_allow_destructive.isChecked())


class TestApplyToSettings(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.dialog = SettingsDialog(self.settings)
        self.addCleanup(self.dialog.deleteLater)
        # Checking "Enable the MCP server" below fires §5a's one-time
        # summary, which is a real modal QMessageBox.exec() - neutralize
        # it here since this test is about persistence, not the popup
        # (TestEnableSummaryConstructionSafety covers the popup itself).
        self.dialog._show_mcp_enable_summary = lambda: None

    def test_round_trips_every_mcp_field(self):
        d = self.dialog
        d.mcp_enabled.setChecked(True)
        d.mcp_port.setValue(9001)
        d.mcp_token.setText("a-token")
        d.mcp_dry_run.setChecked(True)
        d.mcp_allow_research.setChecked(True)
        d.mcp_allow_hydrus_writes.setChecked(False)
        d.mcp_allow_destructive.setChecked(True)
        d.mcp_audit_max_mb.setValue(42)

        d.apply_to_settings()

        mcp = self.settings.mcp
        self.assertTrue(mcp.enabled)
        self.assertEqual(mcp.port, 9001)
        self.assertEqual(mcp.token, "a-token")
        self.assertTrue(mcp.dry_run)
        self.assertTrue(mcp.allow_research)
        self.assertFalse(mcp.allow_hydrus_writes)
        self.assertTrue(mcp.allow_destructive)
        self.assertEqual(mcp.audit_log_max_bytes, 42_000_000)


class TestEnableSummaryConstructionSafety(unittest.TestCase):
    """mcp-settings-spec.md §5a / §10 risk 5: the one-time summary must
    fire on a real click of the server checkbox and never on the dialog
    merely restoring a saved enabled=True state."""

    def test_does_not_fire_when_restoring_a_saved_enabled_state(self):
        settings = Settings()
        settings.mcp.enabled = True
        settings.mcp.token = "x"
        dialog = SettingsDialog(settings)
        self.addCleanup(dialog.deleteLater)
        calls = []
        dialog._show_mcp_enable_summary = lambda: calls.append(1)
        # Construction already happened by this point; nothing should
        # have fired.
        self.assertEqual(calls, [])

    def test_fires_exactly_once_on_a_real_off_to_on_click(self):
        dialog = SettingsDialog(Settings())
        self.addCleanup(dialog.deleteLater)
        calls = []
        dialog._show_mcp_enable_summary = lambda: calls.append(1)
        dialog.mcp_enabled.setChecked(True)
        self.assertEqual(calls, [1])
        dialog.mcp_enabled.setChecked(False)
        self.assertEqual(calls, [1])  # off->on only, not on->off


class TestConnectionStates(unittest.TestCase):
    """mcp-settings-spec.md §3's six states, driven through a fake
    McpServerController so no real socket is ever touched."""

    def setUp(self):
        self.settings = Settings()
        self.settings.mcp.enabled = True
        self.settings.mcp.token = "abc123"
        self.window = _FakeMainWindow(self.settings)
        self.addCleanup(self.window.deleteLater)
        self.dialog = SettingsDialog(self.settings, self.window)
        self.addCleanup(self.dialog.deleteLater)

    def test_state_1_stopped_when_not_enabled(self):
        self.dialog.mcp_enabled.setChecked(False)
        self.dialog._refresh_mcp_connection()
        self.assertEqual(self.dialog.mcp_connection_primary.text(), "Stopped.")
        self.assertTrue(self.dialog.mcp_restart_btn.isHidden())

    def test_state_2_listening_when_running(self):
        self.window._mcp_server._thread = unittest.mock.Mock(is_alive=lambda: True)
        self.dialog._refresh_mcp_connection()
        self.assertIn("Listening on 127.0.0.1:", self.dialog.mcp_connection_primary.text())

    def test_state_3_mcp_package_not_installed(self):
        self.window._mcp_server.last_error = (
            "MCP support not installed - run `venv/bin/pip install mcp` to enable it")
        self.dialog._refresh_mcp_connection()
        self.assertIn("isn't installed", self.dialog.mcp_connection_primary.text())
        self.assertIn("venv/bin/pip install mcp", self.dialog.mcp_connection_remedy.text())

    def test_state_4_no_token(self):
        self.window._mcp_server.last_error = "Blank bearer token: refusing to start the MCP server"
        self.dialog._refresh_mcp_connection()
        self.assertIn("no token is set", self.dialog.mcp_connection_primary.text())

    def test_state_5_port_busy(self):
        self.window._mcp_server.last_error = (
            "MCP server did not start listening on port 8787 within 5s (port already in use?)")
        self.dialog._refresh_mcp_connection()
        self.assertIn("already in use", self.dialog.mcp_connection_primary.text())
        self.assertIn('Restart server', self.dialog.mcp_connection_remedy.text())

    def test_state_6_honest_fallback_shows_open_log_button(self):
        self.window._mcp_server.last_error = "Could not start the MCP server: weird exception"
        self.dialog._refresh_mcp_connection()
        self.assertEqual(
            self.dialog.mcp_connection_primary.text(), "Could not start the MCP server: weird exception")
        self.assertFalse(self.dialog.mcp_open_log_btn.isHidden())
        self.dialog.mcp_open_log_btn.click()
        self.assertEqual(self.window.view_logs_calls, 1)


class TestRestartServer(unittest.TestCase):
    def test_restart_uses_the_typed_port_and_token_not_the_saved_ones(self):
        settings = Settings()
        settings.mcp.enabled = True
        settings.mcp.token = "saved-token"
        settings.mcp.port = 8787
        window = _FakeMainWindow(settings)
        self.addCleanup(window.deleteLater)
        dialog = SettingsDialog(settings, window)
        self.addCleanup(dialog.deleteLater)

        dialog.mcp_token.setText("typed-token")
        dialog.mcp_port.setValue(18787)

        calls = []
        window._mcp_server.restart = lambda s: calls.append(s) or False
        dialog._mcp_restart_server()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].token, "typed-token")
        self.assertEqual(calls[0].port, 18787)
        # The saved settings object must not have been mutated by a probe.
        self.assertEqual(settings.mcp.token, "saved-token")
        self.assertEqual(settings.mcp.port, 8787)


class TestAuditOutcomeFormatting(unittest.TestCase):
    """mcp-settings-spec.md §7's Outcome-column table, against the real
    shapes core/mcp_audit.record()/gui/mcp_bridge.py actually write."""

    def test_refused_call(self):
        entry = {"allowed": False, "tier": "research", "reason": "irrelevant to the cell text"}
        text, weight = _mcp_outcome_cell(entry)
        self.assertEqual(text, "Refused — Re-search is off")
        self.assertEqual(weight, "ink_100")

    def test_dry_run(self):
        entry = {"allowed": True, "dry_run": True, "tier": "hydrus_write",
                 "outcome": {"note": "dry_run is on: nothing was changed."}}
        text, weight = _mcp_outcome_cell(entry)
        self.assertEqual(text, "Recorded, not run — dry run is on")
        self.assertEqual(weight, "ink_100")

    def test_successful_hydrus_send_gets_the_trailing_glyph(self):
        entry = {"allowed": True, "dry_run": False, "tool": "send_upload",
                 "outcome": {"row": 3, "success": True, "warning": None, "error": None}}
        text, weight = _mcp_outcome_cell(entry)
        self.assertTrue(text.startswith("Allowed — sent"))
        self.assertIn("●", text)
        self.assertEqual(weight, "ink_65")

    def test_refused_hydrus_send_has_no_glyph(self):
        entry = {"allowed": False, "tier": "hydrus_write", "reason": "x"}
        text, _ = _mcp_outcome_cell(entry)
        self.assertNotIn("●", text)

    def test_select_candidate(self):
        entry = {"allowed": True, "dry_run": False, "tool": "select_candidate",
                 "outcome": {"row": 1, "selected_candidate_index": 2}}
        text, _ = _mcp_outcome_cell(entry)
        self.assertEqual(text, "Allowed — picked candidate 2")

    def test_toggle_reviewed(self):
        entry = {"allowed": True, "dry_run": False, "tool": "toggle_reviewed",
                 "outcome": {"row": 1, "reviewed": True}}
        text, _ = _mcp_outcome_cell(entry)
        self.assertEqual(text, "Allowed — marked reviewed")

    def test_remove_row_and_reset_result(self):
        self.assertEqual(
            _mcp_outcome_cell({"allowed": True, "dry_run": False, "tool": "remove_row",
                                "outcome": {"row": 1, "removed": "x.png"}})[0],
            "Allowed — row removed")
        self.assertEqual(
            _mcp_outcome_cell({"allowed": True, "dry_run": False, "tool": "reset_result",
                                "outcome": {"row": 1, "reset": True}})[0],
            "Allowed — result reset")


class TestTargetCell(unittest.TestCase):
    def test_row_and_candidate(self):
        self.assertEqual(_mcp_target_cell({"row": 482, "candidate_index": 2}), "#482 → candidate 2")

    def test_row_only(self):
        self.assertEqual(_mcp_target_cell({"row": 482, "candidate_index": None}), "#482")

    def test_no_row_is_an_em_dash(self):
        self.assertEqual(_mcp_target_cell({"row": None, "candidate_index": None}), "—")


class TestTimeCell(unittest.TestCase):
    def test_a_past_date_shows_the_month_and_day(self):
        self.assertEqual(_mcp_time_cell("2020-01-02T03:04:05Z"), "Jan 2, 03:04:05")

    def test_an_unparseable_timestamp_is_shown_verbatim_not_raised(self):
        self.assertEqual(_mcp_time_cell("garbage"), "garbage")


class TestReadAuditLog(unittest.TestCase):
    """Reading a log the server may still be appending to must not raise
    on a partially-written final line."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-audit-tab-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mcp_audit.jsonl"

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(_read_mcp_audit_entries(self.path), [])

    def test_a_truncated_final_line_is_dropped_not_fatal(self):
        self.path.write_text(
            '{"tool": "list_queue", "tier": "read"}\n'
            '{"tool": "send_upload", "tier": "hydrus_writ'  # mid-write, no closing brace/newline
        )
        entries = _read_mcp_audit_entries(self.path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "list_queue")


class TestAuditTableFiltering(unittest.TestCase):
    """The table against the real mcp_audit.record() format, including
    the default Actions/All filter (mcp-settings-spec.md §7)."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-audit-tab-")
        self.addCleanup(self.tmp.cleanup)
        self.audit_path = Path(self.tmp.name) / "mcp_audit.jsonl"
        self._patch = unittest.mock.patch.object(mcp_audit, "AUDIT_LOG_FILE", self.audit_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        mcp_audit.record(tool="list_queue", tier="read", allowed=True,
                          outcome={"rows": []}, path=self.audit_path)
        mcp_audit.record(tool="select_candidate", tier="local_write", allowed=True,
                          row=1, outcome={"row": 1, "selected_candidate_index": 0},
                          path=self.audit_path)

        self.dialog = SettingsDialog(Settings())
        self.addCleanup(self.dialog.deleteLater)

    def test_actions_filter_hides_reads_by_default(self):
        self.dialog._refresh_mcp_audit_table()
        table = self.dialog.mcp_audit_table
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 1).text(), "select_candidate")

    def test_all_calls_filter_shows_everything(self):
        self.dialog.mcp_audit_filter_buttons["all"].setChecked(True)
        self.dialog._refresh_mcp_audit_table()
        self.assertEqual(self.dialog.mcp_audit_table.rowCount(), 2)

    def test_newest_first(self):
        self.dialog.mcp_audit_filter_buttons["all"].setChecked(True)
        self.dialog._refresh_mcp_audit_table()
        table = self.dialog.mcp_audit_table
        self.assertEqual(table.item(0, 1).text(), "select_candidate")
        self.assertEqual(table.item(1, 1).text(), "list_queue")


if __name__ == "__main__":
    unittest.main()
