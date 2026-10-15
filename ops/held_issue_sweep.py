"""Visibility-only sweep for reconciliation-held issues (DAN-496, DAN-801).

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

Clearing a hold (making the held issue host a run again) is board-only: the
only thing that does it is one plain comment from a human board user
(`admitExplicitNativeContinuation`'s first line is
`if (input.actorType !== "user" || !actorId) return null`). But DAN-801
found that board comment is not the only remedy, and sometimes not even a
reachable one:

  - held + unfinished work remains + the assignee<->hold-run pairing is
    already broken -> no board comment can ever fix that (reassigning
    further only makes it worse); **re-route** the objective to a fresh
    issue, which is not held, naming the same assignee/parent to carry it.
  - held + the only thing left is a decision or a status write (an approval
    verdict, a close-out) -> the hold blocks runs *bound to* the issue, not
    *writes to* it. The seated participant can clear it with a
    **cross-issue write** performed from a run bound to a different issue
    (this tracker, for instance) -- no board comment needed, and this is
    often cheaper even when a board comment would also work. Never check
    out or bind a run to the held issue itself for this -- a checkout does
    not restore anything and a bound run just parks as
    `deferred_issue_execution` like any other. DAN-660/DAN-800 (2026-10-07)
    is verified evidence this works for an approval *decision*, not only a
    plain status write: a single cross-issue `PATCH {status: "done",
    comment}` closed DAN-660 and its pending approval stage transitioned
    cleanly to `completed`/`approved` with no bounce back to review and no
    inert gate left behind. It is only safe because the remedy is offered
    at all precisely when the issue's `assigneeAgentId` already matches the
    seated participant -- `choose_remedy()` requires `assignee_matches`
    before ever returning this remedy. That is a precondition, not a
    separate thing the performer has to verify; but if it is ever false (an
    *unseated* gate), a status write silently reassigns the issue to the
    approver instead of closing it, destroying the board-comment pairing
    with no error (DAN-466) -- so the rule for anyone applying this pattern
    by hand rather than from this remedy's own routing is: check
    `executionPolicy` + current `assigneeAgentId` first, and if they do not
    already match the intended writer, do not write `status` at all.
  - held + it genuinely needs to host a run of its own + the board-comment
    preconditions are jointly satisfiable -> **board comment**, as before.

The two board-comment preconditions that matter here are (A) no pending
interaction/approval stands in the way, and (B) the issue's `assigneeAgentId`
still equals the hold run's own `agentId`. These can be jointly
unsatisfiable rather than independently blocked: on DAN-660, (B) held only
*because* a pending approval stage had seated the hold agent as assignee --
resolving that approval to satisfy (A) hands the issue back to
`returnAssignee`, which breaks (B). Checking (A) and (B) separately (as the
original DAN-496 cut of this script did) reports "all preconditions
satisfied" for an issue with no board path at all. This cut evaluates them
jointly and routes to a cross-issue write instead.

Closing a held issue is a one-way door: no board comment can ever reopen it
once `status` is `done`/`cancelled`. That makes the issue's own status
orthogonal to whether the `executionBlocker` field is still populated --
once terminal, the hold is *sealed*, not *live*: nothing ever tries to wake
a closed issue, so a stale blocker on it costs nothing and needs no remedy.
DAN-466 went through this exact shape (closed with its `executionBlocker`
still set) and is the regression fixture for it.

A previously-filed tracker must not stay closed while its subject's hold is
still live and non-terminal -- that is the DAN-498/DAN-466 incident this
script was supposed to prevent and didn't: DAN-498 ("Held issue: DAN-466")
was closed while DAN-466 sat held for three more days. This cut reopens a
closed tracker whenever its subject is still found live-held on a later
sweep, regardless of whether the finding's content changed, and reports the
hold's age so it is something that visibly escalates rather than something
that silently vanishes behind a closed ticket.

The one exception is deliberate acceptance. A closer who knows the hold is
still there and is leaving it on purpose writes a line `accept-hold:
<reason>` in the tracker's closing comment (or description). Acceptance is
never inferred from free text. While it stands the sweep does not reopen; it
reports `action: accepted_hold` with the reason so it stays visible in every
report. It expires, and the tracker is reopened as normal, when either the
hold's identity (hold run id + `executionBlocker.cause`, as last recorded on
the tracker) changes, or 14 days pass since the closure. Anything it cannot
read counts as expired.

This script never writes to the held issue itself through the sweep's own
run -- creating or updating a separate tracker issue is as far as it goes.
Actually clearing a hold (making the issue host a run again) is still
board-only by design; this only makes holds visible, routes the remedy to
whoever can actually act on it, and refuses to let a tracker go quiet while
the thing it reported is still true.
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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

NON_TERMINAL_STATUSES = "todo,in_progress,in_review,blocked"
TERMINAL_ISSUE_STATUSES = {"done", "cancelled"}

# Tracker issues default here only for the "board_comment" and "sealed"
# remedies, since only the board can act on the former and nobody needs to
# act on the latter. Every other remedy routes to whoever can actually do
# the work -- see choose_remedy().
CLOUD_AGENT_ID = "c28db8ef-7f04-4c21-b575-ddee819e050a"

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

# Deliberate hold acceptance (DAN-801, Cloud's 2026-10-15 decision). A closed
# tracker is normally reopened because closing it means someone *believed*
# the hold was gone. A closer who knows it is still there and is leaving it
# on purpose says so with a line `accept-hold: <reason>` -- never inferred
# from free text. It expires when the hold's identity changes or after
# ACCEPT_HOLD_TTL, so acceptance cannot quietly outlive the decision.
_ACCEPT_HOLD_RE = re.compile(r"^[ \t]*accept-hold:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
_HOLD_RUN_RE = re.compile(r"^- Hold run: `([^`]+)`", re.MULTILINE)
_CAUSE_RE = re.compile(r"^- Cause: `([^`]+)`", re.MULTILINE)
ACCEPT_HOLD_TTL = timedelta(days=14)


def _fingerprint_marker(fingerprint: str) -> str:
    return f"<!-- held-sweep-fingerprint: {fingerprint} -->"


@dataclass
class Preflight:
    """The admission pre-flight for one held issue's board-comment path.

    `assignee_matches`/`decision_pending`/`conflict`/`seated_participant_agent_id`
    are exposed (not just folded into `reasons`) because `choose_remedy`
    needs to tell *why* admission fails, not just that it does, to route to
    the right one of the three remedies.
    """

    admits: bool
    reasons: list[str] = field(default_factory=list)
    assignee_matches: bool = True
    decision_pending: bool = False
    conflict: bool = False
    seated_participant_agent_id: Optional[str] = None


@dataclass
class Remedy:
    """Which of the three remedies applies, and who should perform it."""

    kind: str  # "board_comment" | "cross_issue_write" | "re_route" | "sealed"
    owner_agent_id: Optional[str]
    detail: str


@dataclass
class HeldIssue:
    issue_id: str
    identifier: str
    title: str
    status: str
    assignee_agent_id: Optional[str]
    parent_id: Optional[str]
    cause: str
    hold_run_id: str
    hold_agent_id: str
    held_since: Optional[str]
    preflight: Preflight
    remedy: Remedy

    def fingerprint(self) -> str:
        basis = "|".join(
            [
                self.status,
                str(self.assignee_agent_id),
                self.cause,
                self.hold_run_id,
                str(self.preflight.admits),
                "|".join(sorted(self.preflight.reasons)),
                self.remedy.kind,
                str(self.remedy.owner_agent_id),
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

    def update_issue(self, issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/api/issues/{issue_id}", payload)


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


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def hold_age_human(held_since: Optional[str], now: datetime) -> str:
    """Render the hold's age so it is a number that visibly escalates.

    Deliberately coarse (minutes/hours/days, not seconds) -- this is read by
    a human deciding whether to worry, not logged for machine parsing.
    """
    if not held_since:
        return "unknown (no deferred_issue_execution wake found)"
    delta = now - _parse_iso(held_since)
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def run_preflight(
    issue: dict[str, Any],
    run: Optional[dict[str, Any]],
    agent: Optional[dict[str, Any]],
    interactions: list[dict[str, Any]],
) -> Preflight:
    """Pre-flight `admitExplicitNativeContinuation`'s admission conditions.

    Every check runs (no short-circuit) so a failing issue reports *all* of
    its blockers, not just the first one found. (A) is the pending-decision
    checks below, (B) is the assignee-matches check -- the DAN-801 addition
    is evaluating whether satisfying (A) would break (B), not just checking
    each in isolation.
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
    if status in TERMINAL_ISSUE_STATUSES:
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

    pending_interaction = any(i.get("status") == "pending" for i in interactions)
    if pending_interaction:
        reasons.append("a pending issue-thread interaction exists -- admission would return decision_pending")

    exec_state = issue.get("executionState") or {}
    pending_approval = (
        exec_state.get("status") == "pending" or exec_state.get("lastDecisionOutcome") == "revision_requested"
    )
    if pending_approval:
        reasons.append("a pending/revision-requested approval exists -- admission would return decision_pending")

    participant_agent_id = (exec_state.get("currentParticipant") or {}).get("agentId")
    return_assignee_agent_id = (exec_state.get("returnAssignee") or {}).get("agentId")

    # (A)/(B) joint evaluation: (B) can hold *only because* a pending
    # approval stage seated the hold agent as assignee. Satisfying (A) by
    # resolving that approval hands the issue to returnAssignee, which
    # breaks (B) -- so there is no sequence of actions that satisfies both
    # at once, not merely a blocker that clears on its own.
    conflict = bool(
        pending_approval
        and assignee_matches
        and participant_agent_id == hold_agent_id
        and return_assignee_agent_id is not None
        and return_assignee_agent_id != hold_agent_id
    )
    if conflict:
        reasons.append(
            "(A) and (B) are jointly unsatisfiable: resolving the pending approval would hand "
            f"the issue back to returnAssignee ({return_assignee_agent_id}), breaking the "
            "assigneeAgentId<->hold-run pairing that currently satisfies (B) -- there is no "
            "board-comment path here, not merely a blocked one"
        )

    decision_pending = pending_interaction or pending_approval

    return Preflight(
        admits=not reasons,
        reasons=reasons,
        assignee_matches=assignee_matches,
        decision_pending=decision_pending,
        conflict=conflict,
        seated_participant_agent_id=participant_agent_id,
    )


