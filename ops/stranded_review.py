"""Stranded-approval-stage detector (DAN-267): wake a participant nothing is waking.

DAN-236 and DAN-213 both sat `in_review` for hours after a run-level failure,
and the mistaken belief at the time was that a dead run had frozen them and
only the board could unfreeze them. It was wrong: `checkoutRunId`,
`executionRunId`, and `executionLockedAt` were all null on both -- no lock --
and `executionState.status` was `pending` with a real `currentParticipant`.
The stage outlives the run that was driving it. The remedy needs no board
access at all: a comment on the issue wakes the current participant, who can
then advance the stage (`422 "Only the active reviewer or approver can
advance the current execution stage"` from anyone else is why this script
only ever comments, never PATCHes the stage itself).

A finding is one issue where ALL of:

1. `status == "in_review"`, and not a `routine_execution` issue (those
   self-report by design; gating them would wedge the automation), and
2. `executionState.status == "pending"` with a non-null `currentParticipant`,
   and
3. no live run is driving it -- `checkoutRunId`/`executionRunId`/
   `executionLockedAt` are all null (no lock at all, the DAN-236/DAN-213
   shape), OR a lock exists but `GET /api/issues/{id}/execution` reports a
   dead `phase` (`failed`/`recovery_needed`/`cancelled`) for it anyway, and
4. the stage has been pending with no new decision for >= 30 minutes --
   read from `reviewAttention.paths[].since` for the `execution_participant`
   path, falling back to the issue's own last-activity timestamp when that
   path is absent.

Action on a finding is a wake, nothing else: one comment on the stranded
issue addressed to the current participant, naming the null-lock evidence
(or the dead execution phase) and the exact PATCH that advances the stage.
Never a status change, never a reassignment, never a new issue. Capped to
one (re-)poke per 6 hours per issue via an idempotency marker in the
comment itself. A roll-up comment naming every finding this tick is also
posted on the routine's own tick issue (`--post-comment-on`), so there is
one place to read the day's strandings.

`ACTIONABLE: yes` on stdout iff at least one finding this tick was actually
due to be (re-)poked (DAN-385) -- a finding that is still inside its own 6h
repoke window is a pure restatement of an already-known stall, exactly the
shape that let DAN-213 re-report as a fresh exception every 15 minutes for
8.4 hours before this fix existed.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

STRANDED_THRESHOLD_MINUTES = 30
REPOKE_WINDOW_HOURS = 6
DEAD_EXECUTION_PHASES = {"failed", "recovery_needed", "cancelled"}
POKE_MARKER = "_(auto-poke by ops/stranded_review.py)_"
LOCK_FIELDS = ("checkoutRunId", "executionRunId", "executionLockedAt")


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_lock(issue: dict[str, Any]) -> bool:
    return any(issue.get(field) for field in LOCK_FIELDS)


def _no_live_run(issue: dict[str, Any], execution_phase: Optional[str]) -> bool:
    if not _has_lock(issue):
        return True
    return execution_phase in DEAD_EXECUTION_PHASES


def _pending_since(issue: dict[str, Any]) -> Optional[datetime]:
    review_attention = issue.get("reviewAttention") or {}
    for path in review_attention.get("paths") or []:
        if path.get("path") == "execution_participant":
            ts = _parse_timestamp(path.get("since"))
            if ts is not None:
                return ts
    # Fall back to the issue's own last-activity timestamp.
    return _parse_timestamp(issue.get("updatedAt")) or _parse_timestamp(issue.get("createdAt"))


def _participant_label(participant: Any) -> str:
    if isinstance(participant, dict):
        agent_id = participant.get("agentId") or participant.get("id")
        name = participant.get("name")
        if agent_id:
            return f"[@{name or agent_id}](agent://{agent_id})"
        return str(name or participant)
    return f"[@participant](agent://{participant})"


@dataclass
class StrandedFinding:
    issue_id: str
    identifier: str
    participant: Any
    pending_since: datetime
    pending_minutes: float
    last_decision_id: Optional[str]
    last_decision_outcome: Optional[str]
    dead_execution_phase: Optional[str]


def find_finding(
    issue: dict[str, Any],
    now: datetime,
    execution_phase_lookup: Callable[[str], Optional[str]],
) -> Optional[StrandedFinding]:
    """Evaluate one issue. `execution_phase_lookup` is only called when the
    issue carries a lock, so a clean DAN-236/DAN-213-shaped finding never
    costs a second API round trip."""
    if issue.get("status") != "in_review":
        return None
    if issue.get("workMode") == "routine_execution":
        return None

    execution_state = issue.get("executionState") or {}
    if execution_state.get("status") != "pending":
        return None
    participant = execution_state.get("currentParticipant")
    if not participant:
        return None

    execution_phase = execution_phase_lookup(issue["id"]) if _has_lock(issue) else None
    if not _no_live_run(issue, execution_phase):
        return None

    pending_since = _pending_since(issue)
    if pending_since is None:
        return None
    pending_minutes = (now - pending_since).total_seconds() / 60.0
    if pending_minutes < STRANDED_THRESHOLD_MINUTES:
        return None

    return StrandedFinding(
        issue_id=issue["id"],
        identifier=issue.get("identifier", issue["id"]),
        participant=participant,
        pending_since=pending_since,
        pending_minutes=pending_minutes,
        last_decision_id=execution_state.get("lastDecisionId"),
        last_decision_outcome=execution_state.get("lastDecisionOutcome"),
        dead_execution_phase=execution_phase,
    )


def find_findings(
    issues: list[dict[str, Any]],
    now: datetime,
    execution_phase_lookup: Callable[[str], Optional[str]],
) -> list[StrandedFinding]:
    findings = []
    for issue in issues:
        finding = find_finding(issue, now, execution_phase_lookup)
        if finding is not None:
            findings.append(finding)
    return findings


def _poke_body(finding: StrandedFinding) -> str:
    lines = [
        POKE_MARKER,
        "",
        f"{_participant_label(finding.participant)} -- this issue's approval/execution stage has "
        f"been pending for {finding.pending_minutes:.0f} minutes with no run driving it.",
    ]
    if finding.dead_execution_phase:
        lines.append(f"Execution phase is `{finding.dead_execution_phase}` -- the run is gone, the stage is not.")
    else:
        lines.append("No active lock (`checkoutRunId`/`executionRunId`/`executionLockedAt` are all null).")
    if finding.last_decision_id:
        lines.append(f"A decision is already recorded: `{finding.last_decision_id}` ({finding.last_decision_outcome}).")
    else:
        lines.append("No decision has been recorded yet.")
    lines.append(
        "Advance it with `PATCH /api/issues/"
        + finding.issue_id
        + '` carrying `{"status": "done", "comment": "Approved: ..."}` (or `in_progress` to request changes).'
    )
    return "\n".join(lines)


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip issues/execution API."""

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

    def list_in_review_issues(self, company_id: str, limit: int) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/issues?status=in_review&limit={limit}"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def get_execution_phase(self, issue_id: str) -> Optional[str]:
        result = self._request("GET", f"/api/issues/{issue_id}/execution")
        return result.get("phase") if isinstance(result, dict) else None

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def comment(self, issue_id: str, body: str) -> Any:
        return self._request("POST", f"/api/issues/{issue_id}/comments", {"body": body})


