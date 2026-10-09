"""DAN-926: wake-payload headroom detector tests.

Fixtures reconstruct the three real calibration points measured against the
live board on 2026-10-09 (see ops/payload_headroom.py's module docstring):
DAN-781 (proxy 97,956 / direct 133,877 -- the one that actually blew),
DAN-647 (proxy 64,971 / direct 76,225), and DAN-926 itself (proxy 9,574 /
direct 33,682). These are not independently re-derived here; they anchor the
ordering assertion the objective asks for.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Optional
from unittest.mock import patch

from ops.payload_headroom import (
    MAX_ARG_STRLEN,
    Headroom,
    PaperclipClient,
    Severity,
    direct_bytes,
    latest_run_id_for_issue,
    main,
    proxy_bytes,
    severity_for,
    sweep,
)


def _comment(body: str) -> dict[str, Any]:
    return {"body": body}


class ProxyBytesTests(unittest.TestCase):
    def test_description_and_comments_sum(self) -> None:
        self.assertEqual(
            proxy_bytes("abc", [_comment("de"), _comment("fgh")]), 3 + 2 + 3
        )

    def test_none_description_counts_as_empty(self) -> None:
        self.assertEqual(proxy_bytes(None, []), 0)

    def test_utf8_multibyte_counted_in_bytes_not_chars(self) -> None:
        # the DAN-348 "null is not evidence" rule em-dash is 3 bytes in UTF-8
        self.assertEqual(proxy_bytes("—", []), 3)


class DirectBytesTests(unittest.TestCase):
    def test_missing_run_is_none(self) -> None:
        self.assertIsNone(direct_bytes(None))

    def test_run_without_paperclip_wake_key_is_none_not_zero(self) -> None:
        # issue_unblock_requested shape: contextSnapshot exists, no paperclipWake key
        run = {"contextSnapshot": {"issueId": "i-1", "wakeReason": "issue_unblock_requested"}}
        self.assertIsNone(direct_bytes(run))

    def test_null_paperclip_wake_value_is_none_not_zero(self) -> None:
        run = {"contextSnapshot": {"paperclipWake": None}}
        self.assertIsNone(direct_bytes(run))

    def test_serializes_compactly(self) -> None:
        run = {"contextSnapshot": {"paperclipWake": {"a": 1, "b": "x"}}}
        # compact separators, no extra whitespace
        self.assertEqual(direct_bytes(run), len('{"a":1,"b":"x"}'))


class SeverityTests(unittest.TestCase):
    def test_below_warn_is_ok(self) -> None:
        self.assertEqual(severity_for(0.69), Severity.OK)

    def test_warn_boundary(self) -> None:
        self.assertEqual(severity_for(0.70), Severity.WARN)

    def test_between_warn_and_alarm(self) -> None:
        self.assertEqual(severity_for(0.85), Severity.WARN)

    def test_alarm_boundary(self) -> None:
        self.assertEqual(severity_for(0.90), Severity.ALARM)

    def test_well_over_ceiling_is_alarm(self) -> None:
        self.assertEqual(severity_for(1.3), Severity.ALARM)


class LatestRunIdForIssueTests(unittest.TestCase):
    def test_picks_newest_matching_run(self) -> None:
        runs = [
            {"id": "old", "createdAt": "2026-10-01T00:00:00Z", "contextSnapshot": {"issueId": "i-1"}},
            {"id": "new", "createdAt": "2026-10-08T00:00:00Z", "contextSnapshot": {"issueId": "i-1"}},
            {"id": "other-issue", "createdAt": "2026-10-09T00:00:00Z", "contextSnapshot": {"issueId": "i-2"}},
        ]
        self.assertEqual(latest_run_id_for_issue(runs, "i-1"), "new")

    def test_no_match_is_none(self) -> None:
        runs = [{"id": "x", "createdAt": "2026-10-08T00:00:00Z", "contextSnapshot": {"issueId": "i-2"}}]
        self.assertIsNone(latest_run_id_for_issue(runs, "i-1"))


class FakePaperclipClient(PaperclipClient):
    """In-memory double; never touches urllib."""

    def __init__(
        self,
        issues: dict[str, dict[str, Any]],
        comments: dict[str, list[dict[str, Any]]],
        runs: list[dict[str, Any]],
        run_details: dict[str, dict[str, Any]],
    ) -> None:
        self._issues = issues
        self._comments = comments
        self._runs = runs
        self._run_details = run_details
        self.get_run_calls: list[str] = []

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        return [{"id": i} for i in self._issues]

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        return self._issues[issue_id]

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return self._comments.get(issue_id, [])

    def list_recent_runs(self, company_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        return self._runs

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        self.get_run_calls.append(run_id)
        return self._run_details.get(run_id)


def _issue(issue_id: str, identifier: str, description: str, status: str = "in_progress") -> dict[str, Any]:
    return {"id": issue_id, "identifier": identifier, "description": description, "status": status}


def _run_index_entry(run_id: str, issue_id: str, created_at: str) -> dict[str, Any]:
    # lean shape: the bulk heartbeat-runs list never carries paperclipWake
    return {"id": run_id, "createdAt": created_at, "contextSnapshot": {"issueId": issue_id}}


def _run_detail(paperclip_wake: Any) -> dict[str, Any]:
    return {"contextSnapshot": {"paperclipWake": paperclip_wake}}


class SweepTests(unittest.TestCase):
    def test_reproduces_dan781_dan647_dan926_ordering(self) -> None:
        # Sized to match the real measured proxy byte counts from the
        # 2026-10-09 calibration, with direct measurements available for
        # DAN-781 and DAN-647 (both crossed the DIRECT_MEASURE threshold)
        # and proxy-only for DAN-926 (below it).
        client = FakePaperclipClient(
            issues={
                "i-781": _issue("i-781", "DAN-781", "d" * 1000, status="cancelled"),
                "i-647": _issue("i-647", "DAN-647", "d" * 1768, status="done"),
                "i-926": _issue("i-926", "DAN-926", "d" * 4931, status="in_progress"),
                "i-quiet": _issue("i-quiet", "DAN-1", "d" * 100, status="todo"),
            },
            comments={
                "i-781": [_comment("c" * (97956 - 1000))],
                "i-647": [_comment("c" * (64971 - 1768))],
                "i-926": [_comment("c" * (9574 - 4931))],
                "i-quiet": [],
            },
            runs=[
                _run_index_entry("run-781", "i-781", "2026-10-08T01:43:08Z"),
                _run_index_entry("run-647", "i-647", "2026-10-09T03:18:58Z"),
                # no run indexed for i-926 or i-quiet
            ],
            run_details={
                "run-781": _run_detail("w" * 133877),
                "run-647": _run_detail("w" * 76225),
            },
        )

        result = sweep(client, "company-1")
        identifiers = [h.identifier for h in result]
        self.assertEqual(identifiers, ["DAN-781", "DAN-647", "DAN-926", "DAN-1"])

        by_id = {h.identifier: h for h in result}
        self.assertEqual(by_id["DAN-781"].method, "direct")
        # +2 for the JSON string quotes direct_bytes's json.dumps adds around
        # this fixture's bare string payload.
        self.assertEqual(by_id["DAN-781"].measured_bytes, 133877 + 2)
        self.assertEqual(by_id["DAN-647"].method, "direct")
        self.assertEqual(by_id["DAN-926"].method, "proxy")
        self.assertEqual(by_id["DAN-926"].measured_bytes, 9574)

    def test_fixture_deliberately_over_the_ceiling_alarms(self) -> None:
        over_line = "x" * (MAX_ARG_STRLEN + 500)
        client = FakePaperclipClient(
            issues={"i-1": _issue("i-1", "DAN-OVER", over_line)},
            comments={"i-1": []},
            runs=[],
            run_details={},
        )
        result = sweep(client, "company-1")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].severity, Severity.ALARM)
        self.assertGreater(result[0].ratio, 1.0)

    def test_direct_measurement_skipped_below_gating_threshold(self) -> None:
        # proxy well under DIRECT_MEASURE_PROXY_RATIO -- get_run must not be
        # called even though a matching run is indexed, so small/healthy
        # issues don't cost an extra API round-trip each.
        client = FakePaperclipClient(
            issues={"i-1": _issue("i-1", "DAN-SMALL", "d" * 100)},
            comments={"i-1": []},
            runs=[_run_index_entry("run-1", "i-1", "2026-10-09T00:00:00Z")],
            run_details={"run-1": _run_detail("w" * 50000)},
        )
        result = sweep(client, "company-1")
        self.assertEqual(result[0].method, "proxy")
        self.assertEqual(client.get_run_calls, [])

    def test_run_present_but_no_paperclip_wake_key_falls_back_to_proxy(self) -> None:
        big_proxy = "d" * int(MAX_ARG_STRLEN * 0.6)
        client = FakePaperclipClient(
            issues={"i-1": _issue("i-1", "DAN-FALLBACK", big_proxy)},
            comments={"i-1": []},
            runs=[_run_index_entry("run-1", "i-1", "2026-10-09T00:00:00Z")],
            run_details={"run-1": {"contextSnapshot": {"wakeReason": "issue_unblock_requested"}}},
        )
        result = sweep(client, "company-1")
        self.assertEqual(result[0].method, "proxy")
        self.assertEqual(result[0].measured_bytes, len(big_proxy))


class MainJsonOutputTests(unittest.TestCase):
    """--json stdout must stay parseable -- the ACTIONABLE line is a human
    convenience, not part of the machine-readable payload."""

    def test_json_mode_emits_only_valid_json_on_stdout(self) -> None:
        alarming = Headroom(
            issue_id="i-1",
            identifier="DAN-OVER",
            status="in_progress",
            proxy_measured=MAX_ARG_STRLEN + 1,
            direct_measured=None,
            direct_run_id=None,
        )
        out, err = io.StringIO(), io.StringIO()
        with patch("ops.payload_headroom.sweep", return_value=[alarming]), \
                patch.dict("os.environ", {"PAPERCLIP_API_KEY": "test-key"}), \
                redirect_stdout(out), redirect_stderr(err):
            rc = main(["--company-id", "c", "--api-base", "http://x", "--json"])
        self.assertEqual(rc, 0)
        parsed = json.loads(out.getvalue())
        self.assertEqual(parsed[0]["identifier"], "DAN-OVER")
        self.assertIn("ACTIONABLE: yes", err.getvalue())
        self.assertNotIn("ACTIONABLE", out.getvalue())


if __name__ == "__main__":
    unittest.main()
