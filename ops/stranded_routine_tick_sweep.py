"""Repeatable disposal for stranded routine ticks (DAN-781, re-route of DAN-466).

A routine (e.g. the OAuth credential-expiry watchdog, the seat-health
watchdog) fires on a schedule and files one issue per tick. When the ACP
runtime itself is down -- the 2026-10-07 06:13-09:55Z credential outage that
produced this script's first run -- every tick still gets filed, but the
agent assigned to it can never actually start: the only comments the issue
ever receives are the harness's own terminal-failure notices
(`ACP agent reported a terminal access failure.` /
`... terminal limit failure.`), not real detector output. The tick sits
open forever looking like live work.

DAN-466 did this once by hand and was then wedged on a reconciliation hold
before it could be re-run (see that issue, and AGENTS.md's "Wedged issues"
section); DAN-781 re-routes around the hold and asks for this to be a
script instead of a one-off, because three days later the pile had grown
from 50 to 43 *fresh* ones from a second outage. Re-running this after any
future outage is the point.

A tick only counts as stranded if *every* comment on it matches the known
harness-failure boilerplate (or it has no comments at all). A tick with any
other comment is left alone and reported separately -- it may have produced
real findings, and disposal must be verified per-group, not assumed from
the routine title alone (see AGENTS.md's QA lens #2: absence-of-error is
not a pass, and the mirror of it here is that presence-of-a-comment is not
proof of output either, so the check is content-based, not count-based).

Default is dry-run/report-only. `--apply` cancels, and only cancels ticks
classified `stranded`; anything classified `has_output` is never touched by
this script. `--limit` caps how many cancellations one invocation performs,
to stay under the per-run cross-issue write cap of 20 comments+PATCHes
combined (see AGENTS.md) -- run again for the residue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

NON_TERMINAL_STATUSES = "todo,in_progress,backlog,blocked"

# The harness's own terminal-failure notices, verified against the live
# board on 2026-10-07 as the *only* comment bodies a never-executed tick
# from the 06:13-09:55Z outage ever received.
_BOILERPLATE_RE = re.compile(
    r"^ACP agent reported a terminal (access|limit) failure\.$"
)


def is_boilerplate_comment(body: str) -> bool:
    return bool(_BOILERPLATE_RE.match((body or "").strip()))


@dataclass
class StrandedCandidate:
    issue_id: str
    identifier: str
    title: str
    status: str
    created_at: str
    assignee_agent_id: Optional[str]
    comment_count: int
    verdict: str  # "stranded" or "has_output"
    reason: str


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip API.

    Kept separate from `sweep`/`dispose` so tests can swap in a fake that
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
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None

    def list_candidate_issues(self, company_id: str) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/issues?status={NON_TERMINAL_STATUSES}&limit=400"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def cancel(self, issue_id: str, comment: str) -> Any:
        return self._request(
            "PATCH", f"/api/issues/{issue_id}", {"status": "cancelled", "comment": comment}
        )


def classify(issue: dict[str, Any], comments: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (verdict, reason) for one candidate tick.

    `stranded` requires every comment (zero is fine) to be harness
    boilerplate -- any other comment body flips the verdict to `has_output`
    so real findings are never silently cancelled.
    """
    if not comments:
        return "stranded", "no comments -- tick never executed"

    non_boilerplate = [c for c in comments if not is_boilerplate_comment(c.get("body", ""))]
    if non_boilerplate:
        return (
            "has_output",
            f"{len(non_boilerplate)} of {len(comments)} comment(s) are not harness-failure "
            "boilerplate -- possible real output, left for manual review",
        )
    return "stranded", f"{len(comments)} comment(s), all harness terminal-failure boilerplate"


