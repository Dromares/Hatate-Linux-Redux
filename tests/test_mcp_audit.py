"""The MCP audit log: append, redaction, and rotation (DAN-707).

"Every call is logged, including refusals and dry runs" is the whole
point of this file existing - the morning-after question needs the
calls that were REFUSED as much as the ones that ran.
"""
import json
import tempfile
import unittest
from pathlib import Path

from . import _path  # noqa: F401
from core import mcp_audit


class TestAppending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-audit-")
        self.path = Path(self.tmp.name) / "mcp_audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_creates_the_file_and_its_parent_directory(self):
        nested = Path(self.tmp.name) / "nested" / "mcp_audit.jsonl"
        mcp_audit.record(tool="list_queue", tier="read", allowed=True, path=nested)
        self.assertTrue(nested.exists())

    def test_one_json_object_per_line(self):
        mcp_audit.record(tool="list_queue", tier="read", allowed=True, path=self.path)
        mcp_audit.record(tool="get_entry", tier="read", allowed=True, row=3, path=self.path)
        lines = self._lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["tool"], "list_queue")
        self.assertEqual(lines[1]["row"], 3)

    def test_every_entry_has_a_timestamp(self):
        mcp_audit.record(tool="list_queue", tier="read", allowed=True, path=self.path)
        self.assertIn("timestamp", self._lines()[0])

    def test_a_refusal_is_logged_with_its_reason(self):
        mcp_audit.record(
            tool="send_upload", tier="hydrus_write", allowed=False,
            reason="send_upload is disabled: enable Settings > MCP > Allow Hydrus writes",
            path=self.path,
        )
        entry = self._lines()[0]
        self.assertFalse(entry["allowed"])
        self.assertIn("disabled", entry["reason"])

    def test_a_dry_run_is_logged_as_such(self):
        mcp_audit.record(
            tool="remove_row", tier="destructive", allowed=True, dry_run=True,
            outcome={"would_call": "remove_row"}, path=self.path,
        )
        self.assertTrue(self._lines()[0]["dry_run"])

    def test_candidate_index_is_recorded_when_given(self):
        mcp_audit.record(
            tool="select_candidate", tier="local_write", allowed=True,
            row=1, candidate_index=2, path=self.path,
        )
        self.assertEqual(self._lines()[0]["candidate_index"], 2)


class TestRedaction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-audit-")
        self.path = Path(self.tmp.name) / "mcp_audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self):
        return json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])

    def test_a_bearer_token_in_arguments_is_redacted(self):
        mcp_audit.record(
            tool="list_queue", tier="read", allowed=True,
            arguments={"token": "super-secret-value"}, path=self.path,
        )
        entry = self._entry()
        self.assertEqual(entry["arguments"]["token"], mcp_audit.REDACTED_VALUE)
        self.assertNotIn("super-secret-value", json.dumps(entry))

    def test_a_hydrus_access_key_in_the_outcome_is_redacted(self):
        mcp_audit.record(
            tool="send_upload", tier="hydrus_write", allowed=True,
            outcome={"access_key": "0123456789abcdef"}, path=self.path,
        )
        entry = self._entry()
        self.assertEqual(entry["outcome"]["access_key"], mcp_audit.REDACTED_VALUE)

    def test_redaction_reaches_nested_dicts(self):
        mcp_audit.record(
            tool="list_queue", tier="read", allowed=True,
            arguments={"settings": {"hydrus": {"access_key": "secret"}}}, path=self.path,
        )
        entry = self._entry()
        self.assertEqual(
            entry["arguments"]["settings"]["hydrus"]["access_key"], mcp_audit.REDACTED_VALUE)

    def test_redaction_is_case_insensitive(self):
        mcp_audit.record(
            tool="list_queue", tier="read", allowed=True,
            arguments={"Token": "secret"}, path=self.path,
        )
        self.assertEqual(self._entry()["arguments"]["Token"], mcp_audit.REDACTED_VALUE)

    def test_ordinary_arguments_are_left_alone(self):
        mcp_audit.record(
            tool="get_entry", tier="read", allowed=True,
            arguments={"row": 5}, path=self.path,
        )
        self.assertEqual(self._entry()["arguments"]["row"], 5)


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hatate-mcp-audit-")
        self.path = Path(self.tmp.name) / "mcp_audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotates_once_the_cap_is_reached(self):
        mcp_audit.record(tool="list_queue", tier="read", allowed=True,
                         path=self.path, max_bytes=1)
        first_size = self.path.stat().st_size
        self.assertGreater(first_size, 0)

        mcp_audit.record(tool="get_entry", tier="read", allowed=True,
                         path=self.path, max_bytes=1)

        rotated = self.path.with_name(self.path.name + ".1")
        self.assertTrue(rotated.exists())
        self.assertEqual(rotated.stat().st_size, first_size)
        # The live file now holds only the entry that triggered rotation.
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["tool"], "get_entry")

    def test_stays_in_one_file_below_the_cap(self):
        for _ in range(5):
            mcp_audit.record(tool="list_queue", tier="read", allowed=True,
                             path=self.path, max_bytes=5_000_000)
        rotated = self.path.with_name(self.path.name + ".1")
        self.assertFalse(rotated.exists())
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
