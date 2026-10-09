"""Seat-health watchdog, arm 1 (DAN-218): detect a seat crash-looping.

A finding is one seat (`agentId`) whose most recent heartbeat runs show 3 or
more CONSECUTIVE non-succeeded, non-cancelled runs that produced no new
evidence -- a bare crash before the agent ever got a turn (`resultJson`
missing a `summary`, or carrying one byte-for-byte identical to the run's own
top-level `error` -- the `acpx_turn_failed` "...terminal access failure."
shape is the canonical example) -- spanning at least 10 minutes from the
oldest to the newest run in that streak. The span requirement exists so a
handful of retries fired seconds apart by the platform itself never counts;
a real crash loop burns wall-clock time.

Two kinds of run are exempt from the streak: they neither extend it nor reset
it, they are simply skipped over.

* A non-succeeded run carrying a REAL narrative of its own -- a distinct
  `resultJson.summary` that differs from the raw `error` (it ran a probe,
  posted a dispositive comment, the DAN-171 shape) -- even at $0, even
  several times in a row. The agent got a turn and did something; that is
  evidence the seat itself is not the problem.
* A run whose bare-crash text is the literal "...terminal limit failure."
  shape (`errorFamily: provider_quota` reads identically to a real crash by
  the summary/error test above, but it is a terminal/budget condition this
  watchdog does not own -- see `ops/limit-triage.py`).

Only a `succeeded` run resets the streak to zero; a `cancelled` run is
likewise skipped (it says nothing about whether the seat is unhealthy).

Findings are visibility-only: `--file-findings` dedupes on a stable
`[<agentId>]` marker in a tracking issue's title, owned by THIS watchdog's
own agent (never the affected seat) -- an already-open tracker gets an
update comment, a closed one is resumed once, and a tracker that is closed
and was already resumed once before only gets a plain comment
("noted_closed") with no status change. `ACTIONABLE: yes` on stdout iff at
least one finding this tick resulted in a brand-new tracker being filed
(DAN-491, narrowed from DAN-385's three-way updated/resumed/noted_closed
split): a human has not seen a brand-new tracker before, but has already
seen an updated or resumed one.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

MIN_STREAK = 3
MIN_SPAN_MINUTES = 10
RESUME_MARKER = "_(auto-resumed by ops/seat_watchdog.py)_"
EXCLUDED_ERROR_SUBSTRING = "terminal limit failure"

# DAN-491: narrowed to "filed" only. An already-open tracker merely being
# touched again is real activity, but it puts nothing NEW in front of a
# human on this tick's own execution issue.
ACTIONABLE_ACTIONS = {"filed"}


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_timestamp(run: dict[str, Any]) -> Optional[datetime]:
    return _parse_timestamp(run.get("startedAt")) or _parse_timestamp(run.get("finishedAt"))


def _bare_crash_text(run: dict[str, Any]) -> Optional[str]:
    """The run's own crash text, or None if this run is not a bare crash.

    A run is a bare crash (no new evidence) when its `resultJson.summary`
    is missing, or is identical to the run's top-level `error` field -- the
    agent produced nothing of its own before dying. Returns the matched
    text so callers can check it against the excluded-phrase list without
    re-deriving it.
    """
    result_json = run.get("resultJson") or {}
    summary = result_json.get("summary")
    raw_error = run.get("error")
    if not summary:
        return raw_error or ""
    if summary == raw_error:
        return summary
    return None


class RunClass:
    RESETS = "resets"
    COUNTS = "counts"
    SKIPS = "skips"


def classify_run(run: dict[str, Any]) -> str:
    """Classify one heartbeat run for the crash-streak walk."""
    status = run.get("status")
    if status == "succeeded":
        return RunClass.RESETS
    if status == "cancelled":
        return RunClass.SKIPS

    crash_text = _bare_crash_text(run)
    if crash_text is None:
        # Non-succeeded, non-cancelled, but with its own distinct
        # narrative -- exempt, per the DAN-171 shape.
        return RunClass.SKIPS
    if EXCLUDED_ERROR_SUBSTRING in crash_text:
        # Owned by ops/limit-triage.py, not this watchdog.
        return RunClass.SKIPS
    return RunClass.COUNTS


@dataclass
class SeatFinding:
    agent_id: str
    streak_runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def streak_length(self) -> int:
        return len(self.streak_runs)

    @property
    def span_minutes(self) -> float:
        timestamps = [ts for ts in (_run_timestamp(r) for r in self.streak_runs) if ts is not None]
        if len(timestamps) < 2:
            return 0.0
        return (max(timestamps) - min(timestamps)).total_seconds() / 60.0

    @property
    def latest_run(self) -> dict[str, Any]:
        return self.streak_runs[0]

    @property
    def oldest_run(self) -> dict[str, Any]:
        return self.streak_runs[-1]


def find_seat_finding(agent_id: str, runs_newest_first: Iterable[dict[str, Any]]) -> Optional[SeatFinding]:
    """Walk one seat's runs (newest first) for a currently-active crash streak.

    Stops the moment a `succeeded` run is found -- anything before it
    already resolved. `cancelled` and exempt runs are skipped without
    breaking the walk, so a probe or a cancellation in the middle of a real
    crash loop never hides it.
    """
    streak: list[dict[str, Any]] = []
    for run in runs_newest_first:
        cls = classify_run(run)
        if cls == RunClass.RESETS:
            break
        if cls == RunClass.COUNTS:
            streak.append(run)
        # RunClass.SKIPS: continue past it without touching the streak.

    if len(streak) < MIN_STREAK:
        return None

    finding = SeatFinding(agent_id=agent_id, streak_runs=streak)
    if finding.span_minutes < MIN_SPAN_MINUTES:
        return None
    return finding


def group_runs_by_seat(runs: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_seat: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        agent_id = run.get("agentId")
        if not agent_id:
            continue
        by_seat.setdefault(agent_id, []).append(run)
    return by_seat


def find_findings(runs: Iterable[dict[str, Any]]) -> list[SeatFinding]:
    """Findings across all seats present in `runs`.

    `runs` must already be sorted newest-first per seat (the heartbeat-runs
    list endpoint returns that order; this function does not re-sort, so a
    caller feeding it oldest-first data will silently get wrong answers --
    deliberately not defended against here, to keep this a thin, auditable
    wrapper around `find_seat_finding` rather than a second place that
    encodes sort order).
    """
    findings = []
    for agent_id, seat_runs in group_runs_by_seat(runs).items():
        finding = find_seat_finding(agent_id, seat_runs)
        if finding is not None:
            findings.append(finding)
    return findings


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip issues/runs API.

    Kept separate from the dedupe logic so tests can swap in a fake that
    never touches the network.
    """

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

    def list_recent_runs(self, company_id: str, limit: int) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/heartbeat-runs?limit={limit}"
        result = self._request("GET", path)
        return result.get("runs", []) if isinstance(result, dict) else result

    def find_tracker(self, company_id: str, agent_id: str) -> Optional[dict[str, Any]]:
        marker = f"[{agent_id}]"
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