def choose_remedy(issue: dict[str, Any], preflight: Preflight, hold_agent_id: Optional[str]) -> Remedy:
    """Pick one of the three standing remedies from the issue's actual state.

    Order matters: terminal status (sealed) is checked before anything
    else, since nothing else about the issue matters once it can never host
    another wake. The conflict check comes before the plain
    decision-pending check because a conflict is the harder claim (no board
    path at all, not just "none right now") and should be reported as such.
    """
    status = issue.get("status")
    if status in TERMINAL_ISSUE_STATUSES:
        return Remedy(
            kind="sealed",
            owner_agent_id=None,
            detail=(
                f"status is {status!r}: the hold is sealed, not live. Nothing ever tries to "
                "wake a closed issue, so the stale executionBlocker costs nothing and no "
                "remedy is needed or possible."
            ),
        )

    if preflight.admits:
        return Remedy(
            kind="board_comment",
            owner_agent_id=None,
            detail="all board-comment admission preconditions are satisfied.",
        )

    if preflight.conflict:
        return Remedy(
            kind="cross_issue_write",
            owner_agent_id=preflight.seated_participant_agent_id,
            detail=(
                "the board-comment preconditions (A)/(B) are jointly unsatisfiable, so there "
                "is no board path at all -- only a decision remains. This reads like the "
                "hardest case but is actually the cheapest: the very thing that kills the "
                "board-comment path (a pending approval seated the hold agent as assignee) is "
                "what makes the cross-issue write legal, since it guarantees assigneeAgentId "
                "already matches the seated participant. The seated participant should resolve "
                "it with a cross-issue write (a comment/PATCH issued from a run bound to a "
                "different issue; the hold blocks runs bound to this issue, not writes to it) "
                "-- do not escalate this as 'no remedy available'."
            ),
        )

    if preflight.decision_pending and preflight.assignee_matches:
        return Remedy(
            kind="cross_issue_write",
            owner_agent_id=preflight.seated_participant_agent_id or hold_agent_id,
            detail=(
                "a pending approval or issue-thread interaction is the only thing standing in "
                "the way and the assignee<->hold-run pairing is still intact -- only a "
                "decision/status write remains, which can be done as a cross-issue write "
                "without a board comment."
            ),
        )

    if not preflight.assignee_matches:
        return Remedy(
            kind="re_route",
            owner_agent_id=issue.get("assigneeAgentId"),
            detail=(
                "the assignee<->hold-run pairing is already broken, which a board comment can "
                "never repair, and the remaining reasons indicate unfinished work rather than "
                "a pending decision -- re-route the objective to a fresh issue (not held) "
                "carrying the same assignee and parent; do not touch this one."
            ),
        )

    return Remedy(
        kind="re_route",
        owner_agent_id=issue.get("assigneeAgentId"),
        detail=(
            "admission is blocked for a reason other than assignee pairing or a pending "
            "decision (see the preflight reasons above) -- re-route the objective rather than "
            "wait on a path that may never open."
        ),
    )


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
        remedy = choose_remedy(issue, preflight, blocker.get("agentId"))

        held.append(
            HeldIssue(
                issue_id=issue["id"],
                identifier=issue.get("identifier", issue["id"]),
                title=issue.get("title", ""),
                status=issue.get("status", ""),
                assignee_agent_id=issue.get("assigneeAgentId"),
                parent_id=issue.get("parentId"),
                cause=blocker.get("cause", ""),
                hold_run_id=blocker.get("runId", ""),
                hold_agent_id=blocker.get("agentId", ""),
                held_since=earliest_held_at(wakes),
                preflight=preflight,
                remedy=remedy,
            )
        )
    return held


