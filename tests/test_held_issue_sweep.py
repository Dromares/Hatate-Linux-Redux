"""DAN-496: detector for reconciliation-held issues (non-null `executionBlocker`).

Fixtures below reconstruct the *held* state of DAN-213, DAN-236 and DAN-314
from AGENTS.md's standing "Wedged issues" writeup plus the real
`executionBlocker` / `GET /api/heartbeat-runs/{id}` / agent shapes observed
live against the board on 2026-10-04 (`cause`, the recovery-action/run/agent
id patterns, the wake-event shape). All three issues are closed now, and a
closed issue's current `assigneeAgentId` reflects whatever happened *after*
resolution (e.g. routed to Minos for approval), not the assignment at the
moment the hold would have been checked -- so these are reconstructed
snapshots of the held state, not literal byte-for-byte API replays of
already-overwritten history. What matters for this detector, and what is
reconstructed faithfully, is the asymmetry the ticket calls out: DAN-213 was
reassigned away from its hold run's own agent while still held (the
unrecoverable shape), DAN-236 and DAN-314 were not (the admittable shape).
"""

from __future__ import annotations

import unittest
from typing import Any

from ops.held_issue_sweep import (
    PaperclipClient,
    earliest_held_at,
    file_or_update_trackers,
    format_finding,
    run_preflight,
    sweep,
)

VIRGIL = "d67c9967-9a1c-43c9-b3e4-bc079aa8f74e"
MINOS = "500355a5-9f7f-4881-8a9c-6ed3447f1878"
DANTE = "5ce92a46-f3a4-45f0-9937-d35cd1edd088"

CLAUDE_LOCAL_AGENT = {"adapterType": "claude_local"}


def _issue(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "issue-id",
        "identifier": "DAN-0",
        "title": "placeholder",
        "status": "in_review",
        "assigneeAgentId": VIRGIL,
        "executionBlocker": {
            "recoveryActionId": "rec-1",
            "runId": "run-1",
            "agentId": VIRGIL,
            "cause": "legacy_execution_requires_reconciliation",
            "nextAction": "Automatic recovery stopped.",
        },
        "executionState": None,
    }
    base.update(overrides)
    return base


def _run(**overrides: Any) -> dict[str, Any]:
    base = {
        "status": "cancelled",
        "finishedAt": "2026-10-03T16:05:35.874Z",
        "nativeIssueId": None,
        "contextSnapshot": {"issueId": "issue-id"},
    }
    base.update(overrides)
    return base


# DAN-213: reassigned to Minos (the approver) while still held, breaking the
# hold run's agentId (Virgil) <-> assigneeAgentId pairing permanently.
DAN_213_ISSUE = _issue(
    id="c3d756d9-81c1-44a2-9575-358c47371490",
    identifier="DAN-213",
    title="Neither of our two gates runs without a human",
    status="in_review",
    assigneeAgentId=MINOS,
    executionBlocker={
        "recoveryActionId": "97303b61-94f8-401d-a0ae-79e4fa531a90",
        "runId": "50426868-4111-4bc5-8e9c-387e5f75c9de",
        "agentId": VIRGIL,
        "cause": "legacy_execution_requires_reconciliation",
        "nextAction": "Automatic recovery stopped.",
    },
)
DAN_213_RUN = _run(
    nativeIssueId=None,
    contextSnapshot={"issueId": "c3d756d9-81c1-44a2-9575-358c47371490"},
)

# DAN-236: still assigned to the hold run's own agent (Virgil) -- the board's
# "try now" comment on 2026-10-04 actually admitted this one.
DAN_236_ISSUE = _issue(
    id="39d09e08-abaf-420e-8842-fe335b06de5f",
    identifier="DAN-236",
    title="scripts/merge_pr.sh is referenced by the merge gate but does not exist",
    status="in_review",
    assigneeAgentId=VIRGIL,
    executionBlocker={
        "recoveryActionId": "cc2ce24c-0dd6-4de4-a721-16bd70f1546f",
        "runId": "dd43b2aa-915b-4884-837f-889e2190a896",
        "agentId": VIRGIL,
        "cause": "legacy_execution_requires_reconciliation",
        "nextAction": "Automatic recovery stopped.",
    },
)
DAN_236_RUN = _run(
    nativeIssueId=None,
    contextSnapshot={"issueId": "39d09e08-abaf-420e-8842-fe335b06de5f"},
)

# DAN-314: still assigned to its hold run's own agent (Dante).
DAN_314_ISSUE = _issue(
    id="85eaeceb-0af9-4715-b8a2-cf5c5f42547b",
    identifier="DAN-314",
    title="Implement DAN-293 fix: QPushButton:checked border ink_28 -> ink_46",
    status="blocked",
    assigneeAgentId=DANTE,
    executionBlocker={
        "recoveryActionId": "696570b5-386e-4007-acb6-6667cba7f0ba",
        "runId": "8d2d5d57-fb89-471d-8ef7-5837f5f410bc",
        "agentId": DANTE,
        "cause": "legacy_execution_requires_reconciliation",
        "nextAction": "Automatic recovery stopped.",
    },
)
DAN_314_RUN = _run(
    nativeIssueId=None,
    contextSnapshot={"issueId": "85eaeceb-0af9-4715-b8a2-cf5c5f42547b"},
)


