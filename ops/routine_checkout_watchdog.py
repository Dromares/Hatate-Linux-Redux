#!/usr/bin/env python3
"""Cross-routine no-checkout detection arm -- DAN-666 (follow-up to DAN-645).

DAN-645 found that the Default-approver gate sweep and False-done sweep
routines produced ticks for ~19h that were created fine but never checked
out (`checkoutRunId` and `executionRunId` both null forever, so step-0's own
orphan-guard -- the thing that would normally drain a backlog like this --
never ran either, since it lives inside a checkout that never happened).
Nothing in the company alerted on this: the Seat-health watchdog (narrow
arm) routine already fires every 15 minutes with read access to every
routine, but had no arm that looked at OTHER routines' checkout health, only
at seat crash-loops (seat_watchdog.py) and stranded approval stages
(stranded_review.py). This script is that missing arm.

A finding requires, for one OTHER active schedule-triggered routine:
  1. Its most recent N runs (`--limit`, default 5, matching the DAN-666
     proposal) include an unbroken run of "misses" starting from the
     newest: `status: in_progress` with BOTH `checkoutRunId` and
     `executionRunId` null -- never checked out, not merely slow. Each
     run's linked issue is re-fetched fresh via `GET /api/issues/{id}`
     rather than trusting the run's embedded `linkedIssue.status`, which can
     lag (same rule the routine's own step-0 orphan-guard applies, per
     DAN-479/DAN-532).
  2. The elapsed time from `now` back to the OLDEST run in that unbroken
     streak (its `triggeredAt`) is at least `STALE_MULTIPLIER` (2x) the
     routine's own configured cadence -- so one transient single-tick miss
     can never false-positive. Cadence is read directly off the routine's
     enabled schedule trigger as `nextRunAt - lastFiredAt`: exact for
     whatever cron expression is configured (fixed-interval, hourly-offset,
     daily, ...) with no cron-string parsing needed.

DAN-703 (follow-up to DAN-693/DAN-666) widened the miss shape: DAN-680
wedged behind a run that was CREATED but sat `queued` (`claimedAt`/
`startedAt` both null) for ~3h before ever dispatching. A queued run holds
`executionRunId` non-null on its target issue for the entire time it sits
queued -- the same mechanism behind the standing `409 Issue run ownership
conflict` rule -- so the original "`executionRunId` null" clause above
never caught it even though the issue was genuinely never checked out.
`wedged_checkout_streak`/`wedged_checkout_miss` are the sibling arm for
this shape: same two-part bar (unbroken streak, 2x-cadence elapsed), but
keyed on resolving `executionRunId` via `GET /api/heartbeat-runs/{id}` and
finding it non-terminal with `startedAt`/`claimedAt` both still null,
rather than on `executionRunId` being absent. A finding of this shape also
reports the owning seat's running/queued occupancy (a queued run behind an
occupied single-concurrency seat is not by itself evidence it is dead --
DAN-693/DAN-694) and, when the stuck run is itself a retry successor whose
`scheduledRetryAt` lands on an instant shared by another seat's run, flags
that as a company-wide quota/clock boundary rather than a routine-specific
failure.

Known limitation, by construction: a routine cannot reliably detect its own
checkout failure -- if a tick never fires (or never checks out), this arm
never runs either. This script therefore always excludes its own routine
(`SELF_ROUTINE_ID`, the Seat-health watchdog itself) from the scan. Other
routines are what it exists to watch; the watchdog's own health has to come
from somewhere else (a human noticing cadence drift, or a future sibling
routine on a different seat).

Action on a finding stays close to this routine's existing report-on-
exception doctrine (DAN-289/DAN-385/DAN-491), with one deliberate addition
the DAN-666 proposal asks for -- a wake, not just a record:
  - Dedupe on the routine (a stable `[routineId]` marker in a tracking
    issue's title, same pattern as seat_watchdog.py's per-seat tracker):
    a brand-new finding files a tracking issue assigned to THIS watchdog's
    own owner (visibility only -- same posture as every other arm) and
    `[@mention](agent://...)`s the affected routine's owning agent so they
    are woken to self-trigger it (`POST /api/routines/{id}/run`, the
    pattern used on DAN-664/DAN-665), per the DAN-666 proposal.
  - A still-open tracker found again proves the condition has now persisted
    across at least two consecutive watchdog ticks -- the DAN-666 proposal's
    own escalation trigger -- so that comment additionally mentions Cloud.
  - Repeat notifications on an already-open tracker are throttled to once
    per `RENOTIFY_INTERVAL_SECONDS` (1h) via a marker-timestamp check (the
    same `should_poke` shape as stranded_review.py), so an unresolved
    finding does not get a fresh ping (and a fresh Cloud wake) every single
    15-minute tick forever.
  - Only `"filed"` counts toward this tick's own ACTIONABLE signal (DAN-491
    narrowing) -- an `"updated"`/`"resumed"`/`"throttled"` action is a
    repeat of an already-known, already-reported finding.
  - No cap on resumes (unlike seat_watchdog.py's MAX_AUTO_RESUMES) -- this
    is a first cut of a new arm; add that hardening later if a live
    thrashing tracker is ever observed, the same incremental pattern this
    whole routine has followed arm-by-arm.

Usage:
  PAPERCLIP_API_KEY=... PAPERCLIP_API_URL=... PAPERCLIP_COMPANY_ID=... \
      python3 ops/routine_checkout_watchdog.py [--limit 5] \
      [--post-comment-on ISSUE_ID] [--dry-run] [--post] \
      [--file-findings --project-id PID --assignee-agent-id AID]

Exit code is always 0 -- this script only reports, it never signals failure
of the routines it is watching.
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# This watchdog's own routine id -- excluded from the scan. See module
# docstring for why self-detection here is undecidable by construction.
SELF_ROUTINE_ID = "6c72d65a-5220-49a7-a81d-6dc7db32aebc"

STALE_MULTIPLIER = 2.0
RENOTIFY_INTERVAL_SECONDS = 60 * 60  # 1h cap on repeat pings for an unresolved finding
OPEN_STATUSES = frozenset({"todo", "in_progress", "in_review", "blocked"})
ACTIONABLE_ACTIONS = frozenset({"filed"})
MARKER = "<!-- routine-checkout-watchdog:v1 -->"

# Heartbeat-run statuses that mean a run is finished one way or another --
# used to recognize the DAN-680 "queued and never started" wedge shape,
# which is anything NOT in this set with both `startedAt`/`claimedAt` null.
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "timed_out"})

# Cloud, CEO -- DAN-666's own escalation target once a finding has persisted
# past a second consecutive watchdog tick. Same id used by
# stranded_review.py's ESCALATION_AGENT_ID.
ESCALATION_AGENT_ID = "c28db8ef-7f04-4c21-b575-ddee819e050a"


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


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def schedule_trigger(routine):
    """First enabled, non-archived `schedule` trigger on `routine`, or None
    if it has none (webhook/api-only routines are out of scope -- there is
    no cadence to compare an elapsed streak against)."""
    for trigger in routine.get("triggers") or []:
        if trigger.get("kind") == "schedule" and trigger.get("enabled") and not trigger.get("archived"):
            return trigger
    return None


def cadence_seconds(trigger):
    """The routine's exact configured cadence, read directly off its
    trigger as `nextRunAt - lastFiredAt` -- the platform has already
    resolved whatever cron expression is configured (fixed-interval,
    hourly-offset, daily, ...), so there is no need to parse the cron
    string here. None if either timestamp is missing or the computed
    cadence is non-positive (e.g. a trigger that has never fired)."""
    last = parse_ts(trigger.get("lastFiredAt"))
    nxt = parse_ts(trigger.get("nextRunAt"))
    if last is None or nxt is None:
        return None
    delta = (nxt - last).total_seconds()
    return delta if delta > 0 else None


def no_checkout_miss(issue):
    """True if `issue` (a fresh GET /api/issues/{id} response) is
    `in_progress` but was never checked out at all -- both lock fields
    null. This is the "never checked out", not "slow" shape DAN-645 and
    DAN-666 are both about."""
    return (
        issue is not None
        and issue.get("status") == "in_progress"
        and issue.get("checkoutRunId") is None
        and issue.get("executionRunId") is None
    )


def no_checkout_streak(runs, fetch_issue):
    """Walks `runs` (newest-first, as returned by
    GET /api/routines/{id}/runs) and returns the leading unbroken run of
    no-checkout misses -- (streak_runs, oldest_triggered_at), newest-first,
    or ([], None) if the newest run is not itself a miss.

    Stops at the first run that is not a miss (terminal, or genuinely
    checked out) or that has no `linkedIssueId` at all (a coalesced run
    with nothing of its own to evaluate). Each linked issue is fetched
    fresh via `fetch_issue` rather than trusting the run's embedded
    `linkedIssue.status`, which can lag live state.

    A run whose tick was coalesced into another run can share the same
    `linkedIssueId` as its neighbour in the streak -- that linked issue
    then appears twice in the returned list. This only affects the
    displayed run count, not the elapsed-time threshold below (which is
    computed from timestamps, not from how many runs are in the list), so
    it is left as a known cosmetic simplification rather than solved here.
    """
    streak = []
    for run in runs:
        linked_issue_id = run.get("linkedIssueId")
        if not linked_issue_id:
            break
        issue = fetch_issue(linked_issue_id)
        if not no_checkout_miss(issue):
            break
        streak.append(run)
    if not streak:
        return [], None
    oldest = parse_ts(streak[-1].get("triggeredAt"))
    return streak, oldest


def queued_never_started(run):
    """True if `run` (a fresh `GET /api/heartbeat-runs/{id}` response) is
    non-terminal and has never started -- both `startedAt` and `claimedAt`
    null. This is the DAN-680 shape: a queued run that sat behind seat
    occupancy for ~3h before ever dispatching. Terminality is read off
    `status` (see `TERMINAL_RUN_STATUSES`), never inferred from the two
    null timestamps alone -- a long-queued run on an occupied single-
    concurrency seat is indistinguishable from dead by those two fields by
    themselves (DAN-693/DAN-694)."""
    return (
        run is not None
        and run.get("status") not in TERMINAL_RUN_STATUSES
        and run.get("startedAt") is None
        and run.get("claimedAt") is None
    )


def wedged_checkout_miss(issue, run):
    """True if `issue` is the DAN-680 wedge shape that `no_checkout_miss`
    cannot see: `in_progress`, never checked out itself
    (`checkoutRunId is None`), but held by a queued-and-never-started run
    through `executionRunId` rather than being genuinely absent. A queued
    run holds `executionRunId` non-null on its target issue for the entire
    time it sits queued -- the same mechanism behind the standing `409
    Issue run ownership conflict` rule -- so `executionRunId` being set does
    not by itself mean the issue was ever checked out; `run` (the fresh
    `GET /api/heartbeat-runs/{id}` resolution of that id) is what decides
    it. Deliberately excludes `checked_out_issue()`'s shape: a genuinely
    checked-out issue also has `checkoutRunId` set, which this requires to
    be null."""
    return (
        issue is not None
        and issue.get("status") == "in_progress"
        and issue.get("checkoutRunId") is None
        and issue.get("executionRunId") is not None
        and queued_never_started(run)
    )


def wedged_checkout_streak(runs, fetch_issue, fetch_run):
    """Sibling of `no_checkout_streak` for the DAN-680 wedge shape: walks
    `runs` newest-first and returns the leading unbroken run of
    wedged-checkout misses, (streak_runs, oldest_triggered_at) newest-first,
    or ([], None) if the newest run is not itself a wedge. Each linked
    issue's `executionRunId` is resolved fresh via `fetch_run` rather than
    trusting its mere presence -- that is exactly what distinguishes this
    shape from a genuine checkout."""
    streak = []
    for run_entry in runs:
        linked_issue_id = run_entry.get("linkedIssueId")
        if not linked_issue_id:
            break
        issue = fetch_issue(linked_issue_id)
        if issue is None or issue.get("status") != "in_progress" or issue.get("checkoutRunId") is not None:
            break
        execution_run_id = issue.get("executionRunId")
        if not execution_run_id:
            break
        run = fetch_run(execution_run_id)
        if not wedged_checkout_miss(issue, run):
            break
        streak.append(run_entry)
    if not streak:
        return [], None
    oldest = parse_ts(streak[-1].get("triggeredAt"))
    return streak, oldest


def seat_occupancy_counts(seat_runs):
    """Running vs queued counts across `seat_runs` (a seat's own run list,
    as returned by `GET /api/companies/{id}/heartbeat-runs?agentId=...`).
    A queued run on an occupied single-concurrency seat is a seat-occupancy
    fact, not evidence the queued run itself is dead (DAN-693/DAN-694) --
    this is what lets a wedge finding say "seat busy" instead of implying
    the stuck run is stuck forever."""
    return {
        "running": sum(1 for r in seat_runs if r.get("status") == "running"),
        "queued": sum(1 for r in seat_runs if r.get("status") == "queued"),
    }


def shared_quota_boundary(run, other_seat_runs):
    """If `run` (the stuck queued-never-started run) is itself a retry
    successor (`retryOfRunId` set) carrying a `scheduledRetryAt`, and that
    exact instant is also carried by a DIFFERENT agent's run in
    `other_seat_runs`, this is a company-wide quota/clock reset boundary
    (DAN-694), not evidence this routine's seat alone is broken. Returns
    the shared ISO timestamp string, or None when there is no such run, no
    `scheduledRetryAt`, or no match on another seat."""
    if run is None or not run.get("retryOfRunId"):
        return None
    scheduled = run.get("scheduledRetryAt")
    if not scheduled:
        return None
    own_agent = run.get("agentId")
    for other in other_seat_runs:
        if other.get("scheduledRetryAt") == scheduled and other.get("agentId") != own_agent:
            return scheduled
    return None


def evaluate_routine(
    routine, trigger, runs, fetch_issue, now,
    fetch_run=None, fetch_seat_runs=None, fetch_company_runs=None,
):
    """Returns a finding dict or None. `trigger` must already be known to
    be an enabled schedule trigger (see `schedule_trigger`).

    `fetch_run` is optional and opts into the DAN-680 wedge arm
    (`wedged_checkout_streak`) alongside the original no-checkout arm
    (`no_checkout_streak`); omitting it (the default) preserves the
    original DAN-666 behaviour exactly. At most one of the two streaks can
    be non-empty for a given run list, because they require mutually
    exclusive shapes of the newest run's linked issue (`executionRunId`
    null vs. set-and-resolving-to-queued-never-started), so there is no
    ambiguity in preferring whichever is non-empty.

    `fetch_seat_runs` and `fetch_company_runs` are optional and, only when
    the wedge arm actually fires, attach seat-occupancy and shared-quota
    context (see `seat_occupancy_counts`/`shared_quota_boundary`) onto the
    returned finding's `wedge_context` so `describe_finding` can report
    seat-busy/quota-boundary framing instead of implying the queued run is
    dead (DAN-693/DAN-694)."""
    cadence = cadence_seconds(trigger)
    if cadence is None:
        return None

    streak, oldest = no_checkout_streak(runs, fetch_issue)
    kind = "no_checkout"
    if not streak and fetch_run is not None:
        streak, oldest = wedged_checkout_streak(runs, fetch_issue, fetch_run)
        kind = "wedged"

    if not streak or oldest is None:
        return None
    elapsed = (now - oldest).total_seconds()
    if elapsed < STALE_MULTIPLIER * cadence:
        return None

    finding = {
        "routine": routine,
        "cadence_seconds": cadence,
        "streak": streak,
        "oldest_triggered_at": oldest,
        "elapsed_seconds": elapsed,
        "kind": kind,
    }

    if kind == "wedged":
        newest_issue = fetch_issue(streak[0].get("linkedIssueId"))
        wedge_run = fetch_run(newest_issue.get("executionRunId")) if newest_issue else None
        seat_agent_id = wedge_run.get("agentId") if wedge_run else None
        wedge_context = {"seat_agent_id": seat_agent_id}
        if seat_agent_id and fetch_seat_runs is not None:
            try:
                wedge_context.update(seat_occupancy_counts(fetch_seat_runs(seat_agent_id) or []))
            except Exception:
                pass
        if wedge_run is not None and fetch_company_runs is not None:
            try:
                wedge_context["quota_boundary_ts"] = shared_quota_boundary(wedge_run, fetch_company_runs() or [])
            except Exception:
                pass
        finding["wedge_context"] = wedge_context

    return finding


def compute_findings(
    routines, fetch_runs, fetch_issue, now, self_routine_id=SELF_ROUTINE_ID,
    fetch_run=None, fetch_seat_runs=None, fetch_company_runs=None,
):
    findings = []
    for routine in routines:
        if routine.get("id") == self_routine_id:
            continue
        if routine.get("status") != "active":
            continue
        trigger = schedule_trigger(routine)
        if trigger is None:
            continue
        runs = fetch_runs(routine["id"])
        finding = evaluate_routine(
            routine, trigger, runs, fetch_issue, now,
            fetch_run=fetch_run, fetch_seat_runs=fetch_seat_runs, fetch_company_runs=fetch_company_runs,
        )
        if finding is not None:
            findings.append(finding)
    return findings


def agent_names(company_id):
    try:
        agents = api_get(f"/api/companies/{company_id}/agents")
        return {a["id"]: a.get("name") or a.get("displayName") or a["id"] for a in agents}
    except Exception:
        return {}


def finding_marker(routine_id):
    """Stable per-routine dedupe tag embedded in the tracking issue's title."""
    return f"[{routine_id}]"


