"""Limit-class run-failure triage (DAN-926).

Named this way (not `other`) because every prior reference to this file
assumed one already existed -- `ops/seat_watchdog.py`'s docstring and
`tests/test_seat_watchdog.py` both cite "ops/limit-triage.py" as the owner
of `provider_quota`-shaped crashes it deliberately excludes from its own
crash-streak count, and DAN-920's description cites "ops/limit-triage.py
docstring, 2026-09-30: fault first observed and classified" as part of its
evidence trail. **None of that was true.** This file did not exist anywhere
in this repo's git history, any branch, or any work product before this
commit -- confirmed by searching all of `git log --all` and every branch.
The citations above were written as if a real triage tool already existed
and had already recorded a 2026-09-30 incident; it had not. This is the
first version.

## Two measurement bugs, fixed from the start rather than patched

1. **Named buckets, not `other`.** Every failed run's `errorCode` *is*
   already a name (`acpx_session_init_failed`, `provider_quota`,
   `acpx_turn_failed`, `process_lost`, `setup_failed`, ...) -- there was
   never a reason to collapse any of them into `other`. This script buckets
   by `errorCode` directly; `other` is reserved for the genuinely rare case
   of a failed run with no `errorCode` at all.

2. **Retries collapsed into incidents before counting.** A `scheduledRetryAt`
   on a failed run mints an automatic successor (`retryOfRunId` pointing
   back). Because `spawn E2BIG` is deterministic in payload size, a retry
   replays the same oversized payload and fails identically -- one real
   incident costs 3 rows (root + 2 retries) by platform design, not 3
   incidents. This script walks `retryOfRunId` to group failed runs into
   connected chains and reports **incidents** (one per chain), with
   `rawRowCount` kept alongside for comparison, never substituted for it.

## Correcting DAN-920's own evidence trail, measured against the live board 2026-10-09

DAN-920 claims: "the 2026-09-30 'third fault' ... bucketed `other` by
`ops/limit-triage.py`, 7 occurrences, never root-caused, no issue opened."
Measured directly against `GET /api/companies/{id}/heartbeat-runs
?agentId={id}` for every seat (the per-agent filter is honoured and, unlike
the company-wide endpoint, reaches far enough back to check this -- see
below), every detail is off:

- **Not 2026-09-30 -- 2026-09-29.** All 7 raw rows cluster at
  02:16-02:50 UTC on 2026-09-29.
- **Not an unidentified/Beatrice-pattern seat -- Cloud.** All 7 rows ran
  under the Cloud seat's `agentId`, not Beatrice's (Beatrice's own 3 rows
  are the already-known 2026-10-08 DAN-914/DAN-920 incident).
- **Not 7 occurrences -- 3 incidents, all against the same issue.** The 7
  rows collapse into exactly 3 `retryOfRunId` chains (sizes 3, 3, 1), and
  all 3 chains' root runs point at `contextSnapshot.issueId ==
  c90a3219-47ca-4322-9518-2ec8ae2c0d7b` -- DAN-1, "Paperclip onboarding",
  the company's own first issue. This is the same shape DAN-920 already
  documents for DAN-781 ("a heavy, long-lived, deeply cross-linked ticket")
  -- the onboarding issue is exactly that kind of issue, and it was still
  `done`-pending at the time.
- **Never ticketed is correct** -- no issue was ever opened for it, then or
  since.

So: this is the *same* retry-overcount mistake DAN-914 made about the
2026-10-08 Beatrice incident (reported "3 occurrences", was 1), applied a
second time to an event that was apparently never actually measured at all
-- the citation describes a file and a docstring that never existed.

## Why per-agent queries, not the company-wide endpoint

`GET /api/companies/{id}/heartbeat-runs` hard-caps at 1000 rows company-wide
regardless of `?limit=` (tried 2000 and 5000, both still returned exactly
1000) and **silently ignores `?offset=`** -- `offset=1000` and no `offset`
return byte-for-byte the same newest-1000 rows. Nothing in the response
signals either limitation; same "silently-ignored parameter" shape as the
`identifier` filter trap in AGENTS.md (DAN-454), confirmed fresh here on
2026-10-09. At this company's current fleet-wide volume (~500 runs/day
across 6 seats) that 1000-row company-wide cap only reaches back about two
days -- nowhere near 2026-09-30.

`?agentId=` **is honoured** (consistent with AGENTS.md's existing note for
the same endpoint, DAN-337/DAN-694) and each seat produces far fewer runs
per day than the fleet total, so a per-seat `limit=1000` query reaches each
seat's *entire* history: Beatrice back to 2026-09-30T03:02Z, Virgil and
Cloud back to 2026-09-27 (within a day of what is presumably this company's
founding), Oderisi to 2026-10-01, Minos to 2026-10-03. Dante's query hit the
1000-row cap at 2026-10-04 -- if Dante has runs before that date, they are
out of reach by this same mechanism and this script does not claim coverage
before each seat's own earliest returned row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Incident:
    bucket: str
    agent_id: Optional[str]
    run_ids: list[str] = field(default_factory=list)
    first_at: Optional[str] = None
    last_at: Optional[str] = None
    issue_ids: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.run_ids)


def bucket_for(run: dict[str, Any]) -> str:
    """Name the failure by its own `errorCode` -- never collapse into `other`
    unless the run genuinely carries none."""
    return run.get("errorCode") or "other"


def build_incidents(runs: list[dict[str, Any]]) -> list[Incident]:
    """Collapse `retryOfRunId` chains of FAILED runs into one incident each.

    A chain's root is a failed run whose `retryOfRunId` either is null or
    points outside the failed set (succeeded/cancelled predecessor, or a
    predecessor outside the fetched window) -- in either case nothing
    upstream of it should be double-counted as a separate incident.
    """
    failed = {r["id"]: r for r in runs if r.get("status") == "failed"}

    children: dict[str, list[str]] = {}
    for r in failed.values():
        parent = r.get("retryOfRunId")
        if isinstance(parent, str) and parent in failed:
            children.setdefault(parent, []).append(r["id"])

    roots = [r for r in failed.values() if r.get("retryOfRunId") not in failed]

    incidents: list[Incident] = []
    visited: set[str] = set()
    for root in roots:
        if root["id"] in visited:
            continue
        chain_ids: list[str] = []
        stack = [root["id"]]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            chain_ids.append(cur)
            stack.extend(children.get(cur, []))

        chain_runs = sorted((failed[i] for i in chain_ids), key=lambda r: r.get("createdAt") or "")
        issue_ids = sorted(
            {
                iid
                for r in chain_runs
                if (iid := ((r.get("contextSnapshot") or {}).get("issueId")))
            }
        )
        incidents.append(
            Incident(
                bucket=bucket_for(chain_runs[-1]),
                agent_id=chain_runs[0].get("agentId"),
                run_ids=[r["id"] for r in chain_runs],
                first_at=chain_runs[0].get("createdAt"),
                last_at=chain_runs[-1].get("createdAt"),
                issue_ids=issue_ids,
            )
        )

    incidents.sort(key=lambda inc: inc.first_at or "")
    return incidents


def summarize_by_bucket(incidents: list[Incident]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for inc in incidents:
        row = summary.setdefault(inc.bucket, {"incidents": 0, "rawRowCount": 0})
        row["incidents"] += 1
        row["rawRowCount"] += inc.row_count
    return summary


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip agents/runs API.

    Kept separate from the triage logic so tests can swap in a fake that
    never touches the network.
    """

    def __init__(self, api_base: str, api_key: str, run_id: Optional[str] = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id

    def _request(self, method: str, path: str) -> Any:
        url = f"{self.api_base}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        if self.run_id:
            req.add_header("X-Paperclip-Run-Id", self.run_id)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None

    def list_agents(self, company_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/companies/{company_id}/agents")
        return result.get("agents", []) if isinstance(result, dict) else result

    def list_runs_for_agent(self, company_id: str, agent_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/heartbeat-runs?limit={limit}&agentId={agent_id}"
        result = self._request("GET", path)
        return result.get("runs", []) if isinstance(result, dict) else result


def fetch_all_runs(client: PaperclipClient, company_id: str) -> list[dict[str, Any]]:
    """Every reachable run for every seat -- see module docstring for why
    this goes through the per-agent filter rather than the company-wide
    endpoint."""
    runs: list[dict[str, Any]] = []
    for agent in client.list_agents(company_id):
        runs.extend(client.list_runs_for_agent(company_id, agent["id"]))
    return runs


def format_report(incidents: list[Incident]) -> str:
    summary = summarize_by_bucket(incidents)
    lines = [
        f"{'bucket':<28} {'incidents':>9} {'rawRows':>8}",
        "-" * 48,
    ]
    for bucket, counts in sorted(summary.items(), key=lambda kv: kv[1]["rawRowCount"], reverse=True):
        lines.append(f"{bucket:<28} {counts['incidents']:>9} {counts['rawRowCount']:>8}")
    lines.append("")
    lines.append("Incidents, oldest first:")
    for inc in incidents:
        lines.append(
            f"  {inc.bucket:<24} agent={inc.agent_id} rows={inc.row_count} "
            f"{inc.first_at} -> {inc.last_at} issues={inc.issue_ids}"
        )
    return "\n".join(lines)


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
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
    runs = fetch_all_runs(client, args.company_id)
    incidents = build_incidents(runs)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": summarize_by_bucket(incidents),
                    "incidents": [
                        {
                            "bucket": inc.bucket,
                            "agentId": inc.agent_id,
                            "runIds": inc.run_ids,
                            "rawRowCount": inc.row_count,
                            "firstAt": inc.first_at,
                            "lastAt": inc.last_at,
                            "issueIds": inc.issue_ids,
                        }
                        for inc in incidents
                    ],
                },
                indent=2,
            )
        )
    else:
        print(format_report(incidents))
    return 0


if __name__ == "__main__":
    sys.exit(main())