def _tracker_title(agent_id: str) -> str:
    return f"Seat-health watchdog: crash-loop on seat [{agent_id}]"


def _finding_summary(finding: SeatFinding) -> str:
    return (
        f"{finding.streak_length} consecutive non-succeeded, non-cancelled run(s) with no new "
        f"evidence, spanning {finding.span_minutes:.1f} minutes "
        f"(latest run {finding.latest_run.get('id')}, oldest {finding.oldest_run.get('id')})."
    )


def dedupe_and_file(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    assignee_agent_id: str,
    finding: SeatFinding,
) -> str:
    """File, update, resume, or note a finding's tracker. Returns the action taken."""
    existing = client.find_tracker(company_id, finding.agent_id)
    if existing is None:
        client.create_tracker(
            company_id,
            project_id,
            assignee_agent_id,
            _tracker_title(finding.agent_id),
            _finding_summary(finding),
        )
        return "filed"

    if existing.get("status") not in ("done", "cancelled"):
        client.comment(existing["id"], _finding_summary(finding))
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
    runs = client.list_recent_runs(company_id, limit)
    findings = find_findings(runs)

    report: dict[str, Any] = {"findings": [], "actionable": False}
    for finding in findings:
        row: dict[str, Any] = {
            "agentId": finding.agent_id,
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
    parser.add_argument("--limit", type=int, default=300)
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