def finding_title(finding):
    routine = finding["routine"]
    n = len(finding["streak"])
    return (
        f"Routine checkout watchdog: {routine.get('title', routine['id'])} -- "
        f"{n} unchecked-out tick(s) {finding_marker(routine['id'])}"
    )


def is_tracking_issue(issue, routine_id):
    """True if `issue` is this routine's own tracking issue -- matched on
    the stable `[routineId]` marker in the title, not the human-readable
    count or label, both of which change between updates."""
    return finding_marker(routine_id) in (issue.get("title") or "")


def find_existing_tracking_issue(company_id, routine_id, api_get_fn=api_get):
    """Searches this watchdog's own issues for `routine_id`'s tracking
    issue, across ALL statuses (a closed tracker still must be found so a
    recurrence is treated as a resume rather than a brand-new finding).
    Among multiple matches, an open one wins over a closed one; among ties,
    the earliest-created wins -- same tie-break rule as
    seat_watchdog.py's `_pick_canonical_match`, applied here without the
    full duplicate-reconciliation machinery that earned its complexity from
    a specific live race (DAN-531); add that hardening here too if a live
    duplicate is ever observed."""
    listing = api_get_fn(
        f"/api/companies/{company_id}/issues"
        f"?q={urllib.parse.quote(finding_marker(routine_id))}&limit=50"
    )
    items = listing if isinstance(listing, list) else listing.get("issues", listing.get("data", []))
    matches = [item for item in items if is_tracking_issue(item, routine_id)]
    if not matches:
        return None
    open_matches = [m for m in matches if m.get("status") in OPEN_STATUSES]
    pool = open_matches if open_matches else matches
    return min(pool, key=lambda m: m.get("createdAt") or m.get("updatedAt") or "")


