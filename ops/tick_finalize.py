"""Routine-agnostic tick finalizer (DAN-289, DAN-385 requirement 6).

Closes out one routine tick's own execution issue along exactly one of two
paths:

* `--clean`: nothing actionable happened this tick (no findings at all, or
  every finding was a restatement of something already known -- DAN-385's
  restated-only extension of the clean path). Appends one timestamped line
  to a single persistent rolling-log issue (found once by a stable title
  marker + assignee, created once if it doesn't exist yet, reused forever
  after -- never a new issue on a clean or restated-only tick), then marks
  THIS tick's own issue `cancelled` (not `done`) with a pointer comment.
  `cancelled`, not `done`, so an empty or restated-only tick never inflates
  completion counts (DAN-289: "14 of Dante's 20 completions" were empty
  watchdog ticks before this existed).
* `--exception-summary`: real, new-to-a-human work happened this tick.
  Marks THIS tick's own issue `done` with the given one-line summary as
  the closing comment.

Deliberately routine-agnostic (no seat-watchdog-specific text anywhere in
this module) so any other routine with the same "report on exception, stay
quiet otherwise" shape can adopt it unchanged -- the rolling-log marker,
routine label, and assignee are all caller-supplied.

The timestamp-prefixed comment format appended on the clean path
(`<ISO-8601>: <note>`) is load-bearing: the seat-health watchdog's own
dead-man-switch step (DAN-720) scans the rolling log for the most recent
comment matching exactly this pattern to find the last time a tick
actually reached its arms, deliberately NOT trusting the issue's bare
"most recent comment" (an incidental human/agent comment would reset that
clock without a real tick ever running). Do not change this format without
checking every reader of it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

ROLLING_LOG_COMMENT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}): ")

DEFAULT_CLEAN_NOTE = "clean -- no findings this tick"


def clean_note(note: Optional[str], now: Optional[datetime] = None) -> str:
    """The exact `<ISO-8601>: <note>` line appended to the rolling log."""
    now = now or datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    return f"{timestamp}: {note or DEFAULT_CLEAN_NOTE}"


def is_rolling_log_line(body: Optional[str]) -> bool:
    """True iff `body` matches the exact tick_finalize.py append pattern.

    Used by callers (e.g. the seat-health watchdog's dead-man switch) that
    must find the last REAL tick, not merely the issue's most recent
    comment -- an incidental status fix or question would otherwise reset
    the clock without a tick ever reaching its arms.
    """
    return bool(body) and bool(ROLLING_LOG_COMMENT_PATTERN.match(body or ""))


def most_recent_rolling_log_line(comments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    candidates = [c for c in comments if is_rolling_log_line(c.get("body"))]
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.get("createdAt") or "")


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip issues API."""

    def __init__(self, api_base: str, api_key: str, run_id: Optional[str] = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        if self.run_id:
            req.add_header("X-Paperclip-Run-Id", self.run_id)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def search_issues_by_text(self, company_id: str, text: str, limit: int = 50) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/issues?q={urllib.parse.quote(text)}&limit={limit}"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def create_issue(
        self, company_id: str, project_id: str, assignee_agent_id: str, title: str, description: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/companies/{company_id}/issues",
            {
                "title": title,
                "description": description,
                "projectId": project_id,
                "assigneeAgentId": assignee_agent_id,
            },
        )

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def comment(self, issue_id: str, body: str) -> Any:
        return self._request("POST", f"/api/issues/{issue_id}/comments", {"body": body})

    def set_status(self, issue_id: str, status: str, comment: str) -> Any:
        return self._request("PATCH", f"/api/issues/{issue_id}", {"status": status, "comment": comment})


def find_rolling_log(
    client: PaperclipClient, company_id: str, marker: str, assignee_agent_id: str
) -> Optional[dict[str, Any]]:
    """Find the persistent rolling-log issue by title marker + assignee.

    Independent of status -- the log issue's own status has been observed
    wrong (DAN-720 found it sitting at `blocked` with zero real blockers),
    and a status-filtered lookup would silently miss it and mint a
    duplicate.
    """
    for issue in client.search_issues_by_text(company_id, marker):
        if marker in (issue.get("title") or "") and issue.get("assigneeAgentId") == assignee_agent_id:
            return issue
    return None


def find_or_create_rolling_log(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    assignee_agent_id: str,
    marker: str,
    routine_label: str,
) -> dict[str, Any]:
    existing = find_rolling_log(client, company_id, marker, assignee_agent_id)
    if existing is not None:
        return existing
    return client.create_issue(
        company_id,
        project_id,
        assignee_agent_id,
        marker,
        f"Persistent rolling log for {routine_label}. One timestamped line is appended per "
        "clean or restated-only tick; never closed, never duplicated -- see ops/tick_finalize.py.",
    )


def finalize_clean(
    client: PaperclipClient,
    company_id: str,
    this_issue_id: str,
    project_id: str,
    assignee_agent_id: str,
    rolling_log_marker: str,
    routine_label: str,
    note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    rolling_log = find_or_create_rolling_log(
        client, company_id, project_id, assignee_agent_id, rolling_log_marker, routine_label
    )
    client.comment(rolling_log["id"], clean_note(note, now))
    pointer = f"Clean tick ({routine_label}) -- logged on {rolling_log.get('identifier', rolling_log['id'])}."
    client.set_status(this_issue_id, "cancelled", pointer)
    return {"path": "clean", "rollingLogIssueId": rolling_log["id"], "status": "cancelled"}


def finalize_exception(client: PaperclipClient, this_issue_id: str, exception_summary: str) -> dict[str, Any]:
    client.set_status(this_issue_id, "done", exception_summary)
    return {"path": "exception", "status": "done"}


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--this-issue-id", required=True)
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--exception-summary")
    parser.add_argument("--project-id")
    parser.add_argument("--assignee-agent-id")
    parser.add_argument("--rolling-log-marker")
    parser.add_argument("--routine-label")
    parser.add_argument("--note")
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    args = parser.parse_args(argv)

    if bool(args.clean) == bool(args.exception_summary):
        parser.error("exactly one of --clean or --exception-summary is required")
    if args.clean and not (args.project_id and args.assignee_agent_id and args.rolling_log_marker and args.routine_label):
        parser.error("--clean requires --project-id, --assignee-agent-id, --rolling-log-marker, and --routine-label")
    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )

    if args.clean:
        result = finalize_clean(
            client,
            args.company_id,
            args.this_issue_id,
            args.project_id,
            args.assignee_agent_id,
            args.rolling_log_marker,
            args.routine_label,
            note=args.note,
        )
    else:
        result = finalize_exception(client, args.this_issue_id, args.exception_summary)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
