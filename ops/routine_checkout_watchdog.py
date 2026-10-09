"""Cross-routine no-checkout detection arm (DAN-666, follow-up to DAN-645).

DAN-645 found the Default-approver gate sweep routine producing ticks at a
steady cadence for ~19 hours, none of which were ever checked out -- so
their own step-0 orphan-guards never ran, so the backlog they are supposed
to drain never drained, and nothing anywhere in the company alerted on it.
A routine's ticks being minted fine but never picked up by an agent run is
invisible everywhere else, including the seat-health watchdog's own first
two arms, which only ever looked at seat crash-loops and stranded approval
stages.

This arm pulls every OTHER active schedule-triggered routine (it always
excludes `SELF_ROUTINE_ID` -- this watchdog's own routine -- because a
routine cannot reliably detect its own checkout failure) and, per routine,
walks its most recent `--limit` runs newest-first for an unbroken streak of
`status: in_progress` ticks whose linked issue has BOTH `checkoutRunId` and
`executionRunId` null -- re-fetched fresh per issue, never trusting a
cached `linkedIssue.status`, the same discipline the seat-health arm's own
step 0 uses. A streak only qualifies once its elapsed span (oldest streak
run's `triggeredAt` to now) is at least 2x that routine's OWN configured
cadence (`nextRunAt - lastFiredAt` off its schedule trigger -- no cron
parsing needed), so one transient single-tick miss never false-positives.

Dedupes on the routine (`[<routineId>]` marker in a tracking issue's
title, the same shape as `seat_watchdog.py`'s `[<agentId>]` marker):

* No existing tracker -> file one (visibility only, assigned to this
  watchdog's own owner) and mention the affected routine's owning agent so
  they are woken to self-trigger it (`POST /api/routines/{id}/run`).
* An existing tracker still open proves the condition has now persisted
  across at least two consecutive watchdog ticks -- this arm's own
  escalation trigger -- so that update additionally mentions Cloud,
  throttled to once per hour so an unresolved finding does not re-ping (and
  re-wake Cloud) every tick.
* An existing tracker that is closed is resumed once; a further
  re-occurrence after that only gets a plain comment (`noted_closed`),
  mirroring `seat_watchdog.py`'s dedupe state machine.

`ACTIONABLE: yes` on stdout iff at least one finding this tick was "filed"
-- a brand-new tracker a human has not seen before, the same narrowing
DAN-491 applied to the other two arms.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

# This watchdog's own routine (the "Seat-health watchdog (narrow arm)"
# routine, DAN-218/DAN-666) -- always excluded, since a routine cannot
# reliably detect its own checkout failure.
SELF_ROUTINE_ID = "6c72d65a-5220-49a7-a81d-6dc7db32aebc"

# Cloud (CEO seat) -- see DAN-666's escalation step.
CLOUD_AGENT_ID = "c28db8ef-7f04-4c21-b575-ddee819e050a"

CLOUD_ESCALATION_WINDOW_HOURS = 1
RESUME_MARKER = "_(auto-resumed by ops/routine_checkout_watchdog.py)_"
CLOUD_MENTION_MARKER = "_(cloud-escalation by ops/routine_checkout_watchdog.py)_"

ACTIONABLE_ACTIONS = {"filed"}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def routine_cadence_minutes(routine: dict[str, Any]) -> Optional[float]:
    """The routine's own configured cadence, read off its schedule trigger
    as `nextRunAt - lastFiredAt` -- no cron parsing needed. None when
    either timestamp is missing (a routine that has never fired, or whose
    trigger carries no schedule), in which case this routine cannot be
    evaluated and must be skipped rather than guessed at.
    """
    for trigger in routine.get("triggers") or []:
        if trigger.get("kind") != "schedule":
            continue
        next_run = _parse_timestamp(trigger.get("nextRunAt"))
        last_fired = _parse_timestamp(trigger.get("lastFiredAt"))
        if next_run is None or last_fired is None:
            return None
        return (next_run - last_fired).total_seconds() / 60.0
    return None


def is_never_checked_out(issue: dict[str, Any]) -> bool:
    return (
        issue.get("status") == "in_progress"
        and issue.get("checkoutRunId") is None
        and issue.get("executionRunId") is None
    )


def find_checkout_streak(
    runs_newest_first: list[dict[str, Any]], issue_lookup: Callable[[str], Optional[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Unbroken streak of never-checked-out runs, newest-first, stopping at
    the first run whose linked issue does not qualify (including a run
    with no linked issue at all -- a platform-level pre-tick-creation
    failure is a different fault shape, not this arm's concern)."""
    streak: list[dict[str, Any]] = []
    for run in runs_newest_first:
        linked_issue_id = run.get("linkedIssueId")
        if not linked_issue_id:
            break
        issue = issue_lookup(linked_issue_id)
        if issue is None or not is_never_checked_out(issue):
            break
        streak.append(run)
    return streak


