"""DAN-496/DAN-801: detector for reconciliation-held issues (non-null `executionBlocker`).

Fixtures below reconstruct the *held* state of DAN-213, DAN-236, DAN-314,
DAN-660 and DAN-466 from AGENTS.md's standing "Wedged issues" writeup plus
the real `executionBlocker` / `GET /api/heartbeat-runs/{id}` / agent /
`executionState` shapes observed live against the board (`cause`, the
recovery-action/run/agent id patterns, the wake-event shape, and for DAN-660
the live `executionState.currentParticipant`/`returnAssignee` pair read
2026-10-07). Several of these issues are closed now, and a closed issue's
current `assigneeAgentId` reflects whatever happened *after* resolution
(e.g. routed to Minos for approval), not the assignment at the moment the
hold would have been checked -- so these are reconstructed snapshots of the
held state, not literal byte-for-byte API replays of already-overwritten
history. What matters for this detector, and what is reconstructed
faithfully, is the asymmetry each fixture calls out:

  - DAN-213: reassigned away from its hold run's own agent while still
    held -- the unrecoverable, re-route shape.
  - DAN-236, DAN-314: not reassigned -- the admittable, board-comment shape.
  - DAN-660: still assigned to its hold run's own agent, but *only because*
    a pending approval stage seated it there -- the structurally-impossible,
    cross-issue-write shape DAN-801 was filed to fix.
  - DAN-466: closed (`done`) with `executionBlocker` still populated -- the
    sealed, terminal-subject shape.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from ops.held_issue_sweep import (
    CLOUD_AGENT_ID,
    HeldIssue,
    PaperclipClient,
    choose_remedy,
    earliest_held_at,
    file_or_update_trackers,
    format_finding,
    hold_age_human,
    run_preflight,
    sweep,
)

VIRGIL = "d67c9967-9a1c-43c9-b3e4-bc079aa8f74e"
MINOS = "500355a5-9f7f-4881-8a9c-6ed3447f1878"
DANTE = "5ce92a46-f3a4-45f0-9937-d35cd1edd088"

CLAUDE_LOCAL_AGENT = {"adapterType": "claude_local"}

# The instant the accept-hold tests close a tracker (matches `completedAt` there).
CLOSED_AT = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _issue(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "issue-id",
        "identifier": "DAN-0",
        "title": "placeholder",
        "status": "in_review",
        "assigneeAgentId": VIRGIL,
        "parentId": None,
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

# DAN-660 (live 2026-10-07): assignee == hold agent (Minos) only because a
# pending approval stage seated Minos there; resolving that approval hands
# the issue back to returnAssignee (Virgil), which breaks the pairing.
# Structurally no board-comment path exists.
DAN_660_ISSUE = _issue(
    id="f6aa3476-350e-46e5-b3cc-39142aef75fe",
    identifier="DAN-660",
    title="DAN-660 run-banner",
    status="blocked",
    assigneeAgentId=MINOS,
    parentId="e3f2c428-c80b-4446-9633-84388b6c410b",
    executionBlocker={
        "recoveryActionId": "67ee49c5-8da2-4ba4-ae38-05efcad6b755",
        "runId": "2d706130-d21d-4a90-9462-715f3b3063c3",
        "agentId": MINOS,
        "cause": "legacy_execution_requires_reconciliation",
        "nextAction": "Automatic recovery stopped.",
    },
    executionState={
        "status": "pending",
        "currentStageType": "approval",
        "currentParticipant": {"type": "agent", "agentId": MINOS},
        "returnAssignee": {"type": "agent", "agentId": VIRGIL},
        "lastDecisionOutcome": None,
    },
)
DAN_660_RUN = _run(
    runId="2d706130-d21d-4a90-9462-715f3b3063c3",
    nativeIssueId=None,
    contextSnapshot={"issueId": "f6aa3476-350e-46e5-b3cc-39142aef75fe"},
)

# DAN-466 (live 2026-10-07): closed `done`, `executionBlocker` still
# populated. The sealed, terminal-subject shape -- the hold is real but
# moot, since nothing ever wakes a closed issue.
DAN_466_ISSUE = _issue(
    id="6f366618-2fa3-4f17-abda-61968a097f13",
    identifier="DAN-466",
    title="Held issue regression fixture",
    status="done",
    assigneeAgentId="f7d1f188-bce3-4145-b4c6-cf404a026435",
    executionBlocker={
        "recoveryActionId": "69c2fafb-27f1-4cf7-a9e8-7dd8666acb08",
        "runId": "3da4d80d-9d73-4fc6-ba24-cdddb3fb9f03",
        "agentId": "f7d1f188-bce3-4145-b4c6-cf404a026435",
        "cause": "legacy_execution_requires_reconciliation",
        "nextAction": "Automatic recovery stopped.",
    },
    executionState=None,
)


class RunPreflightTests(unittest.TestCase):
    def test_dan_213_owner_assignee_mismatch_bounces(self) -> None:
        pf = run_preflight(DAN_213_ISSUE, DAN_213_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertEqual(len(pf.reasons), 1, pf.reasons)
        self.assertIn("does not match the hold run's agentId", pf.reasons[0])
        self.assertFalse(pf.assignee_matches)
        self.assertFalse(pf.conflict)

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
        self.assertTrue(pf.decision_pending)

    def test_pending_approval_fails_preflight(self) -> None:
        issue = _issue(executionState={"status": "pending"})
        pf = run_preflight(issue, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(any("decision_pending" in r for r in pf.reasons))
        self.assertTrue(pf.decision_pending)
        self.assertFalse(pf.conflict, "no returnAssignee mismatch -- not the structural trap")

    def test_dan_660_approval_seat_trap_is_a_joint_conflict_not_two_separate_passes(self) -> None:
        pf = run_preflight(DAN_660_ISSUE, DAN_660_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        self.assertFalse(pf.admits)
        self.assertTrue(pf.assignee_matches, "assignee does match the hold agent right now")
        self.assertTrue(pf.decision_pending)
        self.assertTrue(pf.conflict, pf.reasons)
        self.assertTrue(any("jointly unsatisfiable" in r for r in pf.reasons))
        self.assertEqual(pf.seated_participant_agent_id, MINOS)


class ChooseRemedyTests(unittest.TestCase):
    def test_dan_236_admits_routes_to_board_comment(self) -> None:
        pf = run_preflight(DAN_236_ISSUE, DAN_236_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_236_ISSUE, pf, hold_agent_id=VIRGIL)
        self.assertEqual(remedy.kind, "board_comment")
        self.assertIsNone(remedy.owner_agent_id)

    def test_dan_213_broken_pairing_routes_to_re_route(self) -> None:
        pf = run_preflight(DAN_213_ISSUE, DAN_213_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_213_ISSUE, pf, hold_agent_id=VIRGIL)
        self.assertEqual(remedy.kind, "re_route")
        self.assertEqual(remedy.owner_agent_id, MINOS, "name the issue's current assignee to carry over")

    def test_dan_660_structural_conflict_routes_to_cross_issue_write_owned_by_approver(self) -> None:
        pf = run_preflight(DAN_660_ISSUE, DAN_660_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_660_ISSUE, pf, hold_agent_id=MINOS)
        self.assertEqual(remedy.kind, "cross_issue_write")
        self.assertEqual(remedy.owner_agent_id, MINOS, "the seated approver, not Cloud")
        # DAN-801 follow-up: a joint conflict must route with high confidence,
        # not be escalated as "no remedy available" -- it's the cheapest
        # case precisely because the thing that breaks (A) is what makes (B)
        # (and thus this remedy) true.
        self.assertIn("cheapest", remedy.detail)
        self.assertIn("do not escalate", remedy.detail)

    def test_plain_pending_approval_without_conflict_routes_to_cross_issue_write(self) -> None:
        issue = _issue(
            executionState={
                "status": "pending",
                "currentParticipant": {"agentId": VIRGIL},
                "returnAssignee": {"agentId": VIRGIL},
            }
        )
        pf = run_preflight(issue, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(issue, pf, hold_agent_id=VIRGIL)
        self.assertEqual(remedy.kind, "cross_issue_write")
        self.assertEqual(remedy.owner_agent_id, VIRGIL)

    def test_dan_466_terminal_subject_routes_to_sealed_not_board_comment(self) -> None:
        pf = run_preflight(DAN_466_ISSUE, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_466_ISSUE, pf, hold_agent_id="f7d1f188-bce3-4145-b4c6-cf404a026435")
        self.assertEqual(remedy.kind, "sealed")
        self.assertIsNone(remedy.owner_agent_id)

    def test_other_bounce_reasons_fall_back_to_re_route(self) -> None:
        issue = _issue()
        run = _run(status="running", finishedAt=None)
        pf = run_preflight(issue, run, CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(issue, pf, hold_agent_id=VIRGIL)
        self.assertEqual(remedy.kind, "re_route")


class HoldAgeHumanTests(unittest.TestCase):
    def test_minutes(self) -> None:
        now = datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(hold_age_human("2026-10-07T12:00:00.000Z", now), "30m")

    def test_hours_and_minutes(self) -> None:
        now = datetime(2026, 10, 7, 15, 5, tzinfo=timezone.utc)
        self.assertEqual(hold_age_human("2026-10-07T12:00:00.000Z", now), "3h05m")

    def test_days_and_hours_is_the_escalating_shape_for_dan_466_style_holds(self) -> None:
        now = datetime(2026, 10, 10, 14, 58, 48, tzinfo=timezone.utc)
        self.assertEqual(hold_age_human("2026-10-07T14:58:47.800Z", now), "3d00h")

    def test_unknown_when_never_deferred(self) -> None:
        self.assertIn("unknown", hold_age_human(None, datetime.now(timezone.utc)))


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
        tracker = {
            "id": tracker_id,
            "createdAt": f"{self._next_tracker_id:04d}",
            "status": "todo",
            **payload,
        }
        self._trackers[tracker_id] = tracker
        return tracker

    def update_issue(self, issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        tracker = self._trackers[issue_id]
        comment = payload.pop("comment", None)
        tracker.update(payload)
        if comment is not None:
            self._tracker_comments.setdefault(issue_id, []).append({"body": comment})
        return tracker


def _fixture_client() -> FakePaperclipClient:
    # Copy the module-level fixtures -- tests mutate `assigneeAgentId` on the
    # client's own issue dict to simulate a reassignment between two fires,
    # and a shallow reference here would leak that mutation into every
    # other test that imports the same DAN_213_ISSUE/DAN_236_ISSUE constant.
    return FakePaperclipClient(
        issues={
            DAN_213_ISSUE["id"]: dict(DAN_213_ISSUE),
            DAN_236_ISSUE["id"]: dict(DAN_236_ISSUE),
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
            "parentId": None,
            "executionBlocker": None,
            "executionState": None,
        }
        held = sweep(client, "co")
        identifiers = {h.identifier for h in held}
        self.assertEqual(identifiers, {"DAN-213", "DAN-236"})

    def test_sweep_reports_the_right_admission_verdict_and_remedy_per_issue(self) -> None:
        client = _fixture_client()
        held = {h.identifier: h for h in sweep(client, "co")}
        self.assertFalse(held["DAN-213"].preflight.admits)
        self.assertEqual(held["DAN-213"].remedy.kind, "re_route")
        self.assertTrue(held["DAN-236"].preflight.admits)
        self.assertEqual(held["DAN-236"].remedy.kind, "board_comment")


class ThrottleTests(unittest.TestCase):
    """Two consecutive fires with an unchanged finding produce one report, not two."""

    def test_first_fire_files_second_fire_is_silent(self) -> None:
        client = _fixture_client()
        held = sweep(client, "co")

        first = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held)
        self.assertEqual({a["action"] for a in first}, {"filed"})
        self.assertEqual(len(client._trackers), 2)

        second = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held)
        self.assertEqual({a["action"] for a in second}, {"unchanged"})
        self.assertEqual(len(client._trackers), 2, "must not create a second tracker")
        for comments in client._tracker_comments.values():
            self.assertEqual(comments, [], "an unchanged finding must not post a new comment")

    def test_changed_finding_posts_exactly_one_update_comment(self) -> None:
        client = _fixture_client()
        held = sweep(client, "co")
        file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held)

        # DAN-213 gets reassigned back onto its hold run's own agent: the
        # finding changes from "bounces/re_route" to "admits/board_comment".
        client._issues[DAN_213_ISSUE["id"]]["assigneeAgentId"] = VIRGIL
        held_again = sweep(client, "co")
        actions = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held_again)

        by_id = {a["identifier"]: a for a in actions}
        self.assertEqual(by_id["DAN-213"]["action"], "updated")
        self.assertEqual(by_id["DAN-236"]["action"], "unchanged")

        dan_213_tracker_id = by_id["DAN-213"]["issueId"]
        self.assertEqual(len(client._tracker_comments[dan_213_tracker_id]), 1)

    def test_tracker_closed_while_subject_still_held_is_reopened_not_left_closed(self) -> None:
        """DAN-498/DAN-466 regression: a tracker must not stay `done` while
        its subject's hold is still live and non-terminal."""
        client = _fixture_client()
        held = sweep(client, "co")
        file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held)

        dan_236_tracker_id = next(t["id"] for t in client._trackers.values() if "DAN-236" in t["title"])
        client._trackers[dan_236_tracker_id]["status"] = "done"

        # Nothing about DAN-236's held state changed -- same fingerprint,
        # still non-terminal, still blocked -- yet the tracker was closed.
        held_again = sweep(client, "co")
        actions = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held_again)
        by_id = {a["identifier"]: a for a in actions}

        self.assertEqual(by_id["DAN-236"]["action"], "reopened")
        self.assertEqual(client._trackers[dan_236_tracker_id]["status"], "todo")
        self.assertEqual(len(client._tracker_comments[dan_236_tracker_id]), 1)
        self.assertIn("Held for", client._tracker_comments[dan_236_tracker_id][0]["body"])

    def _closed_tracker_for_236(self, client: FakePaperclipClient, **extra: Any) -> str:
        held = sweep(client, "co")
        file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held, now=CLOSED_AT)
        tracker_id = next(t["id"] for t in client._trackers.values() if "DAN-236" in t["title"])
        client._trackers[tracker_id].update({"status": "done", "completedAt": "2026-10-10T00:00:00.000Z", **extra})
        return tracker_id

    def _sweep_236(self, client: FakePaperclipClient, now: datetime) -> dict[str, Any]:
        actions = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, sweep(client, "co"), now=now)
        return {a["identifier"]: a for a in actions}["DAN-236"]

    def test_accept_hold_marker_keeps_a_closed_tracker_closed_and_reports_it(self) -> None:
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._tracker_comments.setdefault(tracker_id, []).append(
            {"body": "Closing.\naccept-hold: Lucia is board-paused until 2026-10-20"}
        )
        action = self._sweep_236(client, CLOSED_AT + timedelta(days=3))
        self.assertEqual(action["action"], "accepted_hold")
        self.assertEqual(action["reason"], "Lucia is board-paused until 2026-10-20")
        self.assertEqual(client._trackers[tracker_id]["status"], "done")
        self.assertEqual(len(client._tracker_comments[tracker_id]), 1, "must not post a comment")

    def test_accept_hold_in_description_counts(self) -> None:
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._trackers[tracker_id]["description"] += "\naccept-hold: board-paused"
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=1))["action"], "accepted_hold")

    def test_accept_hold_expires_after_14_days(self) -> None:
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._tracker_comments.setdefault(tracker_id, []).append({"body": "accept-hold: paused"})
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=13))["action"], "accepted_hold")
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=14))["action"], "reopened")
        self.assertEqual(client._trackers[tracker_id]["status"], "todo")

    def _assert_new_hold_reopens(self, **blocker_changes: str) -> None:
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._tracker_comments.setdefault(tracker_id, []).append({"body": "accept-hold: paused"})
        issue = client._issues[DAN_236_ISSUE["id"]]
        issue["executionBlocker"] = dict(issue["executionBlocker"], **blocker_changes)
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=1))["action"], "reopened")

    def test_accept_hold_expires_when_the_hold_run_changes(self) -> None:
        self._assert_new_hold_reopens(runId="50426868-4111-4bc5-8e9c-387e5f75c9de")

    def test_accept_hold_expires_when_the_cause_changes(self) -> None:
        self._assert_new_hold_reopens(cause="a_different_cause")

    def test_accept_hold_does_not_carry_over_a_reopen(self) -> None:
        """A marker older than the sweep's latest report covers a past closure only."""
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._tracker_comments.setdefault(tracker_id, []).append({"body": "accept-hold: paused"})
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=14))["action"], "reopened")
        client._trackers[tracker_id].update({"status": "done", "completedAt": "2026-10-25T00:00:00.000Z"})
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=16))["action"], "reopened")

    def test_accept_hold_is_not_inferred_from_free_text(self) -> None:
        client = _fixture_client()
        tracker_id = self._closed_tracker_for_236(client)
        client._tracker_comments.setdefault(tracker_id, []).append(
            {"body": "Leaving this held on purpose, we accept the hold for now."}
        )
        self.assertEqual(self._sweep_236(client, CLOSED_AT + timedelta(days=1))["action"], "reopened")

    def test_findings_route_to_remedy_owner_not_unconditionally_to_cloud(self) -> None:
        client = _fixture_client()
        held = sweep(client, "co")
        actions = file_or_update_trackers(client, "co", "proj", CLOUD_AGENT_ID, held)
        by_id = {a["identifier"]: a for a in actions}

        # DAN-213 bounces (re_route) -- owned by its own current assignee
        # (Minos), not Cloud.
        self.assertEqual(by_id["DAN-213"]["assignedTo"], MINOS)
        dan_213_tracker = client._trackers[by_id["DAN-213"]["issueId"]]
        self.assertEqual(dan_213_tracker["assigneeAgentId"], MINOS)

        # DAN-236 admits (board_comment) -- only the board can act, so this
        # one does fall back to Cloud.
        self.assertEqual(by_id["DAN-236"]["assignedTo"], CLOUD_AGENT_ID)
        dan_236_tracker = client._trackers[by_id["DAN-236"]["issueId"]]
        self.assertEqual(dan_236_tracker["assigneeAgentId"], CLOUD_AGENT_ID)


