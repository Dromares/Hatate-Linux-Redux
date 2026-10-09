"""DAN-289/DAN-385/DAN-720: routine-agnostic tick finalizer.

Uses a fake Paperclip client (no network) to verify rolling-log reuse
(found vs. created once), the cancelled-vs-done split between the clean
and exception paths, the `--note` override text, and the exact
`<ISO-8601>: ` comment format DAN-720's dead-man switch depends on.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from ops.tick_finalize import (
    PaperclipClient,
    clean_note,
    find_or_create_rolling_log,
    find_rolling_log,
    finalize_clean,
    finalize_exception,
    is_rolling_log_line,
    most_recent_rolling_log_line,
)

NOW = datetime(2026, 10, 5, 0, 58, 20, tzinfo=timezone.utc)


class FakePaperclipClient(PaperclipClient):
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self._issues = {i["id"]: dict(i) for i in issues}
        self._comments: dict[str, list[dict[str, Any]]] = {}
        self.created: list[dict[str, Any]] = []
        self.status_calls: list[tuple[str, str, str]] = []

    def search_issues_by_text(self, company_id: str, text: str, limit: int = 50) -> list[dict[str, Any]]:
        return [i for i in self._issues.values() if text in (i.get("title") or "")]

    def create_issue(
        self, company_id: str, project_id: str, assignee_agent_id: str, title: str, description: str
    ) -> dict[str, Any]:
        issue = {
            "id": f"new-{len(self._issues)}",
            "identifier": f"DAN-NEW-{len(self._issues)}",
            "title": title,
            "assigneeAgentId": assignee_agent_id,
            "status": "todo",
        }
        self._issues[issue["id"]] = issue
        self.created.append(issue)
        return issue

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._comments.get(issue_id, []))

    def comment(self, issue_id: str, body: str) -> Any:
        self._comments.setdefault(issue_id, []).append({"body": body, "createdAt": NOW.isoformat()})
        return {"id": "c1", "body": body}

    def set_status(self, issue_id: str, status: str, comment: str) -> Any:
        self.status_calls.append((issue_id, status, comment))
        self._issues[issue_id]["status"] = status
        return self._issues[issue_id]


class CleanNoteTests(unittest.TestCase):
    def test_default_note_when_omitted(self) -> None:
        line = clean_note(None, now=NOW)
        self.assertTrue(line.startswith("2026-10-05T00:58:20"))
        self.assertIn("clean -- no findings this tick", line)

    def test_custom_note_is_appended_after_timestamp(self) -> None:
        line = clean_note("restated-only -- still-open: DAN-9", now=NOW)
        self.assertTrue(line.startswith("2026-10-05T00:58:20"))
        self.assertIn("restated-only -- still-open: DAN-9", line)


class RollingLogLineTests(unittest.TestCase):
    def test_matches_the_tick_finalize_format(self) -> None:
        self.assertTrue(is_rolling_log_line("2026-10-05T00:58:20.123Z: clean -- no findings this tick"))

    def test_rejects_an_incidental_comment(self) -> None:
        # DAN-720 hit exactly this: a human fixing the log issue's own
        # wrong status must never be read as a real tick.
        self.assertFalse(is_rolling_log_line("Fixed the status on this issue, it was wrongly blocked."))

    def test_rejects_a_bare_timestamp_with_no_colon_prefix(self) -> None:
        self.assertFalse(is_rolling_log_line("2026-10-05T00:58:20Z"))

    def test_most_recent_picks_the_latest_matching_comment(self) -> None:
        comments = [
            {"body": "unrelated human comment", "createdAt": "2026-10-05T01:00:00Z"},
            {"body": "2026-10-04T10:00:00Z: clean -- no findings this tick", "createdAt": "2026-10-04T10:00:00Z"},
            {"body": "2026-10-05T00:58:20Z: clean -- no findings this tick", "createdAt": "2026-10-05T00:58:20Z"},
        ]
        latest = most_recent_rolling_log_line(comments)
        assert latest is not None
        self.assertIn("2026-10-05T00:58:20Z", latest["body"])

    def test_no_matching_comment_returns_none(self) -> None:
        self.assertIsNone(most_recent_rolling_log_line([{"body": "just a comment", "createdAt": "2026-10-05T01:00:00Z"}]))


class FindOrCreateRollingLogTests(unittest.TestCase):
    MARKER = "Seat-health watchdog -- rolling log"

    def test_finds_existing_log_by_marker_and_assignee(self) -> None:
        client = FakePaperclipClient(
            issues=[{"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "blocked"}]
        )
        found = find_rolling_log(client, "co", self.MARKER, "watchdog-agent")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found["id"], "log1")

    def test_finds_it_even_when_status_is_blocked(self) -> None:
        # DAN-720: the log issue's own status has been observed wrong
        # (sitting at `blocked` with zero real blockers) -- the lookup
        # must not filter by status.
        client = FakePaperclipClient(
            issues=[{"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "blocked"}]
        )
        found = find_rolling_log(client, "co", self.MARKER, "watchdog-agent")
        self.assertIsNotNone(found)

    def test_ignores_a_title_match_for_a_different_assignee(self) -> None:
        client = FakePaperclipClient(
            issues=[{"id": "log1", "title": self.MARKER, "assigneeAgentId": "someone-else", "status": "todo"}]
        )
        self.assertIsNone(find_rolling_log(client, "co", self.MARKER, "watchdog-agent"))

    def test_creates_once_when_not_found(self) -> None:
        client = FakePaperclipClient(issues=[])
        found = find_or_create_rolling_log(client, "co", "proj", "watchdog-agent", self.MARKER, "Routine X")
        self.assertEqual(len(client.created), 1)
        self.assertEqual(found["title"], self.MARKER)

    def test_never_creates_a_duplicate_when_one_already_exists(self) -> None:
        client = FakePaperclipClient(
            issues=[{"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "todo"}]
        )
        found = find_or_create_rolling_log(client, "co", "proj", "watchdog-agent", self.MARKER, "Routine X")
        self.assertEqual(client.created, [])
        self.assertEqual(found["id"], "log1")


class FinalizeCleanTests(unittest.TestCase):
    MARKER = "Seat-health watchdog -- rolling log"

    def test_appends_to_log_and_cancels_this_issue(self) -> None:
        client = FakePaperclipClient(
            issues=[
                {"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "todo"},
                {"id": "tick1", "title": "tick", "status": "in_progress"},
            ]
        )
        result = finalize_clean(
            client, "co", "tick1", "proj", "watchdog-agent", self.MARKER, "Routine X", now=NOW
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(client.status_calls[-1][:2], ("tick1", "cancelled"))
        self.assertEqual(len(client._comments["log1"]), 1)
        self.assertTrue(client._comments["log1"][0]["body"].startswith("2026-10-05T00:58:20"))

    def test_omitted_note_uses_default_text(self) -> None:
        client = FakePaperclipClient(
            issues=[
                {"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "todo"},
                {"id": "tick1", "title": "tick", "status": "in_progress"},
            ]
        )
        finalize_clean(client, "co", "tick1", "proj", "watchdog-agent", self.MARKER, "Routine X", now=NOW)
        self.assertIn("clean -- no findings this tick", client._comments["log1"][0]["body"])

    def test_note_override_names_the_still_open_tracker(self) -> None:
        client = FakePaperclipClient(
            issues=[
                {"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "todo"},
                {"id": "tick1", "title": "tick", "status": "in_progress"},
            ]
        )
        finalize_clean(
            client,
            "co",
            "tick1",
            "proj",
            "watchdog-agent",
            self.MARKER,
            "Routine X",
            note="restated-only -- still-open: DAN-9",
            now=NOW,
        )
        self.assertIn("restated-only -- still-open: DAN-9", client._comments["log1"][0]["body"])

    def test_never_marks_this_issue_done(self) -> None:
        client = FakePaperclipClient(
            issues=[
                {"id": "log1", "title": self.MARKER, "assigneeAgentId": "watchdog-agent", "status": "todo"},
                {"id": "tick1", "title": "tick", "status": "in_progress"},
            ]
        )
        finalize_clean(client, "co", "tick1", "proj", "watchdog-agent", self.MARKER, "Routine X", now=NOW)
        self.assertNotEqual(client._issues["tick1"]["status"], "done")


class FinalizeExceptionTests(unittest.TestCase):
    def test_marks_this_issue_done_with_summary(self) -> None:
        client = FakePaperclipClient(issues=[{"id": "tick1", "title": "tick", "status": "in_progress"}])
        result = finalize_exception(client, "tick1", "Filed a new crash-loop tracker for seat X.")
        self.assertEqual(result["status"], "done")
        self.assertEqual(client.status_calls, [("tick1", "done", "Filed a new crash-loop tracker for seat X.")])
        self.assertEqual(client._issues["tick1"]["status"], "done")

    def test_never_touches_the_rolling_log(self) -> None:
        client = FakePaperclipClient(issues=[{"id": "tick1", "title": "tick", "status": "in_progress"}])
        finalize_exception(client, "tick1", "summary")
        self.assertEqual(client.created, [])


if __name__ == "__main__":
    unittest.main()
