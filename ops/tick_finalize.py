#!/usr/bin/env python3
"""Tick finalizer for routines moving to report-on-exception -- DAN-289.

DAN-289's complaint is not about the two watchdog arms' own finding logic
(seat_watchdog.py and stranded_review.py are already exception-only: they
dedupe/update rather than re-announce). It is about the routine's OWN
per-tick execution issue -- the platform creates one of these every time the
schedule fires, and up to now this run's own issue got marked `done` with a
"nothing found" comment on every single clean tick, 16 times in 7 hours for
the narrow-arm seat-health routine alone. That is 16 rows a human has to
scroll past to find the one tick that mattered.

This script decides what happens to THIS tick's own execution issue, given
whether either arm found anything:

  - Clean (no findings anywhere this tick): append one timestamped line to a
    single persistent "rolling log" issue (found by a stable marker in its
    title, created once, left open indefinitely -- never marked done by this
    script), then mark THIS tick's own issue `cancelled` (not `done`) with a
    one-line pointer. `cancelled`, not `done`, so a clean tick stops
    inflating completion counts -- that metric is literally what tipped
    DAN-289 off ("14 of Dante's 20 completions" were empty watchdog ticks).

  - Exception (something was found): mark THIS tick's own issue `done`. Real
    work happened this tick -- a per-seat or per-review finding was filed or
    updated elsewhere (that filing/update is each arm's own job, not this
    script's). This script only applies the tick-level status.

DAN-385 requirement 6: "something was found" is not by itself grounds for
`done` -- a finding that is a pure restatement of an already-open,
already-reported finding (each arm's own `ACTIONABLE: no` signal) belongs
in the Clean path too, so it does not also mint a fresh execution issue for
information that is already on record elsewhere. Callers pass `--clean
--note "..."` for that case so the rolling log still records the tick and
can say *why* (restated vs. truly empty) without a status flip either way.

This script never changes any OTHER issue's status and never escalates --
same visibility-only posture as both arms. It is deliberately agent/routine
agnostic (no seat-health- or approval-sweep-specific logic) so the
Default-approver gate sweep routine can adopt the identical pattern.

Usage:
  PAPERCLIP_API_KEY=... PAPERCLIP_API_URL=... PAPERCLIP_COMPANY_ID=... \
      python3 ops/tick_finalize.py --this-issue-id ID --project-id PID \
      --assignee-agent-id AID --rolling-log-marker "Seat-health watchdog -- rolling log" \
      --routine-label "Seat-health watchdog (narrow arm)" \
      (--clean | --exception-summary "...") [--dry-run]

Exit code is always 0.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote


def api_get(path):
    base = os.environ["PAPERCLIP_API_URL"].rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    req = urllib.request.Request(
        base + path,
        headers={"Authorization": "Bearer " + os.environ["PAPERCLIP_API_KEY"]},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _api_write(path, body, method):
    base = os.environ["PAPERCLIP_API_URL"].rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + os.environ["PAPERCLIP_API_KEY"],
            "Content-Type": "application/json",
            "X-Paperclip-Run-Id": os.environ.get("PAPERCLIP_RUN_ID", ""),
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def api_post(path, body):
    return _api_write(path, body, "POST")


def api_patch(path, body):
    return _api_write(path, body, "PATCH")


def is_rolling_log(issue, marker):
    """Matched on an exact marker substring in the title -- deliberately not
    fuzzy, so an unrelated issue that happens to mention the routine's name
    in passing can never be mistaken for the rolling log."""
    return marker in (issue.get("title") or "")


def find_rolling_log(company_id, marker, assignee_agent_id, api_get_fn=api_get):
    """DAN-720: do NOT filter by status. The rolling log is meant to stay
    `in_progress` forever, but Paperclip's own disposition-handoff check has
    been observed force-flipping it to `blocked` within seconds of any PATCH
    that sets it back to `in_progress` -- "Paperclip could not resolve this
    issue's missing disposition automatically" -- because a permanent,
    never-`done` log issue with no blocker/interaction/monitor doesn't look
    like a live continuation path to that heuristic. A status-filtered
    lookup intermittently misses the real rolling log and mints a duplicate
    (this happened for real on DAN-720: DAN-391 flipped to `blocked` and the
    very next clean tick created DAN-726 as a second rolling log). Matching
    on the marker+assignee alone, across every status, is what the marker
    was already designed to make safe (`is_rolling_log`'s docstring: "an
    unrelated issue ... can never be mistaken for the rolling log").
    If more than one match turns up (e.g. a duplicate already minted before
    this fix landed), prefer the oldest -- that is the original log all the
    historical entries live on, never a fresh duplicate.

    DAN-789: the plain `assigneeAgentId&limit=100` listing used here was
    itself unreliable once more than ~100 issues were assigned to this
    agent -- the endpoint's default order is not createdAt/updatedAt
    monotonic, so a dormant old issue (the real rolling log) can fall
    outside the window while still inside the overall date range, and the
    very bug this function's docstring describes (DAN-720: DAN-391 missed,
    DAN-726 minted as a duplicate) reproduced again for real on 2026-10-07
    (DAN-391 missed, DAN-792 minted). Adding `q=<marker>` scopes the search
    to title-relevance instead of recency, which reliably surfaces a
    years-old marker-matching issue regardless of how many newer issues
    exist; `assigneeAgentId` and the `is_rolling_log` marker check below
    still guard against a false-positive relevance match."""
    listing = api_get_fn(
        f"/api/companies/{company_id}/issues"
        f"?q={quote(marker)}&assigneeAgentId={assignee_agent_id}&limit=100"
    )
    items = listing if isinstance(listing, list) else listing.get("issues", listing.get("data", []))
    matches = [item for item in items if is_rolling_log(item, marker)]
    if not matches:
        return None
    matches.sort(key=lambda item: item.get("createdAt") or "")
    return matches[0]


def ensure_rolling_log(company_id, marker, routine_label, project_id, assignee_agent_id,
                        api_get_fn=api_get, api_post_fn=api_post):
    existing = find_rolling_log(company_id, marker, assignee_agent_id, api_get_fn)
    if existing is not None:
        return existing, False
    created = api_post_fn(
        f"/api/companies/{company_id}/issues",
        {
            "title": marker,
            "description": (
                f"Rolling status log for **{routine_label}** (report-on-exception, DAN-289). "
                "Clean ticks append a line here instead of minting a new issue. "
                "This issue is intentionally left open indefinitely -- it is not a stuck task, "
                "and its own age is not a finding."
            ),
            "projectId": project_id,
            "assigneeAgentId": assignee_agent_id,
            "priority": "low",
            "status": "in_progress",
        },
    )
    return created, True


def clean_note(now, detail=None):
    """DAN-385 requirement 6: `--clean` now also covers a tick whose only
    findings are restatements of an already-open/already-reported finding
    (no new execution issue warranted, same as a truly empty tick) -- an
    optional `detail` overrides the default text so the rolling log can
    still distinguish the two cases."""
    return f"{now.isoformat()}: {detail or 'clean -- no qualifying findings this tick.'}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--this-issue-id", required=True)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--project-id")
    parser.add_argument("--assignee-agent-id")
    parser.add_argument("--rolling-log-marker")
    parser.add_argument("--routine-label", default="")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--clean", action="store_true")
    group.add_argument("--exception-summary", help="One-line summary of what was found/filed this tick")
    parser.add_argument(
        "--note",
        help="Override the default rolling-log line for --clean (DAN-385: e.g. "
        "'restated-only -- no new/changed findings, see DAN-NNN' for a tick whose "
        "only findings were already-reported repeats)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.clean:
        if not (args.project_id and args.assignee_agent_id and args.rolling_log_marker):
            parser.error("--clean requires --project-id, --assignee-agent-id, and --rolling-log-marker")
        note = clean_note(now, args.note)
        if args.dry_run:
            print(f"[dry-run] would append to rolling log '{args.rolling_log_marker}': {note}")
            print(f"[dry-run] would mark {args.this_issue_id} cancelled")
            return 0
        log_issue, created = ensure_rolling_log(
            args.company_id, args.rolling_log_marker, args.routine_label,
            args.project_id, args.assignee_agent_id,
        )
        api_post(f"/api/issues/{log_issue['id']}/comments", {"body": note})
        log_link = f"/DAN/issues/{log_issue.get('identifier', log_issue['id'])}"
        api_patch(
            f"/api/issues/{args.this_issue_id}",
            {"status": "cancelled", "comment": f"Clean tick -- logged on [rolling log]({log_link})."},
        )
        print(f"{'Created' if created else 'Reused'} rolling log {log_link}; appended clean note; "
              f"cancelled {args.this_issue_id}.")
    else:
        if args.dry_run:
            print(f"[dry-run] would mark {args.this_issue_id} done: {args.exception_summary}")
            return 0
        api_patch(
            f"/api/issues/{args.this_issue_id}",
            {"status": "done", "comment": args.exception_summary},
        )
        print(f"Exception tick: marked {args.this_issue_id} done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