class RunPreflightTests(unittest.TestCase):
    def test_dan_213_owner_assignee_mismatch_bounces(self) -> None:
        pf = run_preflight(DAN_213_ISSUE, DAN_213_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertEqual(len(pf.reasons), 1, pf.reasons)
        self.assertIn("does not match the hold run's agentId", pf.reasons[0])

    def test_dan_236_preconditions_satisfied_admits(self) -> None:
        pf = run_preflight(DAN_236_ISSUE, DAN_236_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertTrue(pf.admits, pf.reasons)
        self.assertEqual(pf.reasons, [])

    def test_dan_314_preconditions_satisfied_admits(self) -> None:
        pf = run_preflight(DAN_314_ISSUE, DAN_314_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertTrue(pf.admits, pf.reasons)
        self.assertEqual(pf.reasons, [])

    def test_closed_issue_is_unrecoverable_even_with_matching_assignee(self) -> None:
        issue = _issue(status="done")
        pf = run_preflight(issue, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertIn("one-way door", pf.reasons[0])

    def test_non_terminal_hold_run_fails_preflight(self) -> None:
        issue = _issue()
        run = _run(status="running", finishedAt=None)
        pf = run_preflight(issue, run, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(any("not terminal" in r for r in pf.reasons))

    def test_mismatched_native_issue_id_fails_preflight(self) -> None:
        issue = _issue()
        run = _run(contextSnapshot={"issueId": "some-other-issue"})
        pf = run_preflight(issue, run, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(any("does not point at this issue" in r for r in pf.reasons))

    def test_unsupported_adapter_type_fails_preflight(self) -> None:
        issue = _issue()
        pf = run_preflight(issue, _run(), {"adapterType": "slack_bot"}, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(any("not a supported continuation adapter" in r for r in pf.reasons))

    def test_pending_interaction_fails_preflight(self) -> None:
        issue = _issue()
        pf = run_preflight(issue, _run(), CLAUDE_LOCAL_AGENT, interactions=[{"status": "pending"}])
        self.assertFalse(pf.admits)
        self.assertTrue(any("decision_pending" in r for r in pf.reasons))

    def test_pending_approval_fails_preflight(self) -> None:
        issue = _issue(executionState={"status": "pending"})
        pf = run_preflight(issue, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(any("decision_pending" in r for r in pf.reasons))


class EarliestHeldAtTests(unittest.TestCase):
    def test_picks_earliest_deferred_event_not_the_latest(self) -> None:
        wakes = {
            "events": [
                {"status": "deferred_issue_execution", "requestedAt": "2026-10-04T14:00:00.000Z"},
                {"status": "deferred_issue_execution", "requestedAt": "2026-10-03T06:00:00.000Z"},
                {"status": "skipped", "requestedAt": "2026-10-02T00:00:00.000Z"},
            ]
        }
        self.assertEqual(earliest_held_at(wakes), "2026-10-03T06:00:00.000Z")

    def test_returns_none_when_never_deferred(self) -> None:
        self.assertIsNone(earliest_held_at({"events": [{"status": "skipped", "requestedAt": "x"}]}))


class FakePaperclipClient(PaperclipClient):
    """In-memory double; never touches urllib (see test_done_issue_sweep.py)."""

    def __init__(
        self,
        issues: dict[str, dict[str, Any]],
        runs: dict[str, dict[str, Any]],
        agents: dict[str, dict[str, Any]],
        wakes: dict[str, dict[str, Any]],
    ) -> None:
        self._issues = issues
        self._runs = runs
        self._agents = agents
        self._wakes = wakes
        self._trackers: dict[str, dict[str, Any]] = {}
        self._tracker_comments: dict[str, list[dict[str, Any]]] = {}
        self._next_tracker_id = 0

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        return [{"id": i} for i in self._issues]

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        return self._issues[issue_id]

    def get_agent(self, agent_id: Any) -> Any:
        return self._agents.get(agent_id)

    def get_run(self, run_id: Any) -> Any:
        return self._runs.get(run_id)

    def get_wakes(self, issue_id: str) -> dict[str, Any]:
        return self._wakes.get(issue_id, {"events": []})

    def get_interactions(self, issue_id: str) -> list[dict[str, Any]]:
        return []

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._tracker_comments.get(issue_id, []))

    def search_issues(self, company_id: str, query: str) -> list[dict[str, Any]]:
        return [t for t in self._trackers.values() if t["title"] == query]

    def create_issue(self, company_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._next_tracker_id += 1
        tracker_id = f"tracker-{self._next_tracker_id}"
        tracker = {"id": tracker_id, "createdAt": f"{self._next_tracker_id:04d}", **payload}
        self._trackers[tracker_id] = tracker
        return tracker

    def comment(self, issue_id: str, body: str) -> Any:
        self._tracker_comments.setdefault(issue_id, []).append({"body": body})
        return {"id": "comment", "body": body}


def _fixture_client() -> FakePaperclipClient:
    return FakePaperclipClient(
        issues={
            DAN_213_ISSUE["id"]: DAN_213_ISSUE,
            DAN_236_ISSUE["id"]: DAN_236_ISSUE,
        },
        runs={
            "50426868-4111-4bc5-8e9c-387e5f75c9de": DAN_213_RUN,
            "dd43b2aa-915b-4884-837f-889e2190a896": DAN_236_RUN,
        },
        agents={MINOS: CLAUDE_LOCAL_AGENT, VIRGIL: CLAUDE_LOCAL_AGENT},
        wakes={},
    )


class SweepTests(unittest.TestCase):
    def test_sweep_only_returns_issues_with_a_live_blocker(self) -> None:
        client = _fixture_client()
        client._issues["no-hold"] = {
            "id": "no-hold",
            "identifier": "DAN-999",
            "status": "todo",
            "assigneeAgentId": VIRGIL,
            "executionBlocker": None,
            "executionState": None,
        }
        held = sweep(client, "co")
        identifiers = {h.identifier for h in held}
        self.assertEqual(identifiers, {"DAN-213", "DAN-236"})

    def test_sweep_reports_the_right_admission_verdict_per_issue(self) -> None:
        client = _fixture_client()
        held = {h.identifier: h for h in sweep(client, "co")}
        self.assertFalse(held["DAN-213"].preflight.admits)
        self.assertTrue(held["DAN-236"].preflight.admits)


class ThrottleTests(unittest.TestCase):
    """Two consecutive fires with an unchanged finding produce one report, not two."""

    def test_first_fire_files_second_fire_is_silent(self) -> None:
        client = _fixture_client()
        held = sweep(client, "co")

        first = file_or_update_trackers(client, "co", "proj", "cloud-id", held)
        self.assertEqual({a["action"] for a in first}, {"filed"})
        self.assertEqual(len(client._trackers), 2)

        second = file_or_update_trackers(client, "co", "proj", "cloud-id", held)
        self.assertEqual({a["action"] for a in second}, {"unchanged"})
        self.assertEqual(len(client._trackers), 2, "must not create a second tracker")
        for comments in client._tracker_comments.values():
            self.assertEqual(comments, [], "an unchanged finding must not post a new comment")

    def test_changed_finding_posts_exactly_one_update_comment(self) -> None:
        client = _fixture_client()
        held = sweep(client, "co")
        file_or_update_trackers(client, "co", "proj", "cloud-id", held)

        # DAN-213 gets reassigned back onto its hold run's own agent: the
        # finding changes from "bounces" to "admits".
        client._issues[DAN_213_ISSUE["id"]]["assigneeAgentId"] = VIRGIL
        held_again = sweep(client, "co")
        actions = file_or_update_trackers(client, "co", "proj", "cloud-id", held_again)

        by_id = {a["identifier"]: a for a in actions}
        self.assertEqual(by_id["DAN-213"]["action"], "updated")
        self.assertEqual(by_id["DAN-236"]["action"], "unchanged")

        dan_213_tracker_id = by_id["DAN-213"]["issueId"]
        self.assertEqual(len(client._tracker_comments[dan_213_tracker_id]), 1)


class FormatFindingTests(unittest.TestCase):
    def test_admitting_finding_requires_a_human_board_user(self) -> None:
        held = sweep(_fixture_client(), "co")
        dan_236 = next(h for h in held if h.identifier == "DAN-236")
        text = format_finding(dan_236)
        self.assertIn("a human board user must post a plain comment on DAN-236", text)
        self.assertIn("body content is irrelevant", text)
        self.assertIn("No agent comment, status write, or reassignment can substitute", text)
        self.assertIn('actorType === "user"', text)

    def test_bouncing_finding_warns_against_reassignment(self) -> None:
        held = sweep(_fixture_client(), "co")
        dan_213 = next(h for h in held if h.identifier == "DAN-213")
        text = format_finding(dan_213)
        self.assertIn("will bounce", text)
        self.assertIn("Do not reassign it", text)

    def test_every_finding_warns_against_closing_and_reassigning(self) -> None:
        held = {h.identifier: h for h in sweep(_fixture_client(), "co")}
        for identifier, h in held.items():
            text = format_finding(h)
            self.assertIn(f"Do not close {identifier}", text)
            self.assertIn("one-way door", text)
            self.assertIn("do not reassign it", text)


if __name__ == "__main__":
    unittest.main()