class FormatFindingTests(unittest.TestCase):
    def test_admitting_finding_requires_a_human_board_user(self) -> None:
        held = sweep(_fixture_client(), "co")
        dan_236 = next(h for h in held if h.identifier == "DAN-236")
        text = format_finding(dan_236)
        self.assertIn("human board user must post a plain comment on DAN-236", text)
        self.assertIn("body content is irrelevant", text)
        self.assertIn('actorType === "user"', text)

    def test_re_route_finding_names_assignee_and_parent_to_carry_over(self) -> None:
        held = sweep(_fixture_client(), "co")
        dan_213 = next(h for h in held if h.identifier == "DAN-213")
        text = format_finding(dan_213)
        self.assertIn("Re-route the objective to a fresh issue", text)
        self.assertIn(MINOS, text)

    def test_cross_issue_write_finding_names_the_seated_participant(self) -> None:
        pf = run_preflight(DAN_660_ISSUE, DAN_660_RUN, CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_660_ISSUE, pf, hold_agent_id=MINOS)
        self.assertEqual(remedy.kind, "cross_issue_write")  # sanity on the fixture itself

        direct = HeldIssue(
            issue_id=DAN_660_ISSUE["id"],
            identifier="DAN-660",
            title=DAN_660_ISSUE["title"],
            status=DAN_660_ISSUE["status"],
            assignee_agent_id=DAN_660_ISSUE["assigneeAgentId"],
            parent_id=DAN_660_ISSUE["parentId"],
            cause=DAN_660_ISSUE["executionBlocker"]["cause"],
            hold_run_id=DAN_660_ISSUE["executionBlocker"]["runId"],
            hold_agent_id=MINOS,
            held_since=None,
            preflight=pf,
            remedy=remedy,
        )
        text = format_finding(direct)
        self.assertIn("Only a decision/status write remains on DAN-660", text)
        self.assertIn(MINOS, text)
        self.assertIn("jointly unsatisfiable", text)
        # DAN-801 follow-up (2026-10-07): hardened after DAN-660/DAN-800 proved
        # the remedy on a gated issue -- never binds a run to the held issue,
        # and the "only safe when already seated" precondition is explicit so
        # a reader doesn't misapply the pattern to an unseated gate (DAN-466).
        self.assertIn("Never check out or bind a run", text)
        self.assertIn("DAN-660", text)
        self.assertIn("DAN-800", text)
        self.assertIn("unseated", text)
        self.assertIn("DAN-466", text)

    def test_sealed_finding_states_no_remedy_is_needed_and_skips_the_close_warning(self) -> None:
        pf = run_preflight(DAN_466_ISSUE, _run(), CLAUDE_LOCAL_AGENT, interactions=[])
        remedy = choose_remedy(DAN_466_ISSUE, pf, hold_agent_id="f7d1f188-bce3-4145-b4c6-cf404a026435")

        h = HeldIssue(
            issue_id=DAN_466_ISSUE["id"],
            identifier="DAN-466",
            title=DAN_466_ISSUE["title"],
            status="done",
            assignee_agent_id=DAN_466_ISSUE["assigneeAgentId"],
            parent_id=None,
            cause=DAN_466_ISSUE["executionBlocker"]["cause"],
            hold_run_id=DAN_466_ISSUE["executionBlocker"]["runId"],
            hold_agent_id=DAN_466_ISSUE["executionBlocker"]["agentId"],
            held_since="2026-10-04T14:58:47.800Z",
            preflight=pf,
            remedy=remedy,
        )
        text = format_finding(h, now=datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc))
        self.assertIn("already terminal", text)
        self.assertIn("sealed, not live", text)
        self.assertNotIn("Do not close DAN-466", text)

    def test_every_live_finding_warns_against_closing_and_reassigning(self) -> None:
        held = {h.identifier: h for h in sweep(_fixture_client(), "co")}
        for identifier, h in held.items():
            text = format_finding(h)
            self.assertIn(f"Do not close {identifier}", text)
            self.assertIn("one-way door", text)
            self.assertIn("do not reassign it", text)


if __name__ == "__main__":
    unittest.main()
