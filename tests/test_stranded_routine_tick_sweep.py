"""DAN-781: repeatable disposal of stranded routine ticks.

Fixtures below reconstruct real shapes measured live against the board on
2026-10-07: 43 routine-filed issues across five titles whose only comments
were the harness's own terminal-failure notices (`ACP agent reported a
terminal access failure.` / `... terminal limit failure.`), produced by
that day's 06:13-09:55Z credential outage. Classification is content-based
(every comment must be boilerplate), not count- or title-based, so a tick
that happened to produce real findings is never swept up by this script.
"""

from __future__ import annotations

import unittest
from typing import Any

from ops.stranded_routine_tick_sweep import (
    PaperclipClient,
    classify,
    dispose,
    is_boilerplate_comment,
    sweep,
)

DANTE = "5ce92a46-f3a4-45f0-9937-d35cd1edd088"
MINOS = "500355a5-9f7f-4881-8a9c-6ed3447f1878"

SEAT_HEALTH = "Seat-health watchdog (narrow arm)"
OAUTH_WATCHDOG = "Claude OAuth credential-expiry watchdog"
GATE_SWEEP = "Default-approver gate sweep"

ACCESS_FAILURE = "ACP agent reported a terminal access failure."
LIMIT_FAILURE = "ACP agent reported a terminal limit failure."


def _issue(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "issue-id",
        "identifier": "DAN-0",
        "title": SEAT_HEALTH,
        "status": "in_progress",
        "createdAt": "2026-10-07T03:00:20.854Z",
        "assigneeAgentId": DANTE,
    }
    base.update(overrides)
    return base


def _comment(body: str) -> dict[str, Any]:
    return {"body": body}


class IsBoilerplateCommentTests(unittest.TestCase):
    def test_access_failure_matches(self) -> None:
        self.assertTrue(is_boilerplate_comment(ACCESS_FAILURE))

    def test_limit_failure_matches(self) -> None:
        self.assertTrue(is_boilerplate_comment(LIMIT_FAILURE))

    def test_real_finding_does_not_match(self) -> None:
        self.assertFalse(is_boilerplate_comment("Found 3 seats over capacity; filed DAN-900."))

    def test_leading_trailing_whitespace_still_matches(self) -> None:
        self.assertTrue(is_boilerplate_comment(f"  {ACCESS_FAILURE}  "))


class ClassifyTests(unittest.TestCase):
    def test_no_comments_is_stranded(self) -> None:
        verdict, reason = classify(_issue(), [])
        self.assertEqual(verdict, "stranded")
        self.assertIn("never executed", reason)

    def test_all_boilerplate_comments_is_stranded(self) -> None:
        comments = [_comment(ACCESS_FAILURE), _comment(ACCESS_FAILURE), _comment(LIMIT_FAILURE)]
        verdict, reason = classify(_issue(), comments)
        self.assertEqual(verdict, "stranded")
        self.assertIn("boilerplate", reason)

    def test_one_real_comment_among_boilerplate_is_has_output(self) -> None:
        comments = [
            _comment(ACCESS_FAILURE),
            _comment("Seat watchdog found Minos queue depth 9; filed DAN-901."),
        ]
        verdict, reason = classify(_issue(), comments)
        self.assertEqual(verdict, "has_output")
        self.assertIn("possible real output", reason)