def sweep(
    client: PaperclipClient,
    company_id: str,
    routine_titles: list[str],
    cutoff: Optional[str] = None,
) -> list[StrandedCandidate]:
    """Enumerate open issues matching `routine_titles`, classify each.

    `cutoff` (ISO 8601) restricts to issues created at or before it, e.g.
    the moment an outage was declared over -- ticks filed after that point
    may still be live work in progress rather than stranded residue.
    """
    titles = set(routine_titles)
    cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) if cutoff else None

    candidates: list[StrandedCandidate] = []
    for issue in client.list_candidate_issues(company_id):
        if issue.get("title") not in titles:
            continue
        created_at = issue.get("createdAt", "")
        if cutoff_dt is not None and created_at:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if created_dt > cutoff_dt:
                continue

        comments = client.list_comments(issue["id"])
        verdict, reason = classify(issue, comments)
        candidates.append(
            StrandedCandidate(
                issue_id=issue["id"],
                identifier=issue.get("identifier", issue["id"]),
                title=issue.get("title", ""),
                status=issue.get("status", ""),
                created_at=created_at,
                assignee_agent_id=issue.get("assigneeAgentId"),
                comment_count=len(comments),
                verdict=verdict,
                reason=reason,
            )
        )
    return candidates


def dispose(
    client: PaperclipClient,
    candidates: list[StrandedCandidate],
    incident: str,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Cancel `stranded` candidates (never `has_output`), up to `limit`.

    Returns one action record per candidate considered, so callers can
    report exactly what was cancelled, what was skipped as having real
    output, and what was deferred to a later run by the limit.
    """
    actions: list[dict[str, Any]] = []
    cancelled = 0
    for c in candidates:
        if c.verdict != "stranded":
            actions.append({"identifier": c.identifier, "action": "skipped_has_output"})
            continue
        if limit is not None and cancelled >= limit:
            actions.append({"identifier": c.identifier, "action": "deferred_limit"})
            continue

        comment = (
            f"Cancelled by the stranded routine-tick sweep ({incident}). "
            f"This tick never executed -- {c.reason}. See DAN-781 for the disposal "
            "script and rationale; re-run it after any future outage."
        )
        client.cancel(c.issue_id, comment)
        actions.append({"identifier": c.identifier, "action": "cancelled"})
        cancelled += 1
    return actions


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument(
        "--routine-title",
        action="append",
        required=True,
        dest="routine_titles",
        help="exact issue title filed by a routine tick; repeatable",
    )
    parser.add_argument(
        "--cutoff",
        help="ISO 8601 timestamp; only consider issues created at or before it",
    )
    parser.add_argument(
        "--incident",
        help="short incident label recorded in each cancellation comment; required with --apply",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="cap the number of cancellations this invocation performs "
        "(stay under the per-run cross-issue write cap); default is unlimited",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="cancel stranded ticks; default is dry-run/report-only",
    )
    args = parser.parse_args(argv)

    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")
    if args.apply and not args.incident:
        parser.error("--incident is required with --apply")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )
    candidates = sweep(client, args.company_id, args.routine_titles, cutoff=args.cutoff)

    report = [
        {
            "identifier": c.identifier,
            "issueId": c.issue_id,
            "title": c.title,
            "status": c.status,
            "createdAt": c.created_at,
            "assigneeAgentId": c.assignee_agent_id,
            "commentCount": c.comment_count,
            "verdict": c.verdict,
            "reason": c.reason,
        }
        for c in candidates
    ]
    print(json.dumps(report, indent=2))

    stranded = [c for c in candidates if c.verdict == "stranded"]
    has_output = [c for c in candidates if c.verdict == "has_output"]
    print(
        f"\n{len(candidates)} candidate(s): {len(stranded)} stranded, "
        f"{len(has_output)} with possible real output (never touched)",
        file=sys.stderr,
    )

    if args.apply:
        actions = dispose(client, candidates, args.incident, limit=args.limit)
        print(json.dumps(actions, indent=2))
        n_cancelled = sum(1 for a in actions if a["action"] == "cancelled")
        n_deferred = sum(1 for a in actions if a["action"] == "deferred_limit")
        print(
            f"\n{n_cancelled} cancelled, {n_deferred} deferred by --limit, "
            f"{len(has_output)} left as possible real output",
            file=sys.stderr,
        )
    return 0 if not stranded or args.apply else 1


if __name__ == "__main__":
    raise SystemExit(main())
