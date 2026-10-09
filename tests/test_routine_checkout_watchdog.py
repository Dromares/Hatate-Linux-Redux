"""DAN-645/DAN-666: cross-routine no-checkout detection arm.

Uses fakes (no network) to verify the cadence read, the self-exclusion,
the never-checked-out streak walk (fresh per-issue, never cached), the
2x-cadence threshold, and the filed/updated(+escalate)/resumed/
noted_closed dedupe actions.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ops.routine_checkout_watchdog import (
    CLOUD_AGENT_ID,
    CLOUD_MENTION_MARKER,
    PaperclipClient,
    RESUME_MARKER,
    SELF_ROUTINE_ID,
    dedupe_and_file,
    evaluate_routine,
    find_checkout_streak,
    find_findings,
    is_never_checked_out,
    routine_cadence_minutes,
)

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _routine(
    routine_id: str = "routine-1",
    title: str = "Default-approver gate sweep",
    owner: Optional[str] = "minos",
    cadence_minutes: float = 60,
    enabled: bool = True,
) -> dict[str, Any]:
    last_fired = NOW - timedelta(minutes=cadence_minutes)
    return {
        "id": routine_id,
        "title": title,
        "assigneeAgentId": owner,
        "status": "active",
        "triggers": [
            {
                "kind": "schedule",
                "enabled": enabled,
                "nextRunAt": _iso(NOW),
                "lastFiredAt": _iso(last_fired),
            }
        ],
    }


def _run(minutes_ago: float, issue_id: Optional[str] = "issue-1", run_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": run_id or f"run-{minutes_ago}",
        "linkedIssueId": issue_id,
        "triggeredAt": _iso(NOW - timedelta(minutes=minutes_ago)),
    }


def _issue(status: str = "in_progress", checkout_run_id: Optional[str] = None, execution_run_id: Optional[str] = None) -> dict[str, Any]:
    return {"status": status, "checkoutRunId": checkout_run_id, "executionRunId": execution_run_id}


class RoutineCadenceTests(unittest.TestCase):
    def test_reads_cadence_from_schedule_trigger(self) -> None:
        self.assertEqual(routine_cadence_minutes(_routine(cadence_minutes=30)), 30)

    def test_none_when_trigger_never_fired(self) -> None:
        routine = _routine()
        routine["triggers"][0]["lastFiredAt"] = None
        self.assertIsNone(routine_cadence_minutes(routine))

    def test_none_with_no_schedule_trigger(self) -> None:
        routine = {"id": "r1", "triggers": [{"kind": "webhook", "enabled": True}]}
        self.assertIsNone(routine_cadence_minutes(routine))


class IsNeverCheckedOutTests(unittest.TestCase):
    def test_true_when_in_progress_with_no_locks(self) -> None:
        self.assertTrue(is_never_checked_out(_issue()))

    def test_false_when_checked_out(self) -> None:
        self.assertFalse(is_never_checked_out(_issue(checkout_run_id="run-x")))

    def test_false_when_not_in_progress(self) -> None:
        self.assertFalse(is_never_checked_out(_issue(status="done")))


class FindCheckoutStreakTests(unittest.TestCase):
    def test_unbroken_streak_of_never_checked_out(self) -> None:
        runs = [_run(0, "i3"), _run(60, "i2"), _run(120, "i1")]
        issues = {"i1": _issue(), "i2": _issue(), "i3": _issue()}
        streak = find_checkout_streak(runs, lambda iid: issues[iid])
        self.assertEqual(len(streak), 3)

    def test_stops_at_first_checked_out_issue(self) -> None:
        runs = [_run(0, "i3"), _run(60, "i2"), _run(120, "i1")]
        issues = {"i1": _issue(), "i2": _issue(checkout_run_id="run-x"), "i3": _issue()}
        streak = find_checkout_streak(runs, lambda iid: issues[iid])
        self.assertEqual(len(streak), 1)

    def test_stops_at_run_with_no_linked_issue(self) -> None:
        # Cause-B shape (DAN-720): a fire that died before creating a tick
        # at all is a different fault, not this arm's concern.
        runs = [_run(0, "i2"), _run(60, None), _run(120, "i1")]
        issues = {"i1": _issue(), "i2": _issue()}
        streak = find_checkout_streak(runs, lambda iid: issues.get(iid))

        self.assertEqual(len(streak), 1)

    def test_refetches_fresh_per_issue_never_cached(self) -> None:
        calls: list[str] = []

        def lookup(issue_id: str) -> dict[str, Any]:
            calls.append(issue_id)
            return _issue()

        find_checkout_streak([_run(0, "i1")], lookup)
        self.assertEqual(calls, ["i1"])


class EvaluateRoutineTests(unittest.TestCase):
    def test_excludes_self_routine(self) -> None:
        routine = _routine(routine_id=SELF_ROUTINE_ID, cadence_minutes=30)
        runs = [_run(m, "i1") for m in (0, 30, 60, 90, 120)]
        finding = evaluate_routine(routine, runs, lambda _i: _issue(), now=NOW)
        self.assertIsNone(finding)

    def test_fires_when_span_exceeds_2x_cadence(self) -> None:
        routine = _routine(cadence_minutes=30)
        runs = [_run(0, "i3"), _run(35, "i2"), _run(70, "i1")]
        issues = {"i1": _issue(), "i2": _issue(), "i3": _issue()}
        finding = evaluate_routine(routine, runs, lambda iid: issues[iid], now=NOW)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.streak_length, 3)
        self.assertGreaterEqual(finding.span_minutes, finding.threshold_minutes)

    def test_single_transient_miss_does_not_false_positive(self) -> None:
        # One never-checked-out run, well inside the 2x-cadence window.
        routine = _routine(cadence_minutes=60)
        runs = [_run(5, "i1")]
        finding = evaluate_routine(routine, runs, lambda _i: _issue(), now=NOW)
        self.assertIsNone(finding)

    def test_none_when_cadence_unknown(self) -> None:
        routine = _routine()
        routine["triggers"][0]["lastFiredAt"] = None
        runs = [_run(m, "i1") for m in (0, 60, 120)]
        finding = evaluate_routine(routine, runs, lambda _i: _issue(), now=NOW)
        self.assertIsNone(finding)

    def test_no_streak_means_no_finding(self) -> None:
        routine = _routine(cadence_minutes=30)
        runs = [_run(0, "i1")]
        finding = evaluate_routine(routine, runs, lambda _i: _issue(checkout_run_id="run-x"), now=NOW)
        self.assertIsNone(finding)


class FindFindingsTests(unittest.TestCase):
    def test_evaluates_each_routine_independently(self) -> None:
        healthy = _routine(routine_id="r-healthy", cadence_minutes=30)
        sick = _routine(routine_id="r-sick", cadence_minutes=30)
        runs_by_routine = {
            "r-healthy": [_run(0, "ih")],
            "r-sick": [_run(0, "is3"), _run(35, "is2"), _run(70, "is1")],
        }
        issues = {"ih": _issue(checkout_run_id="run-x"), "is1": _issue(), "is2": _issue(), "is3": _issue()}
        findings = find_findings([healthy, sick], runs_by_routine, lambda iid: issues[iid], now=NOW)
        self.assertEqual([f.routine_id for f in findings], ["r-sick"])


class FakePaperclipClient(PaperclipClient):
    def __init__(self, trackers: Optional[list[dict[str, Any]]] = None) -> None:
        self._trackers = {t["id"]: dict(t) for t in (trackers or [])}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self.created: list[dict[str, Any]] = []
        self.commented: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, str]] = []

    def find_tracker(self, company_id: str, routine_id: str) -> Optional[dict[str, Any]]:
        marker = f"[{routine_id}]"
        for tracker in self._trackers.values():
            if marker in tracker.get("title", ""):
                return dict(tracker)
        return None

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._comments.get(issue_id, []))

    def create_tracker(
        self, company_id: str, project_id: str, assignee_agent_id: str, title: str, description: str
    ) -> dict[str, Any]:
        issue = {"id": f"new-{len(self._trackers)}", "title": title, "description": description, "status": "todo"}
        self._trackers[issue["id"]] = issue
        self.created.append(issue)
        return issue

    def comment(self, issue_id: str, body: str) -> Any:
        self._comments.setdefault(issue_id, []).append({"body": body, "createdAt": _iso(NOW)})
        self.commented.append((issue_id, body))
        return {"id": "c1"}

    def resume(self, issue_id: str, comment: str) -> Any:
        self._trackers[issue_id]["status"] = "todo"
        self._comments.setdefault(issue_id, []).append({"body": comment, "createdAt": _iso(NOW)})
        self.resumed.append((issue_id, comment))
        return self._trackers[issue_id]


def _finding(routine_id: str = "r-sick") -> Any:
    routine = _routine(routine_id=routine_id, cadence_minutes=30)
    runs = [_run(0, "i3"), _run(35, "i2"), _run(70, "i1")]
    issues = {"i1": _issue(), "i2": _issue(), "i3": _issue()}
    finding = evaluate_routine(routine, runs, lambda iid: issues[iid], now=NOW)
    assert finding is not None
    return finding


class DedupeAndFileTests(unittest.TestCase):
    def test_files_new_tracker_when_none_exists(self) -> None:
        client = FakePaperclipClient()
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "filed")
        self.assertEqual(len(client.created), 1)
        self.assertIn("minos", client.created[0]["description"])

    def test_updates_open_tracker_and_escalates_to_cloud_first_time(self) -> None:
        client = FakePaperclipClient(
            trackers=[{"id": "t1", "title": "Routine checkout watchdog: never-checked-out ticks on routine [r-sick]", "status": "in_progress"}]
        )
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "updated")
        self.assertEqual(len(client.commented), 1)
        self.assertIn(CLOUD_AGENT_ID, client.commented[0][1])

    def test_cloud_escalation_throttled_to_once_per_hour(self) -> None:
        client = FakePaperclipClient(
            trackers=[{"id": "t1", "title": "Routine checkout watchdog: never-checked-out ticks on routine [r-sick]", "status": "in_progress"}]
        )
        client._comments["t1"] = [{"body": f"{CLOUD_MENTION_MARKER} escalated", "createdAt": _iso(NOW - timedelta(minutes=10))}]
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "updated")
        self.assertNotIn(CLOUD_AGENT_ID, client.commented[0][1])

    def test_cloud_escalation_resumes_after_an_hour(self) -> None:
        client = FakePaperclipClient(
            trackers=[{"id": "t1", "title": "Routine checkout watchdog: never-checked-out ticks on routine [r-sick]", "status": "in_progress"}]
        )
        client._comments["t1"] = [{"body": f"{CLOUD_MENTION_MARKER} escalated", "createdAt": _iso(NOW - timedelta(hours=2))}]
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "updated")
        self.assertIn(CLOUD_AGENT_ID, client.commented[0][1])

    def test_resumes_closed_tracker_once(self) -> None:
        client = FakePaperclipClient(
            trackers=[{"id": "t1", "title": "Routine checkout watchdog: never-checked-out ticks on routine [r-sick]", "status": "done"}]
        )
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "resumed")
        self.assertEqual(len(client.resumed), 1)

    def test_noted_closed_after_already_resumed_once(self) -> None:
        client = FakePaperclipClient(
            trackers=[{"id": "t1", "title": "Routine checkout watchdog: never-checked-out ticks on routine [r-sick]", "status": "done"}]
        )
        client._comments["t1"] = [{"body": f"{RESUME_MARKER} first", "createdAt": _iso(NOW)}]
        action = dedupe_and_file(client, "co", "proj", "watchdog-agent", _finding(), now=NOW)
        self.assertEqual(action, "noted_closed")
        self.assertEqual(client.resumed, [])


if __name__ == "__main__":
    unittest.main()