class SweepTests(unittest.TestCase):
    def test_filters_by_title_and_classifies_each(self) -> None:
        issues = [
            _issue(id="a", identifier="DAN-699", title=SEAT_HEALTH),
            _issue(id="b", identifier="DAN-746", title=GATE_SWEEP),
            _issue(id="c", identifier="DAN-900", title="Unrelated ticket"),
        ]
        comments = {"a": [], "b": [_comment(ACCESS_FAILURE)], "c": []}
        client = FakeClient(issues, comments)

        result = sweep(client, "company-1", [SEAT_HEALTH, GATE_SWEEP])

        self.assertEqual({c.identifier for c in result}, {"DAN-699", "DAN-746"})
        self.assertTrue(all(c.verdict == "stranded" for c in result))

    def test_real_output_ticket_is_excluded_from_stranded(self) -> None:
        issues = [_issue(id="a", identifier="DAN-699", title=SEAT_HEALTH)]
        comments = {"a": [_comment("Found a real problem; filed DAN-902.")]}
        client = FakeClient(issues, comments)

        result = sweep(client, "company-1", [SEAT_HEALTH])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].verdict, "has_output")

    def test_cutoff_excludes_tickets_created_after_it(self) -> None:
        issues = [
            _issue(id="a", identifier="DAN-699", title=SEAT_HEALTH, createdAt="2026-10-07T02:30:20.807Z"),
            _issue(id="b", identifier="DAN-778", title=SEAT_HEALTH, createdAt="2026-10-07T10:30:22.314Z"),
        ]
        comments = {"a": [], "b": []}
        client = FakeClient(issues, comments)

        result = sweep(client, "company-1", [SEAT_HEALTH], cutoff="2026-10-07T09:55:00.000Z")

        self.assertEqual([c.identifier for c in result], ["DAN-699"])


class DisposeTests(unittest.TestCase):
    def test_cancels_only_stranded_candidates(self) -> None:
        issues = [
            _issue(id="a", identifier="DAN-699", title=SEAT_HEALTH),
            _issue(id="b", identifier="DAN-746", title=GATE_SWEEP),
        ]
        comments = {"a": [], "b": [_comment("Found a real problem.")]}
        client = FakeClient(issues, comments)
        candidates = sweep(client, "company-1", [SEAT_HEALTH, GATE_SWEEP])

        actions = dispose(client, candidates, incident="2026-10-07 ACP outage")

        self.assertEqual(
            {(a["identifier"], a["action"]) for a in actions},
            {("DAN-699", "cancelled"), ("DAN-746", "skipped_has_output")},
        )
        self.assertEqual(client.cancelled, ["a"])
        self.assertIn("DAN-781", client.cancel_comments["a"])
        self.assertIn("2026-10-07 ACP outage", client.cancel_comments["a"])

    def test_limit_defers_remaining_stranded_candidates(self) -> None:
        issues = [
            _issue(id="a", identifier="DAN-699", title=SEAT_HEALTH),
            _issue(id="b", identifier="DAN-704", title=SEAT_HEALTH),
            _issue(id="c", identifier="DAN-711", title=SEAT_HEALTH),
        ]
        comments = {"a": [], "b": [], "c": []}
        client = FakeClient(issues, comments)
        candidates = sweep(client, "company-1", [SEAT_HEALTH])

        actions = dispose(client, candidates, incident="outage", limit=2)

        self.assertEqual(len(client.cancelled), 2)
        deferred = [a for a in actions if a["action"] == "deferred_limit"]
        self.assertEqual(len(deferred), 1)

    def test_has_output_never_cancelled_regardless_of_limit(self) -> None:
        issues = [_issue(id="a", identifier="DAN-746", title=GATE_SWEEP)]
        comments = {"a": [_comment("Found a real problem.")]}
        client = FakeClient(issues, comments)
        candidates = sweep(client, "company-1", [GATE_SWEEP])

        dispose(client, candidates, incident="outage", limit=50)

        self.assertEqual(client.cancelled, [])


class FakeClient(PaperclipClient):
    """In-memory double; never touches urllib (see test_done_issue_sweep.py)."""

    def __init__(self, issues: list[dict[str, Any]], comments: dict[str, list[dict[str, Any]]]) -> None:
        self._issues = issues
        self._comments = comments
        self.cancelled: list[str] = []
        self.cancel_comments: dict[str, str] = {}

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        return list(self._issues)

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return self._comments.get(issue_id, [])

    def cancel(self, issue_id: str, comment: str) -> Any:
        self.cancelled.append(issue_id)
        self.cancel_comments[issue_id] = comment
        return {"id": issue_id, "status": "cancelled"}


if __name__ == "__main__":
    unittest.main()