def _fmt_elapsed(seconds):
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def _owner_mention(routine, names):
    owner_id = routine.get("assigneeAgentId")
    if not owner_id:
        return "**no owner**"
    label = names.get(owner_id, owner_id)
    return f"[@{label}](agent://{owner_id})"


def describe_finding(finding, names, escalate=False):
    """The comment/description body for a finding. `escalate=True` adds a
    Cloud mention -- used once a tracker is found already open, proving
    this has persisted past a second consecutive watchdog tick (DAN-666's
    own escalation trigger)."""
    routine = finding["routine"]
    routine_id = routine["id"]
    routine_label = routine.get("title", routine_id)
    streak = finding["streak"]
    cadence = finding["cadence_seconds"]
    elapsed = finding["elapsed_seconds"]
    owner_mention = _owner_mention(routine, names)

    is_wedged = finding.get("kind") == "wedged"
    if is_wedged:
        summary = (
            f"{owner_mention} -- **{routine_label}** (`{routine_id}`) has {len(streak)} consecutive "
            f"tick(s) wedged behind a queued-and-never-started run (`status: in_progress`, "
            f"`checkoutRunId` null, but `executionRunId` points at a run that is non-terminal with "
            f"both `startedAt` and `claimedAt` null -- the [DAN-680](/DAN/issues/DAN-680) shape), "
            f"spanning {_fmt_elapsed(elapsed)} against a routine cadence of ~{_fmt_elapsed(cadence)} "
            f"(threshold {STALE_MULTIPLIER:.0f}x = {_fmt_elapsed(STALE_MULTIPLIER * cadence)})."
        )
    else:
        summary = (
            f"{owner_mention} -- **{routine_label}** (`{routine_id}`) has {len(streak)} consecutive "
            f"tick(s) that were never checked out (`status: in_progress` with both `checkoutRunId` "
            f"and `executionRunId` null), spanning {_fmt_elapsed(elapsed)} against a routine cadence "
            f"of ~{_fmt_elapsed(cadence)} (threshold {STALE_MULTIPLIER:.0f}x = "
            f"{_fmt_elapsed(STALE_MULTIPLIER * cadence)})."
        )

    lines = [
        f"## Routine checkout watchdog -- {routine_label} {MARKER}",
        "",
        summary,
        "",
        "Affected ticks, newest-first:",
    ]
    for run in streak:
        linked = run.get("linkedIssue") or {}
        label = linked.get("identifier") or run.get("linkedIssueId")
        lines.append(f"- [{label}](/DAN/issues/{label}) triggered {run.get('triggeredAt')}")

    if is_wedged:
        ctx = finding.get("wedge_context") or {}
        seat_agent_id = ctx.get("seat_agent_id")
        running = ctx.get("running")
        queued = ctx.get("queued")
        if seat_agent_id and running is not None and queued is not None:
            seat_label = names.get(seat_agent_id, seat_agent_id)
            lines += [
                "",
                f"Owning seat [@{seat_label}](agent://{seat_agent_id}) currently has {running} "
                f"running / {queued} queued run(s). A queued run behind seat occupancy is not by "
                "itself evidence that it is dead "
                "([DAN-693](/DAN/issues/DAN-693)/[DAN-694](/DAN/issues/DAN-694)) -- do not restart "
                "the seat on this finding alone.",
            ]
        quota_ts = ctx.get("quota_boundary_ts")
        if quota_ts:
            lines += [
                "",
                f"Its `scheduledRetryAt` (`{quota_ts}`) is also carried by another seat's run -- this "
                "looks like a company-wide quota/clock reset boundary, not a failure specific to "
                "this routine or seat.",
            ]
        lines += [
            "",
            f"Self-trigger to clear the backlog once the queued run finally dispatches (or resolves): "
            f"`POST /api/routines/{routine_id}/run` (per the pattern used on "
            "[DAN-664](/DAN/issues/DAN-664)/[DAN-665](/DAN/issues/DAN-665)).",
        ]
    else:
        lines += [
            "",
            f"Self-trigger to clear the backlog and let its own orphan-guard run: "
            f"`POST /api/routines/{routine_id}/run` (per the pattern used on "
            "[DAN-664](/DAN/issues/DAN-664)/[DAN-665](/DAN/issues/DAN-665)), then investigate why the "
            "schedule fire itself is not reaching checkout -- see [DAN-645](/DAN/issues/DAN-645) for "
            "the ACP-adapter root cause found for the same shape on other seats.",
        ]

    if escalate:
        ceo_mention = f"[@{names.get(ESCALATION_AGENT_ID, 'Cloud')}](agent://{ESCALATION_AGENT_ID})"
        lines += [
            "",
            f"{ceo_mention} -- this has now been flagged on at least two consecutive watchdog "
            "ticks without clearing; escalating per [DAN-666](/DAN/issues/DAN-666)'s own trigger.",
        ]
    return "\n".join(lines)