@dataclass
class RoutineFinding:
    routine_id: str
    routine_title: str
    owner_agent_id: Optional[str]
    streak_runs: list[dict[str, Any]]
    cadence_minutes: float
    span_minutes: float

    @property
    def streak_length(self) -> int:
        return len(self.streak_runs)

    @property
    def threshold_minutes(self) -> float:
        return 2 * self.cadence_minutes


def _streak_span_minutes(streak_runs: list[dict[str, Any]], now: datetime) -> float:
    timestamps = [
        ts for ts in (_parse_timestamp(r.get("triggeredAt")) for r in streak_runs) if ts is not None
    ]
    if not timestamps:
        return 0.0
    return (now - min(timestamps)).total_seconds() / 60.0


def evaluate_routine(
    routine: dict[str, Any],
    runs_newest_first: list[dict[str, Any]],
    issue_lookup: Callable[[str], Optional[dict[str, Any]]],
    now: Optional[datetime] = None,
) -> Optional[RoutineFinding]:
    now = now or datetime.now(timezone.utc)
    if routine.get("id") == SELF_ROUTINE_ID:
        return None
    cadence = routine_cadence_minutes(routine)
    if cadence is None or cadence <= 0:
        return None

    streak = find_checkout_streak(runs_newest_first, issue_lookup)
    if not streak:
        return None

    span_minutes = _streak_span_minutes(streak, now)
    finding = RoutineFinding(
        routine_id=routine["id"],
        routine_title=routine.get("title") or routine["id"],
        owner_agent_id=routine.get("assigneeAgentId"),
        streak_runs=streak,
        cadence_minutes=cadence,
        span_minutes=span_minutes,
    )
    if finding.span_minutes < finding.threshold_minutes:
        return None
    return finding


def find_findings(
    routines: list[dict[str, Any]],
    runs_by_routine: dict[str, list[dict[str, Any]]],
    issue_lookup: Callable[[str], Optional[dict[str, Any]]],
    now: Optional[datetime] = None,
) -> list[RoutineFinding]:
    now = now or datetime.now(timezone.utc)
    findings = []
    for routine in routines:
        runs = runs_by_routine.get(routine["id"], [])
        finding = evaluate_routine(routine, runs, issue_lookup, now=now)
        if finding is not None:
            findings.append(finding)
    return findings


def _mention(agent_id: str) -> str:
    return f"[@agent](agent://{agent_id})"


def _tracker_title(routine_id: str) -> str:
    return f"Routine checkout watchdog: never-checked-out ticks on routine [{routine_id}]"