def tracker_title(identifier: str) -> str:
    return f"{TRACKER_TITLE_PREFIX} {identifier}"


def _remedy_markdown(h: HeldIssue) -> str:
    r = h.remedy
    if r.kind == "sealed":
        return (
            f"{h.identifier} is already terminal (`{h.status}`). {r.detail} No action is "
            "possible or needed here."
        )
    if r.kind == "board_comment":
        return (
            f"**A human board user must post a plain comment on {h.identifier} (body content "
            "is irrelevant). No agent comment, status write, or reassignment can substitute -- "
            'the bypass checks `actorType === "user"`. Both sanctioned REST endpoints '
            "(`stalled-review-decision`, `recovery-actions/resolve`) return `403 Board access "
            "required` to agents despite the OpenAPI spec marking them `board_or_agent`.**"
        )
    if r.kind == "cross_issue_write":
        owner = f"`{r.owner_agent_id}`" if r.owner_agent_id else "the seated participant"
        return (
            f"**Only a decision/status write remains on {h.identifier}; a board comment is "
            f"not the right remedy here. {owner} should resolve it with a cross-issue write -- "
            f"a comment/PATCH issued from a run bound to a *different* issue (this tracker, "
            f"for instance). Never check out or bind a run to {h.identifier} itself: that does "
            f"not restore anything, a bound run just parks `deferred_issue_execution` same as "
            f"any other wake, and if anything you read here suggests *working* {h.identifier}, "
            f"that is wrong. The hold blocks runs bound to {h.identifier}; it does not block "
            f"writes to it. This is verified for approval decisions, not just plain status "
            f"writes -- DAN-660 was closed via exactly this pattern (one cross-issue `PATCH "
            f"{{status: \"done\"}}` from DAN-800's run) and its pending approval stage "
            f"transitioned cleanly, with no bounce back to review. It is only safe here because "
            f"{h.identifier}'s `assigneeAgentId` already matches the seated participant -- that "
            f"is a precondition of this remedy being offered at all. If you are applying this "
            f"pattern anywhere `assigneeAgentId` does not already match the writer, do not write "
            f"`status`: on an *unseated* gate a status write silently reassigns the issue to the "
            f"approver instead of closing it, with no error (DAN-466).**\n\n{r.detail}"
        )
    if r.kind == "re_route":
        owner = f"`{r.owner_agent_id}`" if r.owner_agent_id else "its current assignee"
        parent = f", parent `{h.parent_id}`" if h.parent_id else ""
        return (
            f"**{h.identifier} cannot be admitted by a board comment as currently assigned -- "
            f"it will bounce, and reassigning {h.identifier} itself can never fix that. "
            f"Re-route the objective to a fresh issue instead: assignee {owner}{parent}, linked "
            f"back to {h.identifier} for the trail.**\n\n{r.detail}"
        )
    raise AssertionError(f"unhandled remedy kind: {r.kind!r}")  # pragma: no cover


