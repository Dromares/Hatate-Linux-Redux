"""Visibility-only sweep for reconciliation-held issues (DAN-496).

An issue is **held** when `GET /api/issues/{id}` returns a non-null
`executionBlocker`. Mechanism: a settled `issueRecoveryActions` row with
`evidence.automaticRecovery.replay = 'blocked'` still matches
`executionBlockerPredicate()` even at `status: resolved`, so the blocker
stays populated and every wake to the issue parks as
`deferred_issue_execution` with `claimedAt: null` -- forever, independent of
the issue's own status or assignee. DAN-213, DAN-236, DAN-314 and DAN-289
each burned hours in this state before a human noticed by hand.

Why this needs per-issue GETs and no bulk shortcut (verified 2026-10-04):
`GET /api/issues/{id}/recovery-actions` reports `active: null` for a held
issue (the hold is *settled*, not active), company `recovery-observability`
reports `activeCount: 0`, and the bulk `GET /api/companies/{id}/issues` list
does not include `executionBlocker` on any item at all. Only the per-issue
detail GET carries it.

The only thing that clears a hold is one plain comment on the issue from a
human board user (`admitExplicitNativeContinuation`'s first line is
`if (input.actorType !== "user" || !actorId) return null`). That comment
bounces unless every one of these holds, so this sweep checks and reports
each by name instead of just raising an alarm:

  - the issue's current `assigneeAgentId` still equals the hold run's
    `agentId` -- reassigning a held issue breaks this permanently, which is
    exactly how DAN-213 got stuck for good
  - the issue's status is not `done`/`cancelled` (closing a held issue is a
    one-way door: no board comment can ever reopen it)
  - the assignee's `adapterType` is a real continuation-capable adapter
  - the hold run itself is terminal, has `finishedAt` set, and its
    `nativeIssueId` (or `contextSnapshot.issueId`) still points at this issue
  - there is no pending issue-thread interaction and no pending/
    revision-requested approval in the way

This script never writes to the held issue itself -- creating or updating a
separate tracker issue is as far as it goes. Clearing a hold is board-only
by design; this only makes holds visible before they go unnoticed for hours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

NON_TERMINAL_STATUSES = "todo,in_progress,in_review,blocked"

# services/explicit-native-continuation.js's CONVERSATION_ADAPTER_TYPES, plus
# the paperclip_runner carve-out -- see the standing "Wedged issues" note.
CONVERSATION_ADAPTER_TYPES = {
    "claude_local",
    "codex_local",
    "cursor",
    "gemini_local",
    "opencode_local",
    "pi_local",
    "grok_local",
    "kimi_local",
    "hermes_local",
}

TERMINAL_RUN_STATUSES = {"failed", "interrupted", "timed_out", "cancelled"}

TRACKER_TITLE_PREFIX = "Held issue:"
_FINGERPRINT_RE = re.compile(r"<!-- held-sweep-fingerprint: ([0-9a-f]{12}) -->")


def _fingerprint_marker(fingerprint: str) -> str:
    return f"<!-- held-sweep-fingerprint: {fingerprint} -->"


@dataclass
class Preflight:
    """The admission pre-flight for one held issue's one-line remedy."""

    admits: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class HeldIssue:
    issue_id: str
    identifier: str
    title: str
    status: str
    assignee_agent_id: Optional[str]
    cause: str
    hold_run_id: str
    hold_agent_id: str
    held_since: Optional[str]
    preflight: Preflight

    def fingerprint(self) -> str:
        basis = "|".join(
            [
                self.status,
                str(self.assignee_agent_id),
                self.cause,
                self.hold_run_id,
                str(self.preflight.admits),
                "|".join(sorted(self.preflight.reasons)),
            ]
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:12]


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip API.

    Kept separate from `sweep`/`file_or_update_trackers` so tests can swap in
    a fake that never touches the network.
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
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/issues?status={NON_TERMINAL_STATUSES}&limit=400"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/issues/{issue_id}")

    def get_agent(self, agent_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not agent_id:
            return None
        try:
            return self._request("GET", f"/api/agents/{agent_id}")
        except urllib.error.HTTPError:
            return None

    def get_run(self, run_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not run_id:
            return None
        try:
            return self._request("GET", f"/api/heartbeat-runs/{run_id}")
        except urllib.error.HTTPError:
            return None

    def get_wakes(self, issue_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/issues/{issue_id}/diagnostics/wakes") or {}

    def get_interactions(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/interactions")
        return result if isinstance(result, list) else result.get("interactions", [])

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def search_issues(self, company_id: str, query: str) -> list[dict[str, Any]]:
        q = urllib.parse.quote(query)
        path = f"/api/companies/{company_id}/issues?q={q}&limit=20"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def create_issue(self, company_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/api/companies/{company_id}/issues", payload)

    def comment(self, issue_id: str, body: str) -> Any:
        return self._request("POST", f"/api/issues/{issue_id}/comments", {"body": body})


def earliest_held_at(wakes: dict[str, Any]) -> Optional[str]:
    """Earliest `deferred_issue_execution` wake's `requestedAt`, or None.

    This is the first moment the hold actually started biting -- every wake
    since has parked the same way, so the earliest one is how long the issue
    has been stuck, not merely the most recent sighting of it.
    """
    events = [e for e in wakes.get("events", []) if e.get("status") == "deferred_issue_execution"]
    if not events:
        return None
    return min(e["requestedAt"] for e in events)


def run_preflight(
    issue: dict[str, Any],
    run: Optional[dict[str, Any]],
    agent: Optional[dict[str, Any]],
    interactions: list[dict[str, Any]],
) -> Preflight:
    """Pre-flight `admitExplicitNativeContinuation`'s admission conditions.

    Every check runs (no short-circuit) so a failing issue reports *all* of
    its blockers, not just the first one found.
    """
    blocker = issue["executionBlocker"]
    hold_agent_id = blocker.get("agentId")
    reasons: list[str] = []

    assignee_matches = issue.get("assigneeAgentId") == hold_agent_id
    if not assignee_matches:
        reasons.append(
            f"assigneeAgentId ({issue.get('assigneeAgentId')}) does not match the hold "
            f"run's agentId ({hold_agent_id}) -- the pairing is broken, a board comment "
            "will bounce, and reassigning it further can never fix this"
        )

    status = issue.get("status")
    if status in ("done", "cancelled"):
        reasons.append(f"status is {status!r} -- closing a held issue is a one-way door")

    adapter_type = (agent or {}).get("adapterType")
    if adapter_type not in CONVERSATION_ADAPTER_TYPES and adapter_type != "paperclip_runner":
        reasons.append(f"assignee adapterType {adapter_type!r} is not a supported continuation adapter")

    run_status = (run or {}).get("status")
    run_finished_at = (run or {}).get("finishedAt")
    if run_status not in TERMINAL_RUN_STATUSES or run_finished_at is None:
        reasons.append(f"hold run is not terminal (status={run_status!r}, finishedAt={run_finished_at!r})")

    native_issue_id = (run or {}).get("nativeIssueId") or ((run or {}).get("contextSnapshot") or {}).get("issueId")
    if native_issue_id != issue.get("id"):
        reasons.append(
            f"hold run's nativeIssueId/contextSnapshot.issueId ({native_issue_id!r}) "
            f"does not point at this issue ({issue.get('id')!r})"
        )

    if any(i.get("status") == "pending" for i in interactions):
        reasons.append("a pending issue-thread interaction exists -- admission would return decision_pending")

    exec_state = issue.get("executionState") or {}
    if exec_state.get("status") == "pending" or exec_state.get("lastDecisionOutcome") == "revision_requested":
        reasons.append("a pending/revision-requested approval exists -- admission would return decision_pending")

    return Preflight(admits=not reasons, reasons=reasons)


def sweep(client: PaperclipClient, company_id: str) -> list[HeldIssue]:
    """Enumerate non-terminal issues and keep the ones with a live hold."""
    held: list[HeldIssue] = []
    for candidate in client.list_candidate_issues(company_id):
        issue = client.get_issue(candidate["id"])
        blocker = issue.get("executionBlocker")
        if not blocker:
            continue

        agent = client.get_agent(issue.get("assigneeAgentId"))
        run = client.get_run(blocker.get("runId"))
        interactions = client.get_interactions(issue["id"])
        wakes = client.get_wakes(issue["id"])
        preflight = run_preflight(issue, run, agent, interactions)

        held.append(
            HeldIssue(
                issue_id=issue["id"],
                identifier=issue.get("identifier", issue["id"]),
                title=issue.get("title", ""),
                status=issue.get("status", ""),
                assignee_agent_id=issue.get("assigneeAgentId"),
                cause=blocker.get("cause", ""),
                hold_run_id=blocker.get("runId", ""),
                hold_agent_id=blocker.get("agentId", ""),
                held_since=earliest_held_at(wakes),
                preflight=preflight,
            )
        )
    return held


def tracker_title(identifier: str) -> str:
    return f"{TRACKER_TITLE_PREFIX} {identifier}"


def format_finding(h: HeldIssue) -> str:
    lines = [
        f"## {h.identifier} is reconciliation-held",
        "",
        f"- Status: `{h.status}`",
        f"- Assignee: `{h.assignee_agent_id}`",
        f"- Cause: `{h.cause}`",
        f"- Hold run: `{h.hold_run_id}` (agent `{h.hold_agent_id}`)",
        f"- Held since: {h.held_since or 'unknown (no deferred_issue_execution wake found)'}",
        "",
        "### Remedy pre-flight",
    ]
    for reason in h.preflight.reasons or ["All preconditions satisfied."]:
        lines.append(f"- {reason}")
    lines.append("")
    if h.preflight.admits:
        lines.append(
            f"**Remedy: a human board user must post a plain comment on {h.identifier} "
            "(body content is irrelevant). No agent comment, status write, or reassignment "
            "can substitute -- the bypass checks `actorType === \"user\"`. Both sanctioned "
            "REST endpoints (`stalled-review-decision`, `recovery-actions/resolve`) return "
            "`403 Board access required` to agents despite the OpenAPI spec marking them "
            "`board_or_agent`.**"
        )
    else:
        lines.append(
            f"**{h.identifier} cannot be admitted by a board comment as currently assigned "
            "-- it will bounce. Do not reassign it; that is the only remaining path to it.**"
        )
    lines.append("")
    lines.append(
        f"Do not close {h.identifier} (`done`/`cancelled` is a one-way door -- no board "
        "comment can ever reopen a held issue once closed) and do not reassign it (admission "
        "requires `assigneeAgentId` to still match the hold run's own `agentId`)."
    )
    lines.append("")
    lines.append(_fingerprint_marker(h.fingerprint()))
    return "\n".join(lines)


def _latest_fingerprint(tracker: dict[str, Any], comments: list[dict[str, Any]]) -> Optional[str]:
    for c in reversed(comments):
        m = _FINGERPRINT_RE.search(c.get("body") or "")
        if m:
            return m.group(1)
    m = _FINGERPRINT_RE.search(tracker.get("description") or "")
    return m.group(1) if m else None


def file_or_update_trackers(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    assignee_agent_id: str,
    held: list[HeldIssue],
) -> list[dict[str, Any]]:
    """Dedupe keyed on the held issue, not the run or the tick.

    An unchanged finding for an issue that already has a tracker produces no
    new comment -- this is the throttle. A new or changed finding files or
    updates exactly one tracker.
    """
    actions: list[dict[str, Any]] = []
    for h in held:
        title = tracker_title(h.identifier)
        existing = [i for i in client.search_issues(company_id, title) if i.get("title") == title]
        existing.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
        body = format_finding(h)

        if not existing:
            created = client.create_issue(
                company_id,
                {
                    "title": title,
                    "description": body,
                    "projectId": project_id,
                    "assigneeAgentId": assignee_agent_id,
                    "priority": "high",
                },
            )
            actions.append({"identifier": h.identifier, "action": "filed", "issueId": created.get("id")})
            continue

        tracker = existing[0]
        comments = client.list_comments(tracker["id"])
        if _latest_fingerprint(tracker, comments) == h.fingerprint():
            actions.append({"identifier": h.identifier, "action": "unchanged", "issueId": tracker["id"]})
            continue

        client.comment(tracker["id"], body)
        actions.append({"identifier": h.identifier, "action": "updated", "issueId": tracker["id"]})

    return actions


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--project-id", help="required with --file-findings")
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument(
        "--assignee-agent-id",
        default="c28db8ef-7f04-4c21-b575-ddee819e050a",
        help="tracker issue owner; default is Cloud, since only the board can clear a hold",
    )
    parser.add_argument(
        "--file-findings",
        action="store_true",
        help="create/update tracker issues; default is dry-run/report-only",
    )
    args = parser.parse_args(argv)

    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")
    if args.file_findings and not args.project_id:
        parser.error("--project-id is required with --file-findings")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )
    held = sweep(client, args.company_id)

    report = [
        {
            "identifier": h.identifier,
            "issueId": h.issue_id,
            "status": h.status,
            "assigneeAgentId": h.assignee_agent_id,
            "cause": h.cause,
            "holdRunId": h.hold_run_id,
            "holdAgentId": h.hold_agent_id,
            "heldSince": h.held_since,
            "admits": h.preflight.admits,
            "reasons": h.preflight.reasons,
        }
        for h in held
    ]
    print(json.dumps(report, indent=2))

    actionable = False
    if held and args.file_findings:
        actions = file_or_update_trackers(
            client, args.company_id, args.project_id, args.assignee_agent_id, held
        )
        print(json.dumps(actions, indent=2))
        actionable = any(a["action"] in ("filed", "updated") for a in actions)
    elif held:
        # Dry-run can't know whether a tracker already restated this finding.
        actionable = True

    print(f"\nACTIONABLE: {'yes' if actionable else 'no'}", file=sys.stderr)
    return 1 if held else 0


if __name__ == "__main__":
    raise SystemExit(main())