def _finding_summary(finding: RoutineFinding) -> str:
    summary = (
        f"Routine '{finding.routine_title}' ({finding.routine_id}) has {finding.streak_length} "
        f"consecutive in_progress tick(s) never checked out, spanning {finding.span_minutes:.1f} minutes "
        f"(threshold {finding.threshold_minutes:.1f} minutes = 2x its own {finding.cadence_minutes:.1f}-minute cadence)."
    )
    if finding.owner_agent_id:
        summary += f" Owning agent: {_mention(finding.owner_agent_id)} -- please self-trigger via POST /api/routines/{finding.routine_id}/run."
    return summary


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip routines/issues API."""

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

    def list_active_schedule_routines(self, company_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/companies/{company_id}/routines")
        routines = result.get("routines", []) if isinstance(result, dict) else result
        return [
            r
            for r in routines
            if r.get("status") != "archived"
            and any(t.get("kind") == "schedule" and t.get("enabled") for t in r.get("triggers") or [])
        ]

    def list_recent_runs(self, routine_id: str, limit: int) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/routines/{routine_id}/runs?limit={limit}")
        return result.get("runs", []) if isinstance(result, dict) else result

    def get_issue(self, issue_id: str) -> Optional[dict[str, Any]]:
        return self._request("GET", f"/api/issues/{issue_id}")

    def find_tracker(self, company_id: str, routine_id: str) -> Optional[dict[str, Any]]:
        marker = f"[{routine_id}]"
        path = f"/api/companies/{company_id}/issues?q={urllib.parse.quote(marker)}&limit=50"
        result = self._request("GET", path)
        issues = result.get("issues", []) if isinstance(result, dict) else result
        for issue in issues:
            if marker in (issue.get("title") or ""):
                return issue
        return None

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def create_tracker(
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
                "priority": "medium",
            },
        )

    def comment(self, issue_id: str, body: str) -> Any:
        return self._request("POST", f"/api/issues/{issue_id}/comments", {"body": body})

    def resume(self, issue_id: str, comment: str) -> Any:
        return self._request("PATCH", f"/api/issues/{issue_id}", {"status": "todo", "comment": comment})


def _last_marker_at(comments: list[dict[str, Any]], marker: str) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for comment in comments:
        if marker not in (comment.get("body") or ""):
            continue
        ts = _parse_timestamp(comment.get("createdAt"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def dedupe_and_file(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    assignee_agent_id: str,
    finding: RoutineFinding,
    now: Optional[datetime] = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    existing = client.find_tracker(company_id, finding.routine_id)
    if existing is None:
        client.create_tracker(
            company_id, project_id, assignee_agent_id, _tracker_title(finding.routine_id), _finding_summary(finding)
        )
        return "filed"

    if existing.get("status") not in ("done", "cancelled"):
        # Persisted across >=2 consecutive watchdog ticks -- escalate to
        # Cloud too, throttled to once per hour.
        comments = client.list_comments(existing["id"])
        last_cloud_mention = _last_marker_at(comments, CLOUD_MENTION_MARKER)
        escalate = last_cloud_mention is None or (now - last_cloud_mention) >= timedelta(
            hours=CLOUD_ESCALATION_WINDOW_HOURS
        )
        body = _finding_summary(finding)
        if escalate:
            body += f"\n\n{CLOUD_MENTION_MARKER} Still unresolved across consecutive ticks -- {_mention(CLOUD_AGENT_ID)}."
        client.comment(existing["id"], body)
        return "updated"

    comments = client.list_comments(existing["id"])
    already_resumed = any(RESUME_MARKER in (c.get("body") or "") for c in comments)
    if not already_resumed:
        client.resume(existing["id"], f"{RESUME_MARKER} {_finding_summary(finding)}")
        return "resumed"

    client.comment(existing["id"], f"Still happening (tracker already closed and previously resumed): {_finding_summary(finding)}")
    return "noted_closed"


def run_watchdog(
    client: PaperclipClient,
    company_id: str,
    limit: int,
    file_findings: bool,
    project_id: Optional[str] = None,
    assignee_agent_id: Optional[str] = None,
) -> dict[str, Any]:
    routines = client.list_active_schedule_routines(company_id)
    runs_by_routine = {r["id"]: client.list_recent_runs(r["id"], limit) for r in routines}

    issue_cache: dict[str, Optional[dict[str, Any]]] = {}

    def issue_lookup(issue_id: str) -> Optional[dict[str, Any]]:
        if issue_id not in issue_cache:
            issue_cache[issue_id] = client.get_issue(issue_id)
        return issue_cache[issue_id]

    findings = find_findings(routines, runs_by_routine, issue_lookup)

    report: dict[str, Any] = {"findings": [], "actionable": False}
    for finding in findings:
        row: dict[str, Any] = {
            "routineId": finding.routine_id,
            "streakLength": finding.streak_length,
            "spanMinutes": round(finding.span_minutes, 1),
        }
        if file_findings:
            assert project_id and assignee_agent_id, "--file-findings requires --project-id and --assignee-agent-id"
            action = dedupe_and_file(client, company_id, project_id, assignee_agent_id, finding)
            row["action"] = action
            if action in ACTIONABLE_ACTIONS:
                report["actionable"] = True
        report["findings"].append(row)
    return report


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--file-findings", action="store_true")
    parser.add_argument("--project-id")
    parser.add_argument("--assignee-agent-id")
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    args = parser.parse_args(argv)

    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")
    if args.file_findings and not (args.project_id and args.assignee_agent_id):
        parser.error("--file-findings requires --project-id and --assignee-agent-id")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )
    report = run_watchdog(
        client,
        args.company_id,
        args.limit,
        args.file_findings,
        project_id=args.project_id,
        assignee_agent_id=args.assignee_agent_id,
    )
    print(json.dumps(report, indent=2))
    print(f"ACTIONABLE: {'yes' if report['actionable'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