def format_finding(h: HeldIssue, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [
        f"## {h.identifier} is reconciliation-held",
        "",
        f"- Status: `{h.status}`",
        f"- Assignee: `{h.assignee_agent_id}`",
        f"- Cause: `{h.cause}`",
        f"- Hold run: `{h.hold_run_id}` (agent `{h.hold_agent_id}`)",
        f"- Held since: {h.held_since or 'unknown (no deferred_issue_execution wake found)'}",
        f"- Held for: {hold_age_human(h.held_since, now)}",
        "",
        "### Remedy pre-flight",
    ]
    for reason in h.preflight.reasons or ["All preconditions satisfied."]:
        lines.append(f"- {reason}")
    lines.append("")
    lines.append(f"### Remedy: {h.remedy.kind}")
    lines.append("")
    lines.append(_remedy_markdown(h))
    if h.remedy.kind != "sealed":
        lines.append("")
        lines.append(
            f"Do not close {h.identifier} (`done`/`cancelled` is a one-way door -- no board "
            "comment can ever reopen a held issue once closed) and do not reassign it "
            f"(admission to {h.identifier} itself requires `assigneeAgentId` to still match the "
            "hold run's own `agentId`)."
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


def _hold_identity(body: str) -> Optional[tuple[str, str]]:
    """(hold run id, cause) as written by `format_finding`, or None."""
    run, cause = _HOLD_RUN_RE.search(body), _CAUSE_RE.search(body)
    return (run.group(1), cause.group(1)) if run and cause else None


def _tracker_hold_identity(tracker: dict[str, Any], comments: list[dict[str, Any]]) -> Optional[tuple[str, str]]:
    """The hold identity the sweep last recorded on this tracker.

    Parsed from the newest sweep-authored body (the one carrying a
    fingerprint marker) rather than from a dedicated marker, so trackers
    filed before accept-hold existed still have an identity.
    """
    for body in [c.get("body") or "" for c in reversed(comments)] + [tracker.get("description") or ""]:
        if _FINGERPRINT_RE.search(body):
            return _hold_identity(body)
    return None


def _accept_hold_reason(tracker: dict[str, Any], comments: list[dict[str, Any]]) -> Optional[str]:
    """The newest `accept-hold:` reason written since the sweep last spoke.

    Scans comments newest-first and stops at the first sweep-authored one
    (fingerprint marker), so a marker only covers the closure that followed
    the latest sweep report and a stale one cannot carry over a reopen.
    The description counts as the oldest source.
    """
    for c in reversed(comments):
        body = c.get("body") or ""
        if _FINGERPRINT_RE.search(body):
            return None
        found = _ACCEPT_HOLD_RE.findall(body)
        if found:
            return found[-1]
    # The description is the sweep's own body for a never-updated tracker, so
    # a marker there was added by hand and is checked after the comments.
    found = _ACCEPT_HOLD_RE.findall(tracker.get("description") or "")
    return found[-1] if found else None


def _closed_at(tracker: dict[str, Any]) -> Optional[datetime]:
    for key in ("completedAt", "cancelledAt", "updatedAt"):
        if tracker.get(key):
            try:
                return _parse_iso(tracker[key])
            except ValueError:
                return None
    return None


def accepted_hold_reason(
    h: HeldIssue,
    tracker: dict[str, Any],
    comments: list[dict[str, Any]],
    now: datetime,
) -> Optional[str]:
    """The reason a closed tracker's hold is still deliberately accepted, else None.

    Fails safe: anything unreadable (no identity recorded, no closure time)
    counts as expired, so the tracker is reopened as it would be with no marker.
    """
    reason = _accept_hold_reason(tracker, comments)
    if reason is None:
        return None
    if _tracker_hold_identity(tracker, comments) != (h.hold_run_id, h.cause):
        return None
    closed_at = _closed_at(tracker)
    if closed_at is None or now - closed_at >= ACCEPT_HOLD_TTL:
        return None
    return reason


def _tracker_assignee(h: HeldIssue, board_assignee_agent_id: str) -> str:
    """Route by remedy owner (DAN-801 problem 3), not unconditionally to the board.

    Only `board_comment` genuinely needs the board; `sealed` needs nobody but
    still has to carry a valid assignee, so both fall back to the same
    board-facing id. Every other remedy has a named owner who can actually
    act -- the seated participant for a cross-issue write, the issue's own
    current assignee for a re-route.
    """
    if h.remedy.kind in ("board_comment", "sealed"):
        return board_assignee_agent_id
    return h.remedy.owner_agent_id or board_assignee_agent_id


def file_or_update_trackers(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    board_assignee_agent_id: str,
    held: list[HeldIssue],
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Dedupe keyed on the held issue, not the run or the tick.

    An unchanged finding for an issue that already has an *open* tracker
    produces no new comment -- this is the throttle. A tracker that has
    gone `done`/`cancelled` while its subject is still live-held is always
    reopened, regardless of whether the finding's content changed, because
    that is exactly the DAN-498/DAN-466 shape this sweep is supposed to
    prevent: a closed tracker reporting a hold that is still true. The one
    exception is a deliberate `accept-hold: <reason>` line from the closer
    (see `accepted_hold_reason`): it reports `accepted_hold` instead, until
    the hold's identity changes or 14 days pass since the closure.
    """
    now = now or datetime.now(timezone.utc)
    actions: list[dict[str, Any]] = []
    for h in held:
        title = tracker_title(h.identifier)
        existing = [i for i in client.search_issues(company_id, title) if i.get("title") == title]
        existing.sort(key=lambda i: i.get("createdAt", ""), reverse=True)
        body = format_finding(h, now=now)
        assignee = _tracker_assignee(h, board_assignee_agent_id)

        if not existing:
            created = client.create_issue(
                company_id,
                {
                    "title": title,
                    "description": body,
                    "projectId": project_id,
                    "assigneeAgentId": assignee,
                    "priority": "high",
                },
            )
            actions.append(
                {
                    "identifier": h.identifier,
                    "action": "filed",
                    "issueId": created.get("id"),
                    "remedy": h.remedy.kind,
                    "assignedTo": assignee,
                }
            )
            continue

        tracker = existing[0]
        comments = client.list_comments(tracker["id"])
        same_fingerprint = _latest_fingerprint(tracker, comments) == h.fingerprint()
        was_closed = tracker.get("status") in TERMINAL_ISSUE_STATUSES

        if was_closed:
            accepted = accepted_hold_reason(h, tracker, comments, now)
            if accepted is not None:
                actions.append(
                    {
                        "identifier": h.identifier,
                        "action": "accepted_hold",
                        "issueId": tracker["id"],
                        "reason": accepted,
                        "heldFor": hold_age_human(h.held_since, now),
                    }
                )
                continue

        if same_fingerprint and not was_closed:
            actions.append({"identifier": h.identifier, "action": "unchanged", "issueId": tracker["id"]})
            continue

        patch: dict[str, Any] = {"comment": body, "assigneeAgentId": assignee}
        if was_closed:
            patch["status"] = "todo"
        client.update_issue(tracker["id"], patch)
        actions.append(
            {
                "identifier": h.identifier,
                "action": "reopened" if was_closed else "updated",
                "issueId": tracker["id"],
                "remedy": h.remedy.kind,
                "assignedTo": assignee,
            }
        )

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
        "--board-assignee-agent-id",
        default=CLOUD_AGENT_ID,
        help=(
            "fallback tracker owner for the board_comment and sealed remedies; default is "
            "Cloud. Every other remedy routes to the owner choose_remedy() names (the seated "
            "participant or the held issue's own assignee), not to this flag."
        ),
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
            "parentId": h.parent_id,
            "cause": h.cause,
            "holdRunId": h.hold_run_id,
            "holdAgentId": h.hold_agent_id,
            "heldSince": h.held_since,
            "heldFor": hold_age_human(h.held_since, datetime.now(timezone.utc)),
            "admits": h.preflight.admits,
            "reasons": h.preflight.reasons,
            "remedy": h.remedy.kind,
            "remedyOwnerAgentId": h.remedy.owner_agent_id,
        }
        for h in held
    ]
    print(json.dumps(report, indent=2))

    actionable = False
    if held and args.file_findings:
        actions = file_or_update_trackers(
            client, args.company_id, args.project_id, args.board_assignee_agent_id, held
        )
        print(json.dumps(actions, indent=2))
        actionable = any(a["action"] in ("filed", "updated", "reopened") for a in actions)
    elif held:
        # Dry-run can't know whether a tracker already restated this finding.
        actionable = True

    print(f"\nACTIONABLE: {'yes' if actionable else 'no'}", file=sys.stderr)
    return 1 if held else 0


if __name__ == "__main__":
    raise SystemExit(main())