def _last_marker_ts(comments):
    last = None
    for comment in comments:
        if MARKER not in (comment.get("body") or ""):
            continue
        ts = parse_ts(comment.get("createdAt"))
        if ts is not None and (last is None or ts > last):
            last = ts
    return last


def should_notify(comments, now):
    """False if a marker comment on the tracker was already posted within
    RENOTIFY_INTERVAL_SECONDS of `now` -- caps repeat pings (and repeat
    Cloud wakes) on a still-unresolved finding to once per window, same
    shape as stranded_review.py's `should_poke`."""
    last = _last_marker_ts(comments)
    if last is None:
        return True
    return (now - last).total_seconds() >= RENOTIFY_INTERVAL_SECONDS


def is_actionable(action):
    return action in ACTIONABLE_ACTIONS


def file_or_update_finding(company_id, finding, names, project_id, assignee_agent_id, now,
                            api_get_fn=api_get, api_post_fn=api_post, api_patch_fn=api_patch):
    """Dedupe on the routine:
      - no existing tracker -> file a new one, owner mention only (first
        occurrence -- nothing to escalate yet).
      - existing tracker is OPEN -> this finding has now been seen on at
        least two consecutive ticks; post an update comment that also
        escalates to Cloud, throttled to once per RENOTIFY_INTERVAL_SECONDS.
      - existing tracker is CLOSED -> recurrence; resume it (reopen to
        `todo`) with an escalating comment -- a closed tracker already
        proved this condition was seen before, so a fresh instance still
        counts as persisting, not as brand new.
    Returns (issue, action) with action in
    {"filed", "updated", "resumed", "throttled"}.
    """
    routine_id = finding["routine"]["id"]
    existing = find_existing_tracking_issue(company_id, routine_id, api_get_fn)

    if existing is None:
        body = describe_finding(finding, names, escalate=False)
        issue = api_post_fn(
            f"/api/companies/{company_id}/issues",
            {
                "title": finding_title(finding),
                "description": body,
                "projectId": project_id,
                "assigneeAgentId": assignee_agent_id,
                "priority": "high",
                "status": "todo",
            },
        )
        return issue, "filed"

    comments = api_get_fn(f"/api/issues/{existing['id']}/comments")
    comments = comments if isinstance(comments, list) else comments.get("comments", comments.get("data", []))

    if not should_notify(comments, now):
        return existing, "throttled"

    body = describe_finding(finding, names, escalate=True)
    if existing.get("status") in OPEN_STATUSES:
        api_post_fn(f"/api/issues/{existing['id']}/comments", {"body": body})
        return existing, "updated"

    api_patch_fn(
        f"/api/issues/{existing['id']}",
        {"status": "todo", "comment": body, "resume": True},
    )
    return existing, "resumed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5, help="Runs to pull per routine, newest-first")
    parser.add_argument("--post-comment-on", help="Issue id for the roll-up comment (this run's own issue)")
    parser.add_argument("--dry-run", action="store_true", help="Print findings, never post")
    parser.add_argument("--post", action="store_true", help="Explicit opt-in: actually post the roll-up comment")
    parser.add_argument(
        "--file-findings", action="store_true",
        help="File-or-update a per-routine tracking issue for each finding (DAN-666)",
    )
    parser.add_argument("--project-id", help="Required with --file-findings")
    parser.add_argument("--assignee-agent-id", help="Required with --file-findings; the watchdog's own owner")
    parser.add_argument("--self-routine-id", default=SELF_ROUTINE_ID)
    args = parser.parse_args()
    if args.file_findings and not args.dry_run and not (args.project_id and args.assignee_agent_id):
        parser.error("--file-findings requires --project-id and --assignee-agent-id")

    company_id = os.environ["PAPERCLIP_COMPANY_ID"]
    now = datetime.now(timezone.utc)

    routines = api_get(f"/api/companies/{company_id}/routines")

    issue_cache = {}

    def fetch_issue(issue_id):
        if issue_id not in issue_cache:
            try:
                issue_cache[issue_id] = api_get(f"/api/issues/{issue_id}")
            except Exception:
                issue_cache[issue_id] = None
        return issue_cache[issue_id]

    def fetch_runs(routine_id):
        return api_get(f"/api/routines/{routine_id}/runs?limit={args.limit}")

    run_cache = {}

    def fetch_run(run_id):
        if run_id not in run_cache:
            try:
                run_cache[run_id] = api_get(f"/api/heartbeat-runs/{run_id}")
            except Exception:
                run_cache[run_id] = None
        return run_cache[run_id]

    def _runs_list(payload):
        return payload if isinstance(payload, list) else payload.get("runs", payload.get("data", []))

    def fetch_seat_runs(seat_agent_id):
        return _runs_list(api_get(f"/api/companies/{company_id}/heartbeat-runs?limit=300&agentId={seat_agent_id}"))

    company_runs_cache = {}

    def fetch_company_runs():
        if "runs" not in company_runs_cache:
            company_runs_cache["runs"] = _runs_list(api_get(f"/api/companies/{company_id}/heartbeat-runs?limit=300"))
        return company_runs_cache["runs"]

    findings = compute_findings(
        routines, fetch_runs, fetch_issue, now, args.self_routine_id,
        fetch_run=fetch_run, fetch_seat_runs=fetch_seat_runs, fetch_company_runs=fetch_company_runs,
    )

    if not findings:
        print("No cross-routine no-checkout streaks found.")
        return 0

    names = agent_names(company_id)
    preview = "## Routine checkout watchdog -- visibility only\n\n" + "\n\n".join(
        describe_finding(f, names, escalate=False) for f in findings
    )
    print(preview)

    if args.file_findings and not args.dry_run:
        actions = []
        for finding in findings:
            issue, action = file_or_update_finding(
                company_id, finding, names, args.project_id, args.assignee_agent_id, now,
            )
            actions.append(action)
            label = issue.get("identifier", issue.get("id"))
            print(f"\n{action} {label} for routine {finding['routine'].get('title')}.")
        print(f"\nACTIONABLE: {'yes' if any(is_actionable(a) for a in actions) else 'no'}")
    elif args.file_findings:
        for finding in findings:
            existing = find_existing_tracking_issue(company_id, finding["routine"]["id"])
            action = "filed" if existing is None else (
                "updated" if existing.get("status") in OPEN_STATUSES else "resumed"
            )
            print(f"\n[dry-run] would {action} tracking issue for routine {finding['routine'].get('title')}.")
        print(f"\nACTIONABLE: {'yes' if any(f for f in findings) else 'no'}")

    if args.post_comment_on and args.post and not args.dry_run:
        api_post(f"/api/issues/{args.post_comment_on}/comments", {"body": preview})
        print(f"\nPosted visibility comment on {args.post_comment_on}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
