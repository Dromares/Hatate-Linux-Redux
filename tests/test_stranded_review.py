"""DAN-267/DAN-385: stranded-approval-stage detector.

Uses a fake Paperclip client (no network) to verify the finding predicate,
the dead-execution-phase fallback, the 30-minute threshold, the 6-hour
re-poke dedupe, and the DAN-236/DAN-213 regression fixture.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ops.stranded_review import (
    PaperclipClient,
    POKE_MARKER,
    find_finding,
    poke_if_due,
    run_stranded_review,
)

NOW = datetime(2026, 10, 3, 18, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _issue(
    *,
    status: str = "in_review",
    work_mode: Optional[str] = None,
    execution_status: Optional[str] = "pending",
    participant: Any = "minos",
    pending_since_minutes_ago: float = 45,
    checkout_run_id: Optional[str] = None,
    execution_run_id: Optional[str] = None,
    execution_locked_at: Optional[str] = None,
    last_decision_id: Optional[str] = None,
    last_decision_outcome: Optional[str] = None,
    issue_id: str = "i1",
    identifier: str = "DAN-1",
) -> dict[str, Any]:
    since = _iso(NOW - timedelta(minutes=pending_since_minutes_ago))
    return {
        "id": issue_id,
        "identifier": identifier,
        "status": status,
        "workMode": work_mode,
        "executionState": (
            {
                "status": execution_status,
                "currentParticipant": participant,
                "lastDecisionId": last_decision_id,
                "lastDecisionOutcome": last_decision_outcome,
            }
            if execution_status is not None
            else {}
        ),
        "checkoutRunId": checkout_run_id,
        "executionRunId": execution_run_id,
        "executionLockedAt": execution_locked_at,
        "reviewAttention": {"paths": [{"path": "execution_participant", "since": since}]},
        "updatedAt": since,
    }


def _no_execution_lookup(_issue_id: str) -> Optional[str]:
    raise AssertionError("execution phase should not be fetched when there is no lock")


class FindFindingTests(unittest.TestCase):
    def test_stranded_issue_fires(self) -> None:
        issue = _issue()
        finding = find_finding(issue, NOW, _no_execution_lookup)
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.identifier, "DAN-1")

    def test_pending_stage_with_live_run_stays_silent(self) -> None:
        issue = _issue(checkout_run_id="run-live")
        finding = find_finding(issue, NOW, lambda _id: "running")
        self.assertIsNone(finding)

    def test_locked_but_dead_execution_phase_still_fires(self) -> None:
        issue = _issue(checkout_run_id="run-dead")
        finding = find_finding(issue, NOW, lambda _id: "failed")
        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.dead_execution_phase, "failed")

    def test_pending_stage_younger_than_threshold_stays_silent(self) -> None:
        issue = _issue(pending_since_minutes_ago=5)
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_done_issue_never_fires(self) -> None:
        issue = _issue(status="done")
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_cancelled_issue_never_fires(self) -> None:
        issue = _issue(status="cancelled")
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_routine_execution_issue_never_fires(self) -> None:
        issue = _issue(work_mode="routine_execution")
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_no_current_participant_stays_silent(self) -> None:
        issue = _issue(participant=None)
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_non_pending_execution_state_stays_silent(self) -> None:
        issue = _issue(execution_status="resolved")
        self.assertIsNone(find_finding(issue, NOW, _no_execution_lookup))

    def test_dan_236_dan_213_regression_fixture(self) -> None:
        # Reconstructed from DAN-267's own investigation: in_review, pending
        # execution state, participant Minos, all three lock fields null,
        # stuck for hours. Both must fire.
        for identifier in ("DAN-236", "DAN-213"):
            issue = _issue(
                identifier=identifier,
                issue_id=identifier,
                participant="minos",
                pending_since_minutes_ago=120,
                checkout_run_id=None,
                execution_run_id=None,
                execution_locked_at=None,
            )
            finding = find_finding(issue, NOW, _no_execution_lookup)
            self.assertIsNotNone(finding, f"{identifier} must fire")
            assert finding is not None
            self.assertIsNone(finding.dead_execution_phase)


class FakePaperclipClient(PaperclipClient):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self._issues = issues
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self._phases: dict[str, Optional[str]] = {}
        self.posted: list[tuple[str, str]] = []

    def list_in_review_issues(self, company_id: str, limit: int) -> list[dict[str, Any]]:
        return list(self._issues)

    def get_execution_phase(self, issue_id: str) -> Optional[str]:
        return self._phases.get(issue_id)

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._comments.get(issue_id, []))

    def comment(self, issue_id: str, body: str) -> Any:
        self._comments.setdefault(issue_id, []).append({"body": body, "createdAt": _iso(NOW)})
        self.posted.append((issue_id, body))
        return {"id": "c1"}


class PokeIfDueTests(unittest.TestCase):
    def test_pokes_when_never_poked_before(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        finding = find_finding(issue, NOW, _no_execution_lookup)
        assert finding is not None
        action = poke_if_due(client, finding, NOW, post=True)
        self.assertEqual(action, "poked")
        self.assertEqual(len(client.posted), 1)
        self.assertIn(POKE_MARKER, client.posted[0][1])

    def test_dry_run_never_posts(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        finding = find_finding(issue, NOW, _no_execution_lookup)
        assert finding is not None
        action = poke_if_due(client, finding, NOW, post=False)
        self.assertEqual(action, "poked")
        self.assertEqual(client.posted, [])

    def test_second_tick_inside_six_hours_stays_silent(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        client._comments[issue["id"]] = [{"body": POKE_MARKER, "createdAt": _iso(NOW - timedelta(hours=1))}]
        finding = find_finding(issue, NOW, _no_execution_lookup)
        assert finding is not None
        action = poke_if_due(client, finding, NOW, post=True)
        self.assertEqual(action, "skipped_window")
        self.assertEqual(client.posted, [])

    def test_repokes_after_six_hours(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        client._comments[issue["id"]] = [{"body": POKE_MARKER, "createdAt": _iso(NOW - timedelta(hours=7))}]
        finding = find_finding(issue, NOW, _no_execution_lookup)
        assert finding is not None
        action = poke_if_due(client, finding, NOW, post=True)
        self.assertEqual(action, "poked")
        self.assertEqual(len(client.posted), 1)


class RunStrandedReviewTests(unittest.TestCase):
    def test_actionable_iff_a_finding_was_actually_poked(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        report = run_stranded_review(client, "co", limit=300, post=True, post_comment_on="tick-1", now=NOW)
        self.assertTrue(report["actionable"])
        self.assertEqual(report["findings"][0]["action"], "poked")

    def test_not_actionable_when_only_restating_inside_window(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        client._comments[issue["id"]] = [{"body": POKE_MARKER, "createdAt": _iso(NOW - timedelta(minutes=5))}]
        report = run_stranded_review(client, "co", limit=300, post=True, post_comment_on="tick-1", now=NOW)
        self.assertFalse(report["actionable"])
        self.assertEqual(report["findings"][0]["action"], "skipped_window")

    def test_rollup_posted_on_tick_issue(self) -> None:
        issue = _issue()
        client = FakePaperclipClient(issues=[issue])
        run_stranded_review(client, "co", limit=300, post=True, post_comment_on="tick-1", now=NOW)
        rollup_targets = [issue_id for issue_id, _ in client.posted if issue_id == "tick-1"]
        self.assertEqual(len(rollup_targets), 1)

    def test_no_findings_means_no_rollup(self) -> None:
        client = FakePaperclipClient(issues=[])
        run_stranded_review(client, "co", limit=300, post=True, post_comment_on="tick-1", now=NOW)
        self.assertEqual(client.posted, [])


if __name__ == "__main__":
    unittest.main()
