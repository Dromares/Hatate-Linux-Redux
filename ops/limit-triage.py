#!/usr/bin/env python3
"""Run-failure triage for the Paperclip heartbeat-run feed.

One tool, two measurement jobs (reconciled in DAN-1130 from two independently
written files that shared this name -- see "Provenance" below):

1. **Sample report** (the original, DAN-94/99/103/105/106/117/118): the newest
   `--limit N` runs, split into quota (`limit`) vs turn-level (`access`) vs
   pre-session (`init`) vs other faults; `access` deaths grouped into time
   bands with a verdict per band; retry-storm gap stats; per-agent start-rate
   bursts and same-issue cooldown breaches. Governs the exit code.
2. **Incident report** (DAN-926): every reachable failed run bucketed by its
   own `errorCode`, with `retryOfRunId` chains collapsed into incidents so a
   retried deterministic failure is counted once, not once per retry.

Usage: PAPERCLIP_API_KEY=... PAPERCLIP_API_URL=... PAPERCLIP_COMPANY_ID=... \
       python3 ops/limit-triage.py [--limit N] [--json]
Exit code: 0 = clean, 1 = deaths present but self-resolving (quota exhaustion,
or an access band with the account-wide-gate or under-determined-single-agent
signature -- see ops/RUNBOOK-limit-deaths.md), 2 = the most recent access band
shows >=1 in-band success (the genuinely-per-agent signature DAN-106 has not
yet observed -- investigate). The burst / same-issue-cooldown / incident
sections are informational and do not affect the exit code.

## Provenance

PR #4 (DAN-926) added this file believing no triage tool existed, while the
project-workspace `ops/limit-triage.py` (the one the runbook, `seat_watchdog.py`
and the DAN-278 routine description cite) already did. Each carried a
correctness property the other lacked: the original had the DAN-106 access-band
rule, the DAN-117/118 issue-aware burst report and the exit-code-2 contract;
PR #4 had per-`errorCode` buckets and `retryOfRunId` collapsing. This file
carries both and is canonical; DAN-1130 has the reconciliation.

## Discriminator (DAN-99/DAN-94/DAN-103/DAN-105/DAN-106)

`errorCode=acpx_turn_failed` covers two distinct faults distinguishable by the
`error` string:
  - "...terminal limit failure."  -> quota exhaustion (wait it out). Triaged
    with a 5h rolling window -- the shared subscription quota really does
    reset on a 5h boundary, so that resolution is correct for this fault.
  - "...terminal access failure." -> a turn-level fault on an already
    authenticated session. Per DAN-103/DAN-105, this is usually NOT an
    expired credential. Per DAN-106, DAN-103's per-agent/5h-window
    discriminator for telling a credential fault from a self-resolving one
    was itself wrong: it measured which agent happened to be running, not
    which agent was affected, and drowned a ~15-minute outage in a 5h spend
    bucket. `access` deaths are triaged by TIME BAND instead (see
    detect_access_bands below and ops/RUNBOOK-limit-deaths.md) -- group
    contiguous deaths, then check who/what was running *inside* the band,
    not inside a 5h window around it.

`errorCode=acpx_session_init_failed` is a separate, pre-session fault (a
rejected credential or `spawn E2BIG` surfaces here, before a session exists)
and has its own `init` column in the sample report so it neither pollutes the
limit/access counts nor hides inside `other`.

DAN-117: `cooldownSec`/`maxConcurrentRuns` (under `runtimeConfig.heartbeat`)
are scoped per-issue, not per-agent -- measured, not documented (see
`agent_cooldowns()` below and RUNBOOK). A per-agent start-rate burst that
hits N distinct issues once each is NOT a cooldown breach; it is the absence
of an agent-level dispatch ceiling. The burst report below is issue-aware so
it states which case it is instead of leaving the reader to guess.

DAN-118: the same-issue repeat that promotes a burst to `real_breach` must
also exclude retry-caused repeats, the same split `same_issue_cooldown_
breaches()` already applies. A retry landing inside its parent's cooldown is
Defect 1 (the retry scheduler ignoring cooldown), not a per-issue dispatcher
breach; a burst whose only in-cooldown same-issue repeat is a retry is
actually the clearest evidence of the missing agent-level ceiling (`no_agent_
ceiling`), not a dispatcher bug.

## Incident report: two measurement bugs fixed from the start rather than patched

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
second time. (The citation was to the workspace copy of this tool, which did
bucket `acpx_session_init_failed` as `other` -- so "bucketed other" is accurate;
the date, seat and occurrence count are what were wrong.)

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
import statistics
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DESCRIPTION = "Triage Paperclip heartbeat-run failures: quota vs access bands, bursts, retry-collapsed incidents."

WINDOW_SECONDS = 5 * 3600

# DAN-106: contiguous-time grouping for `access` deaths. A gap larger than
# this splits one band from the next. 900s (15min) was picked because the
# three known bands in the 200-run sample this was built against are all
# well under 15 minutes internally (8s, 935s, 82s) while being separated from
# each other and from surrounding traffic by hours.
BAND_GAP_SECONDS = 900

# DAN-105: rolling per-agent start-rate detection. 6-in-60s is picked from
# observed data in the 200-run sample this was built against: the known wake
# storm (DAN-105) peaks at 8 starts/60s for one agent, while the busiest
# *normal* agent in the same sample tops out at 5/60s. 6 sits strictly
# between the two so it catches the storm without flagging ordinary load.
BURST_WINDOW_SECONDS = 60
BURST_THRESHOLD = 6

# DAN-117: cooldownSec lives under runtimeConfig.heartbeat, but the agents API
# only returns that block in full for the caller's own identity -- GET
# /api/agents/{id} for any *other* agent silently returns runtimeConfig: {}
# (not a permission error), and GET /api/agents/{id}/configuration demands a
# grant ("agents:suggest-changes") this script does not hold and would not be
# read-only anyway. So cross-agent cooldownSec cannot be read live by an
# unprivileged caller. These are the last board-confirmed values (see
# ops/PLATFORM-BUG-run-amplifier.md, "what we already did on our side" and
# the DAN-117 issue text) -- update this table when they change rather than
# assuming one constant across agents.
FALLBACK_COOLDOWN_SEC = {
    "Cloud": 60,
    "Virgil": 30,
    "Dante": 30,
}
DEFAULT_COOLDOWN_SEC = 60  # used only if an agent has no live value and no table entry


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

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def list_agents(self, company_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/companies/{company_id}/agents")
        return result.get("agents", []) if isinstance(result, dict) else result

    def list_runs_for_agent(self, company_id: str, agent_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/heartbeat-runs?limit={limit}&agentId={agent_id}"
        result = self._request("GET", path)
        return result.get("runs", []) if isinstance(result, dict) else result


def fetch_all_runs(
    client: PaperclipClient, company_id: str, agents: Optional[list[dict[str, Any]]] = None
) -> list[dict[str, Any]]:
    """Every reachable run for every seat -- see module docstring for why
    this goes through the per-agent filter rather than the company-wide
    endpoint. Pass `agents` when the caller already listed them."""
    runs: list[dict[str, Any]] = []
    for agent in agents if agents is not None else client.list_agents(company_id):
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


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def run_ts(run):
    return parse_ts(run.get("startedAt") or run.get("createdAt"))


def classify(run):
    """Fault class for the sample report's per-window counts.

    `init` is `acpx_session_init_failed` (DAN-926): a pre-session fault with
    its own `errorCode`, so it gets its own column rather than being folded
    into `other` -- the original bucketing hid it there.
    """
    if run.get("errorCode") == "acpx_session_init_failed":
        return "init"
    error = run.get("error") or ""
    if "access failure" in error:
        return "access"
    if "limit failure" in error:
        return "limit"
    if run.get("status") == "failed":
        return "other"
    return None


def agent_names(agents):
    """agentId -> display name map, falling back to the id."""
    return {a["id"]: a.get("name") or a.get("displayName") or a["id"] for a in agents}


def agent_cooldowns(client, agent_ids, names):
    """DAN-117: per-agent cooldownSec -- live for the caller's own agent,
    board-confirmed fallback for everyone else (see FALLBACK_COOLDOWN_SEC
    above for why live isn't available cross-agent). Never fatal.

    Returns {agent_id: (cooldown_sec, source_label)}.
    """
    result = {}
    try:
        me = client.get("/api/agents/me")
    except Exception:
        me = None
    if me and me.get("id") in agent_ids:
        heartbeat = (me.get("runtimeConfig") or {}).get("heartbeat") or {}
        if "cooldownSec" in heartbeat:
            result[me["id"]] = (heartbeat["cooldownSec"], "live (own record)")
    for agent_id in agent_ids:
        if agent_id in result:
            continue
        name = names.get(agent_id, agent_id)
        if name in FALLBACK_COOLDOWN_SEC:
            result[agent_id] = (FALLBACK_COOLDOWN_SEC[name], "fallback, board-confirmed")
        else:
            result[agent_id] = (DEFAULT_COOLDOWN_SEC, "fallback, unconfirmed default")
    return result


def issue_key(run):
    """DAN-117: contextSnapshot.issueId, with each None (timer run) treated as
    its own singleton rather than collapsed into one 'no issue' bucket -- two
    timer runs are not 'the same issue' just because both lack one.
    """
    issue_id = (run.get("contextSnapshot") or {}).get("issueId")
    if issue_id is not None:
        return issue_id
    return f"__singleton_{run.get('id')}__"


def window_start(dt):
    epoch_seconds = dt.timestamp()
    bucket = int(epoch_seconds // WINDOW_SECONDS) * WINDOW_SECONDS
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def detect_access_bands(runs, gap_sec):
    """Group `access` deaths into contiguous time bands (DAN-106).

    A gap of more than `gap_sec` between consecutive access-death starts
    splits one band from the next. For each band, report who/what was
    actually running *inside that exact span* — not inside a 5h window
    around it, which is ~20x too coarse for outages that last minutes.
    """
    timestamped = [(run_ts(r), r) for r in runs]
    timestamped = [(t, r) for t, r in timestamped if t is not None]
    timestamped.sort(key=lambda x: x[0])

    access_runs = [(t, r) for t, r in timestamped if classify(r) == "access"]

    bands = []
    delta = timedelta(seconds=gap_sec)
    i = 0
    n = len(access_runs)
    while i < n:
        j = i
        while j + 1 < n and access_runs[j + 1][0] - access_runs[j][0] <= delta:
            j += 1
        group = access_runs[i:j + 1]
        start_ts, end_ts = group[0][0], group[-1][0]

        agents, models = set(), set()
        cost = 0.0
        for _, r in group:
            agents.add(r.get("agentId"))
            usage = r.get("usageJson") or {}
            model = usage.get("model")
            if model:
                models.add(model)
            cost += usage.get("costUsd") or 0

        in_band_success = 0
        agents_active_in_band = set()
        for t, r in timestamped:
            if start_ts <= t <= end_ts:
                agents_active_in_band.add(r.get("agentId"))
                if r.get("status") == "succeeded":
                    in_band_success += 1

        time_to_recovery = None
        for t, r in timestamped:
            if t > end_ts and r.get("status") == "succeeded":
                time_to_recovery = (t - end_ts).total_seconds()
                break

        bands.append({
            "start": start_ts,
            "end": end_ts,
            "deaths": len(group),
            "agents": agents,
            "models": models,
            "cost": cost,
            "in_band_success": in_band_success,
            "agents_active_in_band": agents_active_in_band,
            "time_to_recovery": time_to_recovery,
        })
        i = j + 1
    return bands


def access_band_verdict(band):
    """DAN-106 discriminator, replacing the DAN-103 per-agent/5h-window rule.

    - >=1 in-band success -> genuinely per-agent. New; not observed before
      DAN-106 shipped. Call it out loudly rather than folding it into the
      account-wide case.
    - 0 in-band successes, spanning >1 agent or >1 model -> account-wide gate
      closure. Not fixable by re-running "the affected agent" because there
      wasn't one; it hit everyone who was running.
    - 0 in-band successes, single agent, and that agent was the only one
      awake in the span -> same shape (nobody else was there to *not* be
      affected), but under-determined: we cannot rule out per-agent from a
      sample of one. Say so; do not guess credential.
    """
    if band["in_band_success"] > 0:
        return "per_agent_new"
    if len(band["agents"]) > 1 or len(band["models"]) > 1:
        return "account_wide"
    if len(band["agents_active_in_band"]) <= 1:
        return "under_determined_sole_agent"
    return "under_determined_other_agents_unaffected"


VERDICT_TEXT = {
    "per_agent_new": (
        "GENUINELY PER-AGENT (NEW SIGNATURE). This band had at least one "
        "successful run started inside it — other work was going through "
        "while this agent's runs died. That is NOT the account-wide gate "
        "closure shape seen before DAN-106. Investigate this agent/session "
        "specifically; do not assume it self-heals like the account-wide "
        "bands."
    ),
    "account_wide": (
        "ACCOUNT-WIDE GATE CLOSURE. Zero successful runs started anywhere "
        "in this band, and it spans more than one agent or more than one "
        "model. This is not per-agent and not fixable by re-running "
        "'the affected agent' — everyone who tried to run in this span "
        "died. Self-heals; back off past the band, do not re-authenticate."
    ),
    "under_determined_sole_agent": (
        "SAME SHAPE, UNDER-DETERMINED. Zero successful runs in this band, "
        "but only one agent was awake at all during it, so there was no "
        "second agent available to *not* be affected. Cannot distinguish "
        "account-wide from per-agent from a sample of one agent. Do not "
        "guess credential; wait for a multi-agent band or corroborating "
        "data before concluding either way."
    ),
    "under_determined_other_agents_unaffected": (
        "UNDER-DETERMINED. Only one agent had access deaths in this band, "
        "and other agents were active in the same span without an access "
        "death (though not necessarily with a success). Weak evidence "
        "against account-wide, but zero in-band successes overall means it "
        "does not meet the per-agent bar either. Do not guess credential."
    ),
}


def detect_start_bursts(runs, threshold, window_sec):
    """Per-agent rolling start-rate bursts (DAN-105), issue-aware (DAN-117).

    A run at index j "qualifies" if the count of same-agent starts in the
    trailing `window_sec` ending at j is >= threshold. Qualifying indices are
    grouped by index-adjacency (not by window overlap), so a burst that
    tapers off below threshold and only later re-crosses it is reported as
    two separate bursts instead of one artificially long one.

    Each burst also carries `by_issue`: the same starts grouped by
    `issue_key()`, since cooldownSec/maxConcurrentRuns are per-issue (DAN-117)
    and a burst of N starts across N distinct issues breaches nobody's
    cooldown.
    """
    by_agent = defaultdict(list)
    for r in runs:
        ts = run_ts(r)
        if ts is None:
            continue
        by_agent[r.get("agentId")].append((ts, r))

    bursts = []
    delta = timedelta(seconds=window_sec)
    for agent_id, items in by_agent.items():
        items.sort(key=lambda x: x[0])
        times = [t for t, _ in items]
        n = len(times)
        window_start_idx = [0] * n
        i = 0
        qualifies = [False] * n
        for j in range(n):
            while times[j] - times[i] > delta:
                i += 1
            window_start_idx[j] = i
            qualifies[j] = (j - i + 1) >= threshold

        j = 0
        while j < n:
            if not qualifies[j]:
                j += 1
                continue
            group_end = j
            while group_end + 1 < n and qualifies[group_end + 1]:
                group_end += 1
            extent_start = window_start_idx[j]
            extent_items = items[extent_start:group_end + 1]
            sources = defaultdict(int)
            by_issue = defaultdict(list)
            for t, r in extent_items:
                sources[r.get("invocationSource") or "unknown"] += 1
                by_issue[issue_key(r)].append((t, r))
            bursts.append({
                "agentId": agent_id,
                "start": extent_items[0][0],
                "end": extent_items[-1][0],
                "count": len(extent_items),
                "sources": dict(sources),
                "by_issue": dict(by_issue),
            })
            j = group_end + 1
    return sorted(bursts, key=lambda b: b["start"])


BURST_VERDICT_TEXT = {
    "no_breach_one_per_issue": (
        "NO CONFIG BREACH. Per-issue cooldown respected on every start. "
        "This is the absence of an agent-level dispatch ceiling, not an "
        "unenforced setting. Fix is to add a per-agent cap, not to enforce "
        "cooldownSec."
    ),
    "no_breach_repeat_within_cooldown": (
        "NO CONFIG BREACH. One or more issues were dispatched more than "
        "once inside this burst, but every repeat landed at or after the "
        "agent's configured cooldownSec -- the per-issue setting was still "
        "respected."
    ),
    "real_breach": "REAL PER-ISSUE COOLDOWN BREACH.",
    "no_agent_ceiling": (
        "NO PER-ISSUE COOLDOWN BREACH -- MISSING AGENT-LEVEL CEILING. Every "
        "same-issue repeat inside cooldown in this burst is retry-caused "
        "(Defect 1: the retry scheduler ignores cooldownSec), not a fresh "
        "dispatch landing early. Strip the retries out and this burst is "
        "one fresh start per distinct issue -- exactly the missing "
        "agent-level dispatch ceiling, not a violated per-issue setting."
    ),
}


def burst_verdict(burst, cooldown_sec):
    """DAN-117: key the verdict on distinct_issues / max-per-issue, not the
    raw start count (see ops/PLATFORM-BUG-run-amplifier.md Defect 3). Only a
    repeat on the *same* issue landing inside that issue's own cooldown is a
    real per-issue cooldown breach; one start per issue across many issues is
    a missing agent-level dispatch ceiling, not a violated setting.

    DAN-118: a same-issue repeat inside cooldown that is itself a retry
    (`r1.retryOfRunId == r0.id`) is Defect 1 (the retry scheduler ignoring
    cooldown), not a per-issue dispatcher breach -- same split
    `same_issue_cooldown_breaches()` already applies. Only a *fresh* (non-
    retry) same-issue repeat inside cooldown promotes a burst to
    `real_breach`. A burst whose only in-cooldown same-issue repeats are
    retries reports `no_agent_ceiling` instead: many distinct issues, one
    fresh start each, with the retry-caused repeats surfaced separately.

    Returns (verdict_key, distinct_issues, max_starts_on_any_single_issue,
    fresh_offending, retry_offending), where each is a list of
    (issue_key, t0, r0, t1, r1, gap_seconds) for same-issue starts that
    landed inside cooldown_sec of each other, split by whether the second
    start is a retry of the first.
    """
    by_issue = burst["by_issue"]
    distinct_issues = len(by_issue)
    max_on_one_issue = max(len(v) for v in by_issue.values())

    fresh_offending = []
    retry_offending = []
    for key, items in by_issue.items():
        items = sorted(items, key=lambda x: x[0])
        for (t0, r0), (t1, r1) in zip(items, items[1:], strict=False):
            gap = (t1 - t0).total_seconds()
            if gap < cooldown_sec:
                entry = (key, t0, r0, t1, r1, gap)
                if r1.get("retryOfRunId") == r0.get("id"):
                    retry_offending.append(entry)
                else:
                    fresh_offending.append(entry)

    if distinct_issues == burst["count"]:
        verdict = "no_breach_one_per_issue"
    elif fresh_offending:
        verdict = "real_breach"
    elif retry_offending:
        verdict = "no_agent_ceiling"
    else:
        verdict = "no_breach_repeat_within_cooldown"
    return verdict, distinct_issues, max_on_one_issue, fresh_offending, retry_offending


def same_issue_cooldown_breaches(runs, cooldowns):
    """DAN-117: consecutive same-issue starts per agent, checked against that
    agent's own cooldownSec -- independent of the burst detector above, which
    only looks inside threshold-crossing windows. Splits retries from fresh
    dispatches because they are two different platform bugs: the retry
    scheduler ignoring cooldown entirely (Defect 1) vs. a fresh dispatch
    landing inside cooldown (Defect 2's "real, smaller bug") -- see
    ops/PLATFORM-BUG-run-amplifier.md.

    Returns {agent_id: [breach, ...]} where each breach is a dict with
    issueId, t0/r0, t1/r1, gap, is_retry.
    """
    by_agent_issue = defaultdict(lambda: defaultdict(list))
    for r in runs:
        ts = run_ts(r)
        if ts is None:
            continue
        issue_id = (r.get("contextSnapshot") or {}).get("issueId")
        if issue_id is None:
            continue  # DAN-117: singleton: never "the same issue" as another None
        by_agent_issue[r.get("agentId")][issue_id].append((ts, r))

    breaches_by_agent = defaultdict(list)
    for agent_id, by_issue in by_agent_issue.items():
        cooldown_sec, _source = cooldowns.get(agent_id, (DEFAULT_COOLDOWN_SEC, "fallback"))
        for issue_id, items in by_issue.items():
            items.sort(key=lambda x: x[0])
            for (t0, r0), (t1, r1) in zip(items, items[1:], strict=False):
                gap = (t1 - t0).total_seconds()
                if gap < cooldown_sec:
                    breaches_by_agent[agent_id].append({
                        "issueId": issue_id,
                        "t0": t0, "r0": r0, "t1": t1, "r1": r1,
                        "gap": gap,
                        "is_retry": r1.get("retryOfRunId") == r0.get("id"),
                    })
    return breaches_by_agent


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def build_windows(runs):
    """Per-5h-window counts (runs, successes, cost, and each fault class)."""
    windows = defaultdict(lambda: {
        "runs": 0, "cost": 0.0, "success": 0, "limit": 0, "access": 0, "init": 0, "other": 0,
        "agents_seen": set(),
        "limit_by_agent": defaultdict(int),
    })
    for r in runs:
        ts = run_ts(r)
        if ts is None:
            continue
        w = windows[window_start(ts)]
        w["runs"] += 1
        w["cost"] += (r.get("usageJson") or {}).get("costUsd") or 0
        w["agents_seen"].add(r.get("agentId"))
        if r.get("status") == "succeeded":
            w["success"] += 1
        fault = classify(r)
        if fault:
            w[fault] += 1
        if fault == "limit":
            w["limit_by_agent"][r.get("agentId")] += 1
    return windows


def recent_sample(runs, limit):
    """Newest `limit` runs by creation time -- the window `--limit N` selects.

    The original tool asked the company-wide endpoint for `?limit=N`, which
    returns the newest N; the per-seat fetch returns every seat's history, so
    the same sample is cut here.
    """
    stamped = [(parse_ts(r.get("createdAt")) or run_ts(r), r) for r in runs]
    stamped = [(t, r) for t, r in stamped if t is not None]
    stamped.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in stamped[:limit]]


def compute_exit_code(windows, bands):
    """0 clean; 1 self-resolving deaths; 2 newest access band has an in-band
    success (RUNBOOK-limit-deaths.md "Exit codes")."""
    exit_code = 0
    if windows and windows[max(windows)]["limit"] > 0:
        exit_code = 1
    if bands:
        if access_band_verdict(bands[-1]) == "per_agent_new":
            exit_code = 2
        elif exit_code < 1:
            exit_code = 1
    return exit_code


def print_sample_report(runs, windows, agent_name, cooldowns, band_gap_sec, burst_window_sec, burst_threshold):
    """The original text report (DAN-94..DAN-118) over the `--limit N` sample.
    Returns the access bands, which the exit code is derived from."""
    by_id = {r["id"]: r for r in runs}
    print(f"=== limit-triage: last {len(runs)} runs, {len(windows)} windows ===\n")
    print(f"{'window (UTC)':<18}{'runs':>6}{'succ':>6}{'cost':>10}{'limit':>8}{'access':>8}{'init':>8}{'other':>8}")
    for w in sorted(windows):
        d = windows[w]
        print(f"{w.strftime('%m-%d %H:%M'):<18}{d['runs']:>6}{d['success']:>6}{d['cost']:>10.2f}{d['limit']:>8}{d['access']:>8}{d['init']:>8}{d['other']:>8}")
    print()

    # `limit` deaths: 5h windowing is the right resolution here (the shared
    # subscription quota really does reset on a 5h boundary). Unchanged by
    # DAN-106.
    for w in sorted(windows):
        d = windows[w]
        if d["limit"] == 0:
            continue
        label = w.strftime("%m-%d %H:%M")
        by_agent = ", ".join(
            f"{agent_name(a)} {c}" for a, c in sorted(d["limit_by_agent"].items(), key=lambda kv: -kv[1])
        )
        print(f"  by-agent (limit):  {by_agent}")
        print(f"[{label}] QUOTA EXHAUSTED. Wait for the window boundary; reduce burn. ({d['limit']} limit deaths)")

    # `access` deaths: DAN-106 band detection replaces the DAN-103 per-agent/
    # 5h-window discriminator. A 5h bucket drowns a several-minute outage in
    # hours of unrelated traffic, and "concentrated on one agent" measures
    # who was awake, not who was affected.
    bands = detect_access_bands(runs, band_gap_sec)
    print(f"=== access bands (gap>{band_gap_sec}s splits a band, n={len(bands)}) ===")
    if not bands:
        print("no access deaths in this sample\n")
    for b in bands:
        dur = (b["end"] - b["start"]).total_seconds()
        agents_str = ",".join(sorted(agent_name(a) for a in b["agents"]))
        models_str = ",".join(sorted(b["models"])) if b["models"] else "unknown"
        recovery = f"+{b['time_to_recovery']:.0f}s" if b["time_to_recovery"] is not None else "none in sample"
        print(f"[{b['start'].strftime('%m-%d %H:%M:%S')} -> {b['end'].strftime('%H:%M:%S')}]  "
              f"dur={dur:.0f}s  deaths={b['deaths']}  agents=[{agents_str}]  models=[{models_str}]  "
              f"in-band-successes={b['in_band_success']}  cost=${b['cost']:.2f}  recovery={recovery}")
        verdict = access_band_verdict(b)
        print(f"  VERDICT: {VERDICT_TEXT[verdict]}\n")

    # Retry-storm evidence: gap between a transient-failure retry being scheduled
    # and its parent run finishing. Sub-second gaps indicate a storm, not a backoff.
    gaps = []
    for r in runs:
        if r.get("scheduledRetryReason") != "transient_failure" or not r.get("retryOfRunId"):
            continue
        parent = by_id.get(r["retryOfRunId"])
        if not parent:
            continue
        parent_finished = parse_ts(parent.get("finishedAt") or parent.get("updatedAt"))
        child_created = parse_ts(r.get("createdAt"))
        if parent_finished and child_created:
            gaps.append((child_created - parent_finished).total_seconds())

    print(f"=== retry-storm evidence (transient_failure retries: n={len(gaps)}) ===")
    if gaps:
        print(f"median gap: {statistics.median(gaps):.3f}s  max gap: {max(gaps):.3f}s  min gap: {min(gaps):.3f}s")
    else:
        print("no transient_failure retries in this window")

    # Start-rate bursts (DAN-105): a fault signature can come from too many
    # *fresh* starts landing on one agent in a short span rather than from
    # concurrent/overlapping runs. This is invisible in the per-window death
    # counts above, which only see the outcome, not the arrival rate that
    # produced it.
    bursts = detect_start_bursts(runs, burst_threshold, burst_window_sec)
    print(f"\n=== start-rate bursts (>={burst_threshold} starts/{burst_window_sec}s, per agent) ===")
    if bursts:
        for b in bursts:
            dur = (b["end"] - b["start"]).total_seconds()
            by_source = ", ".join(
                f"{src}={c}" for src, c in sorted(b["sources"].items(), key=lambda kv: -kv[1])
            )
            cooldown_sec, cooldown_source = cooldowns.get(
                b["agentId"], (DEFAULT_COOLDOWN_SEC, "fallback, unconfirmed default")
            )
            verdict, distinct_issues, max_on_one_issue, fresh_offending, retry_offending = burst_verdict(b, cooldown_sec)
            print(f"[{b['start'].strftime('%m-%d %H:%M:%S')} -> {b['end'].strftime('%H:%M:%S')}] "
                  f"{agent_name(b['agentId'])}: {b['count']} starts in {dur:.0f}s  sources: {by_source}  "
                  f"distinct_issues={distinct_issues}  max_starts_on_any_single_issue={max_on_one_issue}")

            def _print_offending(pairs, retry_flag):
                for key, t0, _r0, t1, _r1, gap in sorted(pairs, key=lambda o: o[5]):
                    issue_label = "none (timer, singleton)" if str(key).startswith("__singleton_") else key
                    print(f"    issue={issue_label}  gap={gap:.2f}s  "
                          f"{t0.strftime('%H:%M:%S')} -> {t1.strftime('%H:%M:%S')}  retryOf={retry_flag}")

            if verdict == "real_breach":
                print(f"  VERDICT: {BURST_VERDICT_TEXT[verdict]} "
                      f"(agent cooldownSec={cooldown_sec}, {cooldown_source})")
                _print_offending(fresh_offending, "no")
                if retry_offending:
                    print("  ALSO RETRY-CAUSED (Defect 1: retry scheduler ignores cooldown, not this dispatcher):")
                    _print_offending(retry_offending, "yes")
            elif verdict == "no_agent_ceiling":
                print(f"  VERDICT: {BURST_VERDICT_TEXT[verdict]}")
                print("  RETRY-CAUSED REPEATS (Defect 1: retry scheduler ignores cooldown, not this dispatcher):")
                _print_offending(retry_offending, "yes")
            else:
                print(f"  VERDICT: {BURST_VERDICT_TEXT[verdict]}")
    else:
        print("none")

    # DAN-117: same-issue cooldown breaches, independent of the burst report
    # above (which only looks inside threshold-crossing windows). This is
    # the section that actually answers "was cooldownSec violated" per its
    # real per-issue scope.
    print("\n=== same-issue cooldown breaches (per agent, each agent's own cooldownSec) ===")
    breaches_by_agent = same_issue_cooldown_breaches(runs, cooldowns)
    if not breaches_by_agent:
        print("none")
    else:
        for agent_id, breaches in sorted(breaches_by_agent.items(), key=lambda kv: -len(kv[1])):
            cooldown_sec, cooldown_source = cooldowns.get(
                agent_id, (DEFAULT_COOLDOWN_SEC, "fallback, unconfirmed default")
            )
            retries = [b for b in breaches if b["is_retry"]]
            fresh = [b for b in breaches if not b["is_retry"]]
            print(f"{agent_name(agent_id)}  cooldownSec={cooldown_sec} ({cooldown_source})  "
                  f"{len(breaches)} breaches = {len(retries)} retries + {len(fresh)} fresh")
            if fresh:
                print("  FRESH (not a retry -- genuine dispatcher bug, Defect 2):")
                for b in sorted(fresh, key=lambda x: x["gap"]):
                    print(f"    issue={b['issueId']}  gap={b['gap']:.2f}s  "
                          f"{b['t0'].strftime('%m-%d %H:%M:%S')} -> {b['t1'].strftime('%H:%M:%S')}  "
                          f"{b['r0'].get('invocationSource')}->{b['r1'].get('invocationSource')}  retryOf=no")
            if retries:
                worst = min(retries, key=lambda x: x["gap"])
                print(f"  RETRY (retry scheduler ignores cooldown -- Defect 1, not this dispatcher): "
                      f"{len(retries)} breaches, worst gap {worst['gap']:.3f}s")
    return bands


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument("--limit", type=int, default=200,
                        help="sample report covers the newest N runs (default: %(default)s); "
                             "the incident report always covers every reachable run")
    parser.add_argument("--band-gap-sec", type=int, default=BAND_GAP_SECONDS,
                        help="gap between access deaths that splits one band from the next (default: %(default)s)")
    parser.add_argument("--burst-window-sec", type=int, default=BURST_WINDOW_SECONDS,
                        help="rolling window for start-rate burst detection (default: %(default)s)")
    parser.add_argument("--burst-threshold", type=int, default=BURST_THRESHOLD,
                        help="starts within the window that count as a burst (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
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
    agents = client.list_agents(args.company_id)
    names = agent_names(agents)
    runs = fetch_all_runs(client, args.company_id, agents)
    incidents = build_incidents(runs)

    sample = recent_sample(runs, args.limit)
    windows = build_windows(sample)

    if args.json:
        bands = detect_access_bands(sample, args.band_gap_sec)
        exit_code = compute_exit_code(windows, bands)
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
                    "accessBands": [
                        {
                            "start": b["start"].isoformat(),
                            "end": b["end"].isoformat(),
                            "deaths": b["deaths"],
                            "inBandSuccesses": b["in_band_success"],
                            "verdict": access_band_verdict(b),
                        }
                        for b in bands
                    ],
                    "exitCode": exit_code,
                },
                indent=2,
            )
        )
        return exit_code

    cooldowns = agent_cooldowns(client, {r.get("agentId") for r in sample}, names)
    bands = print_sample_report(
        sample, windows, lambda agent_id: names.get(agent_id, agent_id), cooldowns,
        args.band_gap_sec, args.burst_window_sec, args.burst_threshold,
    )
    print(f"\n=== failed-run incidents ({len(runs)} reachable runs, retry chains collapsed) ===")
    print(format_report(incidents))
    return compute_exit_code(windows, bands)


if __name__ == "__main__":
    sys.exit(main())
