"""DAN-218/289/385/491/720: seat-health watchdog arm-1 logic.

Uses a fake Paperclip client (no network) to verify the crash-streak walk,
the exempt/skip classification, and the file/update/resume/noted_closed
dedupe state machine.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ops.seat_watchdog import (
    PaperclipClient,
    RESUME_MARKER,
    classify_run,
    dedupe_and_file,
    find_findings,
    find_seat_finding,
    run_watchdog,
)

BASE = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)


def _run(
    minutes: float,
    status: str = "failed",
    summary: Optional[str] = "default error text",
    error: Optional[str] = "default error text",
    run_id: Optional[str] = None,
    agent_id: str = "seat-1",
) -> dict[str, Any]:
    ts = (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return {
        "id": run_id or f"run-{minutes}",
        "agentId": agent_id,
        "status": status,
        "startedAt": ts,
        "error": error,
        "resultJson": {"summary": summary} if summary is not None else {},
    }


class ClassifyRunTests(unittest.TestCase):
    def test_succeeded_resets(self) -> None:
        self.assertEqual(classify_run(_run(0, status="succeeded")), "resets")

    def test_cancelled_skips(self) -> None:
        self.assertEqual(classify_run(_run(0, status="cancelled")), "skips")

    def test_bare_crash_with_no_summary_counts(self) -> None:
        run = _run(0, status="failed", summary=None, error="acpx_turn_failed: ...terminal access failure.")
        self.assertEqual(classify_run(run), "counts")

    def test_summary_identical_to_error_counts(self) -> None:
        run = _run(0, status="failed", summary="boom", error="boom")
        self.assertEqual(classify_run(run), "counts")

    def test_distinct_narrative_is_exempt(self) -> None:
        # DAN-171 shape: the agent ran a probe and posted its own summary,
        # distinct from the raw error.
        run = _run(0, status="failed", summary="Ran a probe; confirmed the seat is healthy.", error="boom")
        self.assertEqual(classify_run(run), "skips")

    def test_terminal_limit_failure_is_excluded(self) -> None:
        # Owned by ops/limit-triage.py, not this watchdog, even though the
        # summary/error shape otherwise reads as a bare crash.
        run = _run(
            0,
            status="failed",
            summary="ACP agent reported a terminal limit failure.",
            error="ACP agent reported a terminal limit failure.",
        )
        self.assertEqual(classify_run(run), "skips")


class FindSeatFindingTests(unittest.TestCase):
    def test_three_crashes_spanning_ten_minutes_fires(self) -> None:
        runs_newest_first = [
            _run(20, error="acpx_turn_failed: ...terminal access failure.", summary=None),
            _run(10, error="acpx_turn_failed: ...terminal access failure.", summary=None),
            _run(0, error="acpx_turn_failed: ...terminal access failure.", summary=None),
        ]
        finding = find_seat_finding("seat-1", runs_newest_first)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.streak_length, 3)
        self.assertGreaterEqual(finding.span_minutes, 10)

    def test_three_crashes_under_ten_minutes_stays_silent(self) -> None:
        runs_newest_first = [
            _run(2, error="boom", summary=None),
            _run(1, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        self.assertIsNone(find_seat_finding("seat-1", runs_newest_first))

    def test_three_probes_stay_silent(self) -> None:
        # Each run has its own distinct narrative -- exempt, never counts.
        runs_newest_first = [
            _run(20, summary="probed and confirmed healthy", error="boom"),
            _run(10, summary="probed and confirmed healthy", error="boom"),
            _run(0, summary="probed and confirmed healthy", error="boom"),
        ]
        self.assertIsNone(find_seat_finding("seat-1", runs_newest_first))

    def test_succeeded_run_breaks_the_streak(self) -> None:
        runs_newest_first = [
            _run(30, error="boom", summary=None),
            _run(20, error="boom", summary=None),
            _run(10, status="succeeded"),
            _run(0, error="boom", summary=None),
        ]
        # Only one crash run (at minute 0) follows the succeeded run --
        # not enough for a finding, even though 3 crashes exist overall.
        self.assertIsNone(find_seat_finding("seat-1", runs_newest_first))

    def test_cancelled_and_exempt_runs_do_not_break_the_streak(self) -> None:
        runs_newest_first = [
            _run(30, error="boom", summary=None),
            _run(20, status="cancelled"),
            _run(15, summary="probed, all clear", error="boom"),
            _run(10, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        finding = find_seat_finding("seat-1", runs_newest_first)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.streak_length, 3)

    def test_dan_206_cloud_band_regression(self) -> None:
        # Regression fixture for the real DAN-206 incident: 14 consecutive
        # failed starts on the ACP terminal-access fault, an 18h
        # company-wide stall. Reconstructed from the incident's own
        # documented shape (bare acpx_turn_failed crashes, no narrative)
        # since the original ops/ directory that observed it live is gone
        # (DAN-1054) -- this fixture pins the CONTRACT (14 bare crashes
        # over hours must fire), not the literal historical rows.
        runs_newest_first = [
            _run(
                13 * 60 - i * 60,
                error="acpx_turn_failed: ...terminal access failure.",
                summary=None,
                run_id=f"cloud-band-{i}",
                agent_id="cloud-seat",
            )
            for i in range(14)
        ]
        finding = find_seat_finding("cloud-seat", runs_newest_first)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.streak_length, 14)
        self.assertGreaterEqual(finding.span_minutes, 10)


class FindFindingsTests(unittest.TestCase):
    def test_groups_by_seat_independently(self) -> None:
        runs = [
            _run(20, error="boom", summary=None, agent_id="seat-a"),
            _run(10, error="boom", summary=None, agent_id="seat-a"),
            _run(0, error="boom", summary=None, agent_id="seat-a"),
            _run(5, status="succeeded", agent_id="seat-b"),
        ]
        findings = find_findings(runs)
        self.assertEqual([f.agent_id for f in findings], ["seat-a"])


class FakePaperclipClient(PaperclipClient):
    """Same interface as PaperclipClient, backed by in-memory fixtures."""

    def __init__(self, runs: list[dict[str, Any]], trackers: Optional[list[dict[str, Any]]] = None) -> None:
        self._runs = runs
        self._trackers = {t["id"]: dict(t) for t in (trackers or [])}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self.created: list[dict[str, Any]] = []
        self.commented: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, str]] = []

    def list_recent_runs(self, company_id: str, limit: int) -> list[dict[str, Any]]:
        return list(self._runs)

    def find_tracker(self, company_id: str, agent_id: str) -> Optional[dict[str, Any]]:
        marker = f"[{agent_id}]"
        for tracker in self._trackers.values():
            if marker in tracker.get("title", ""):
                return dict(tracker)
        return None

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._comments.get(issue_id, []))

    def create_tracker(
        self, company_id: str, project_id: str, assignee_agent_id: str, title: str, description: str
    ) -> dict[str, Any]:
        issue = {"id": f"new-{len(self._trackers)}", "title": title, "status": "todo"}
        self._trackers[issue["id"]] = issue
        self.created.append(issue)
        return issue

    def comment(self, issue_id: str, body: str) -> Any:
        self._comments.setdefault(issue_id, []).append({"body": body})
        self.commented.append((issue_id, body))
        return {"id": "c1", "body": body}

    def resume(self, issue_id: str, comment: str) -> Any:
        self._trackers[issue_id]["status"] = "todo"
        self._comments.setdefault(issue_id, []).append({"body": comment})
        self.resumed.append((issue_id, comment))
        return self._trackers[issue_id]


class DedupeAndFileTests(unittest.TestCase):
    def _finding(self) -> Any:
        runs_newest_first = [
            _run(20, error="boom", summary=None),
            _run(10, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        finding = find_seat_finding("seat-1", runs_newest_first)
        assert finding is not None
        return finding

    def test_files_new_tracker_when_none_exists(self) -> None:
        client = FakePaperclipClient(runs=[])
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", self._finding())
        self.assertEqual(action, "filed")
        self.assertEqual(len(client.created), 1)

    def test_updates_open_tracker(self) -> None:
        client = FakePaperclipClient(
            runs=[], trackers=[{"id": "t1", "title": "Seat-health watchdog: crash-loop on seat [seat-1]", "status": "in_progress"}]
        )
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", self._finding())
        self.assertEqual(action, "updated")
        self.assertEqual(len(client.commented), 1)

    def test_resumes_closed_tracker_once(self) -> None:
        client = FakePaperclipClient(
            runs=[], trackers=[{"id": "t1", "title": "Seat-health watchdog: crash-loop on seat [seat-1]", "status": "done"}]
        )
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", self._finding())
        self.assertEqual(action, "resumed")
        self.assertEqual(len(client.resumed), 1)

    def test_dan_329_dan_336_closed_tracker_only_resumes_once(self) -> None:
        # Regression fixture for the DAN-329/DAN-336 shape: a tracker that
        # was already auto-resumed once before must NOT be resumed again
        # on a later re-occurrence -- it only gets a plain comment.
        client = FakePaperclipClient(
            runs=[],
            trackers=[{"id": "t1", "title": "Seat-health watchdog: crash-loop on seat [seat-1]", "status": "done"}],
        )
        client._comments["t1"] = [{"body": f"{RESUME_MARKER} first re-occurrence"}]
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", self._finding())
        self.assertEqual(action, "noted_closed")
        self.assertEqual(len(client.resumed), 0)
        self.assertEqual(len(client.commented), 1)


class RunWatchdogTests(unittest.TestCase):
    def test_dry_run_reports_findings_without_filing(self) -> None:
        runs = [
            _run(20, error="boom", summary=None),
            _run(10, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        client = FakePaperclipClient(runs=runs)
        report = run_watchdog(client, "co", limit=300, file_findings=False)
        self.assertEqual(len(report["findings"]), 1)
        self.assertFalse(report["actionable"])
        self.assertEqual(client.created, [])

    def test_actionable_true_only_for_filed(self) -> None:
        runs = [
            _run(20, error="boom", summary=None),
            _run(10, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        client = FakePaperclipClient(runs=runs)
        report = run_watchdog(
            client, "co", limit=300, file_findings=True, project_id="proj", assignee_agent_id="watchdog-agent"
        )
        self.assertTrue(report["actionable"])
        self.assertEqual(report["findings"][0]["action"], "filed")

    def test_is_actionable_true_only_for_filed_not_updated(self) -> None:
        runs = [
            _run(20, error="boom", summary=None),
            _run(10, error="boom", summary=None),
            _run(0, error="boom", summary=None),
        ]
        client = FakePaperclipClient(
            runs=runs,
            trackers=[{"id": "t1", "title": "Seat-health watchdog: crash-loop on seat [seat-1]", "status": "in_progress"}],
        )
        report = run_watchdog(
            client, "co", limit=300, file_findings=True, project_id="proj", assignee_agent_id="watchdog-agent"
        )
        self.assertFalse(report["actionable"])
        self.assertEqual(report["findings"][0]["action"], "updated")

    def test_no_findings_is_not_actionable(self) -> None:
        client = FakePaperclipClient(runs=[_run(0, status="succeeded")])
        report = run_watchdog(client, "co", limit=300, file_findings=False)
        self.assertEqual(report["findings"], [])
        self.assertFalse(report["actionable"])


if __name__ == "__main__":
    unittest.main()