def _last_poke_at(comments: list[dict[str, Any]]) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for comment in comments:
        if POKE_MARKER not in (comment.get("body") or ""):
            continue
        ts = _parse_timestamp(comment.get("createdAt"))
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def poke_if_due(
    client: PaperclipClient, finding: StrandedFinding, now: datetime, post: bool
) -> str:
    """Returns "poked" or "skipped_window". Never mutates when `post` is False."""
    comments = client.list_comments(finding.issue_id)
    last_poke = _last_poke_at(comments)
    if last_poke is not None and (now - last_poke) < timedelta(hours=REPOKE_WINDOW_HOURS):
        return "skipped_window"
    if post:
        client.comment(finding.issue_id, _poke_body(finding))
    return "poked"


def _rollup_body(results: list[tuple[StrandedFinding, str]]) -> str:
    lines = ["Stranded-approval-stage findings this tick:"]
    for finding, action in results:
        lines.append(
            f"- [{finding.identifier}](/DAN/issues/{finding.identifier}): pending "
            f"{finding.pending_minutes:.0f}m, participant {_participant_label(finding.participant)} -- {action}"
        )
    return "\n".join(lines)


def run_stranded_review(
    client: PaperclipClient,
    company_id: str,
    limit: int,
    post: bool,
    post_comment_on: Optional[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    issues = client.list_in_review_issues(company_id, limit)
    findings = find_findings(issues, now, client.get_execution_phase)

    results: list[tuple[StrandedFinding, str]] = []
    actionable = False
    for finding in findings:
        action = poke_if_due(client, finding, now, post)
        results.append((finding, action))
        if action == "poked":
            actionable = True

    if results and post and post_comment_on:
        client.comment(post_comment_on, _rollup_body(results))

    return {
        "actionable": actionable,
        "findings": [
            {
                "identifier": finding.identifier,
                "issueId": finding.issue_id,
                "pendingMinutes": round(finding.pending_minutes, 1),
                "action": action,
            }
            for finding, action in results
        ],
    }


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument(
        "--post", action="store_true", help="actually post pokes/roll-up; default is dry-run/report-only"
    )
    parser.add_argument("--post-comment-on", help="issue id to receive the roll-up comment")
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    args = parser.parse_args(argv)

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
    report = run_stranded_review(
        client, args.company_id, args.limit, args.post, args.post_comment_on
    )
    print(json.dumps(report, indent=2))
    print(f"ACTIONABLE: {'yes' if report['actionable'] else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
