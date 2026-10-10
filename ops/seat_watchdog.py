#!/usr/bin/env python3
"""Seat-health watchdog, narrow arm -- DAN-218 (design: DAN-215 trigger-spec,
narrowed by the 2026-10-03 DAN-218 wake comment).

Fires visibility-only when a seat (agentId) racks up 3+ consecutive
non-succeeded, non-cancelled runs that produce NO NEW EVIDENCE -- the
crash-before-first-tool-call `acpx_turn_failed`/`process_lost`/etc shape --
spanning >=10 minutes first-to-latest. It is NOT a blanket "3 failures"
counter: a non-succeeded run that still produced its own narrative result
(the DAN-171 shape: ran a real probe, posted a dispositive comment, even at
$0 and even several times in a row) is explicitly exempt and does not count
toward, or reset, the streak. Only a `succeeded` run resets the streak.

`"...terminal limit failure."` is excluded entirely -- that quota-exhaustion
fault already has an owned triage path, ops/limit-triage.py.

Action on a qualifying streak is VISIBILITY ONLY: no status change on any
OTHER issue, no escalation to the affected seat's owner, no auto-block. That
DAN-218 narrowing still holds. What changed under DAN-289 (report-on-
exception) is only how the finding is *recorded*: instead of a comment that
gets buried in this tick's own execution issue and lost the moment that
issue closes, a qualifying finding is filed as -- or updates -- a single
tracking issue owned by this watchdog itself (`--file-findings`), titled
with the actual finding and tagged with a stable `[agentId]` marker so a
repeat finding on the same seat updates that issue instead of spawning a
sibling. This is still not an escalation: the tracking issue is assigned to
the watchdog's own owner, not the affected seat, and nothing about the
affected seat's own tickets is touched.

DAN-289 review (changes requested) found that the first cut of the above
still re-filed a sibling the moment a tracker closed: `find_existing_
finding_issue` only searched OPEN tracking issues, so a closed tracker's
already-reported streak -- which stays inside the `--limit` lookback window
until it ages out -- looked brand new on the very next tick (DAN-329 ->
closed -> DAN-336 re-filed the identical historical evidence 5 minutes
later; see DAN-339). Fixed by:
  - `find_existing_finding_issue` now searches ALL statuses, not just the
    open ones, so a closed tracker for the same seat is still found.
  - Each run this watchdog has already reported is covered by a persistent
    per-seat high-water mark -- not a separate store, but read back out of
    the tracking issue's own description/comments (every reported run's
    timestamp is already embedded there in `describe_finding`'s bullet
    list). `filter_already_covered` drops any run at or before that
    covered-cutoff before streak detection runs, so a closed tracker's
    evidence can never requalify -- only genuinely new runs after the
    cutoff can.
  - If a genuinely new streak appears for a seat whose tracker is closed,
    that tracker is resumed (`resume: true`, reopened to `todo`) rather
    than a sibling filed.

DAN-339 changes-requested round: the fix above stopped the new-ticket-on-
close path but not a second, distinct thrash path it left open -- `resume`
fired on EVERY tick with genuinely-new-by-timestamp evidence, even when that
evidence was just another instance of the exact same already-diagnosed,
non-actionable root cause (e.g. the `issues_open_routine_execution_uq`
platform collision from DAN-329). A tracker closed with that finding would
get yanked back to `todo`, re-closed, yanked back again, etc. -- the same
reopen/update noise this ticket exists to stop, just without a new DAN
number. Fixed by throttling auto-resume: `count_prior_resumes` counts how
many times a tracker has already been auto-reopened (via the `## New
occurrence (resumed)` comment marker `file_or_update_finding` itself posts
on resume). Once a closed tracker has hit `MAX_AUTO_RESUMES`, further new
streaks are recorded as a comment on the still-closed issue instead of
flipping its status -- visible, but no more status thrash. A human (or a
future tick after a genuine fix) can still reopen it manually; this only
throttles the watchdog's own auto-resume.

DAN-491: DAN-385 requirement 6 moved `noted_closed` off this tick's own
ACTIONABLE signal but left `updated` and `resumed` on it, so a tick whose
only activity was a comment on an already-open tracker (`updated`) or
flipping an already-known tracker back open (`resumed`) still kept this
tick's own execution issue `done` -- live evidence on DAN-468/DAN-482, zero
new findings for a human to read, exactly the pattern DAN-289 was filed to
stop. `ACTIONABLE_ACTIONS` now holds only `"filed"`: the single outcome
that puts a tracking issue in front of a human for the first time.

DAN-526: a real escalation (DAN-337) read several of this watchdog's own
"## Update" comments on the SAME tracker, stitched together across many
hours, and concluded "14 hours, zero succeeded runs" for a seat that had in
fact succeeded 28 times in that span and had already self-recovered 49
minutes before the comment citing it was written. Each individual comment's
3-4-run sample was accurate; the problem is that neither the sample nor the
comment said anything about what happened *around* it. Fixed by attaching,
to every finding, the full per-seat denominator over the same fetched
window used for detection (`agent_window_stats`) and a check for a later
`succeeded` run for that seat (`find_recovery`) -- both computed from the
UNFILTERED run list, since `filter_already_covered` deliberately drops
already-reported history and must not become the source of truth for "did
this seat ever succeed". `describe_finding` now always states the
denominator and labels a finding "intermittent" (successes exist in-window)
or "self-recovered" (a later success already landed) rather than reading as
an undifferentiated, ongoing, zero-success outage. A brand-new finding that
is already self-recovered by emit time does not mint a tracker at all
(`"self_recovered_noted"`, never `ACTIONABLE`) -- the whole point of filing
is to put a live incident in front of a human, and this one is already
over.

DAN-531: `find_existing_finding_issue`'s dedupe is a read-then-write with no
atomicity, so two concurrent ticks can both miss (or both hit the same
match) and then both write -- live data showed exactly this: two seats each
with two CLOSED trackers, filed ~8-9min apart. DAN-526's earliest-created
tie-break already makes a closed-duplicate pair harmless (the original
filing always wins, regardless of which one this watchdog touches next).
The open case was not covered: two freshly-raced OPEN trackers would hand
the same self-reinforcing bias DAN-526 fixed for closed matches right back
-- whichever this watchdog picks first keeps winning (every pick posts an
update), permanently orphaning the other. Fixed by (1) the same earliest-
created tie-break for open matches (`_pick_canonical_match`), and (2)
`reconcile_duplicate_trackers`, called from every `file_or_update_finding`
write path, which closes down any OTHER open match for the seat once a
canonical one is known -- merging a race-produced duplicate after the fact
rather than adding a lock (no documented idempotency-key support exists on
the issue-create endpoint to prevent the race up front).

Usage:
  PAPERCLIP_API_KEY=... PAPERCLIP_API_URL=... PAPERCLIP_COMPANY_ID=... \
      python3 ops/seat_watchdog.py [--limit 300] [--post-comment-on ISSUE_ID] [--dry-run]
      [--file-findings --project-id PID --assignee-agent-id AID]

Exit code is always 0 -- this script only reports, it never signals failure
of the seats it is watching.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

QUALIFYING_COUNT = 3
MIN_SPAN_SECONDS = 10 * 60  # trigger-spec §2: >=10min first-to-latest guards against fast retry bursts
LIMIT_FAILURE_MARKER = "terminal limit failure"  # owned by ops/limit-triage.py -- excluded here
FINDING_OPEN_STATUSES = ("todo", "in_progress", "in_review", "blocked")
RESUME_MARKER = "## New occurrence (resumed)"  # tag `file_or_update_finding` posts whenever it auto-resumes a closed tracker
MAX_AUTO_RESUMES = 1  # DAN-339: after this many auto-resumes, repeat instances of the same closed, already-diagnosed finding are only commented, never used to flip status again

# Matches a `describe_finding` bullet line, e.g.
#   - `run-123` 2026-10-03T21:45:06.304000+00:00 process_lost: Process lost...
# Used to recover the per-seat high-water mark from a tracking issue's own
# description/comments -- no separate state store needed.
RUN_LINE_RE = re.compile(
    r"`[^`]+`\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+\d{2}:\d{2}|Z))"
)


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


def api_post(path, body):
    base = os.environ["PAPERCLIP_API_URL"].rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + os.environ["PAPERCLIP_API_KEY"],
            "Content-Type": "application/json",
            "X-Paperclip-Run-Id": os.environ.get("PAPERCLIP_RUN_ID", ""),
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def api_patch(path, body):
    base = os.environ["PAPERCLIP_API_URL"].rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method="PATCH",
        headers={
            "Authorization": "Bearer " + os.environ["PAPERCLIP_API_KEY"],
            "Content-Type": "application/json",
            "X-Paperclip-Run-Id": os.environ.get("PAPERCLIP_RUN_ID", ""),
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def run_ts(run):
    return parse_ts(run.get("startedAt") or run.get("createdAt"))


def classify_run(run):
    """Returns one of: 'ignore' (non-terminal), 'reset' (succeeded),
    'transparent' (cancelled, limit-failure, or produced-new-evidence --
    skip, affects nothing), 'bare_crash' (counts toward the streak)."""
    status = run.get("status")
    if status in ("queued", "running"):
        return "ignore"
    if status == "cancelled":
        return "transparent"
    if status == "succeeded":
        return "reset"

    error = run.get("error") or ""
    if LIMIT_FAILURE_MARKER in error:
        return "transparent"

    result_json = run.get("resultJson")
    summary = (result_json or {}).get("summary") if isinstance(result_json, dict) else None
    # A bare crash-before-first-tool-call run has no model-authored summary at
    # all, or the harness just echoed the raw error back as the "summary" --
    # i.e. the model never got far enough to say anything of its own. Any
    # distinct, model-authored summary text means the run did real work
    # (probed something, posted a comment) before ending non-succeeded --
    # the DAN-171 shape -- and is exempt.
    if summary is None or summary == error:
        return "bare_crash"
    return "transparent"


def compute_findings(runs):
    """runs: list of heartbeat-run dicts (any mix of agents/issues).
    Returns a list of findings, each a dict with agentId and the 3+ run
    dicts that make up the qualifying streak. Fires at most once per
    contiguous streak (streak resets only on a `succeeded` run for that
    agent), so a 14-run bare-crash band produces exactly one finding, not
    one per run past the threshold.
    """
    by_agent = {}
    for run in runs:
        by_agent.setdefault(run["agentId"], []).append(run)

    findings = []
    for agent_id, agent_runs in by_agent.items():
        ordered = sorted((r for r in agent_runs if run_ts(r) is not None), key=run_ts)
        streak = []
        fired = False
        for run in ordered:
            cls = classify_run(run)
            if cls == "ignore" or cls == "transparent":
                continue
            if cls == "reset":
                streak = []
                fired = False
                continue
            # bare_crash
            streak.append(run)
            if (
                not fired
                and len(streak) >= QUALIFYING_COUNT
                and (run_ts(streak[-1]) - run_ts(streak[0])).total_seconds() >= MIN_SPAN_SECONDS
            ):
                findings.append({"agentId": agent_id, "runs": list(streak)})
                fired = True
    return findings


def agent_window_stats(agent_id, all_runs):
    """DAN-526 denominator: succeeded/failed/cancelled counts for `agent_id`
    across the SAME unfiltered run list used for streak detection this tick
    (i.e. the same wall-clock window the watchdog is already looking at),
    plus that window's first/last timestamp and up to 5 sample succeeded-run
    timestamps. Must be computed from the unfiltered fetch, never from the
    `filter_already_covered` output -- that filter exists to stop a streak
    re-qualifying, and already drops the very successes this exists to
    surface. Returns None if `agent_id` has no timestamped runs at all."""
    agent_runs = sorted(
        (r for r in all_runs if r.get("agentId") == agent_id and run_ts(r) is not None),
        key=run_ts,
    )
    if not agent_runs:
        return None
    succeeded = [r for r in agent_runs if r.get("status") == "succeeded"]
    cancelled = [r for r in agent_runs if r.get("status") == "cancelled"]
    failed = [r for r in agent_runs if r.get("status") not in ("succeeded", "cancelled", "queued", "running")]
    return {
        "window_start": run_ts(agent_runs[0]),
        "window_end": run_ts(agent_runs[-1]),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "cancelled": len(cancelled),
        "succeeded_timestamps": [run_ts(r) for r in succeeded[:5]],
    }


def find_recovery(finding, all_runs):
    """DAN-526 self-recovery check: the earliest `succeeded` run for this
    finding's seat strictly after the streak's own last run, read from the
    UNFILTERED run list (never from `filter_already_covered`'s output, for
    the same reason as `agent_window_stats`). Returns that timestamp, or
    None if the seat has not succeeded again since this streak ended --
    i.e. whether, by the time this finding is about to be described or
    filed, the incident it describes is already over."""
    agent_id = finding["agentId"]
    streak_end = run_ts(finding["runs"][-1])
    later_successes = sorted(
        ts
        for r in all_runs
        if r.get("agentId") == agent_id and r.get("status") == "succeeded"
        and (ts := run_ts(r)) is not None and ts > streak_end
    )
    return later_successes[0] if later_successes else None


def true_streak_extent(finding, all_runs):
    """DAN-777: `finding["runs"]` is the 3+-run sample `compute_findings`
    froze at the moment the streak first qualified -- correct for its own
    fires-once-per-streak dedup, but it stops growing even though the real
    streak keeps extending for every subsequent bare-crash run until a
    `succeeded` run resets it. This re-walks the UNFILTERED run list (same
    reason as `agent_window_stats`/`find_recovery`: `filter_already_
    covered`'s output must never be the source here) starting at the
    finding's own first run, re-applying `classify_run`'s exact streak
    rules, to find the TRUE current count/span of that same contiguous
    streak -- for display only; it never changes `finding["runs"]` itself.
    Returns None if the finding's first run cannot be located in
    `all_runs` (should not happen: `all_runs` is always a superset of
    whatever `compute_findings` was run against)."""
    agent_id = finding["agentId"]
    start_id = finding["runs"][0].get("id")
    ordered = sorted(
        (r for r in all_runs if r.get("agentId") == agent_id and run_ts(r) is not None),
        key=run_ts,
    )
    start_index = next((i for i, r in enumerate(ordered) if r.get("id") == start_id), None)
    if start_index is None:
        return None

    streak = []
    ongoing = True
    for run in ordered[start_index:]:
        cls = classify_run(run)
        if cls in ("ignore", "transparent"):
            continue
        if cls == "reset":
            ongoing = False
            break
        streak.append(run)  # bare_crash
    if not streak:
        return None
    return {
        "count": len(streak),
        "start": run_ts(streak[0]),
        "end": run_ts(streak[-1]),
        "ongoing": ongoing,
    }


def describe_finding(finding, agent_names=None, window_stats=None, recovered_at=None, all_runs=None):
    names = agent_names or {}
    agent_label = names.get(finding["agentId"], finding["agentId"])
    runs = finding["runs"]
    first, last = run_ts(runs[0]), run_ts(runs[-1])
    status_label = (
        "self-recovered" if recovered_at is not None
        else "intermittent" if window_stats and window_stats["succeeded"] > 0
        else "ongoing"
    )
    # DAN-777: when the unfiltered run list is available, report the TRUE
    # current extent of this streak in the headline instead of the frozen
    # sample above -- the frozen sample stays below as the bulleted list,
    # it just stops being what the headline's count/span are computed
    # from. Falls back to the frozen sample when `all_runs` isn't passed
    # (direct/unit-test callers that only have the finding itself).
    extent = true_streak_extent(finding, all_runs) if all_runs is not None else None
    headline_count = extent["count"] if extent else len(runs)
    headline_first = extent["start"] if extent else first
    headline_last = extent["end"] if extent else last
    lines = [
        f"Seat **{agent_label}** ({finding['agentId']}) has {headline_count} consecutive "
        f"non-succeeded, no-new-evidence runs spanning "
        f"{(headline_last - headline_first).total_seconds() / 60:.0f} minutes "
        f"({headline_first.isoformat()} → {headline_last.isoformat()}) -- **{status_label}**:",
    ]
    if extent and extent["count"] > len(runs):
        lines.append(
            f"(Showing the first {len(runs)} runs of this streak below -- it "
            f"{'is still ongoing as of this fetch' if extent['ongoing'] else 'continued until the reset below'} "
            f"and reached {extent['count']} runs by {extent['end'].isoformat()}.)"
        )
    for r in runs:
        lines.append(
            f"- `{r.get('id')}` {run_ts(r).isoformat()} [{r.get('invocationSource') or 'unknown'}] "
            f"{r.get('errorCode')}: {r.get('error')}"
        )
    # DAN-526 acceptance #1/#2: the denominator and per-run invocationSource
    # above are what let a reader of this ONE comment tell a real outage
    # apart from a streak embedded in an otherwise-healthy seat, without
    # having to stitch together every other comment this watchdog has ever
    # posted for the same tracker.
    if window_stats:
        lines.append(
            f"\nWindow totals for this seat ({window_stats['window_start'].isoformat()} → "
            f"{window_stats['window_end'].isoformat()}): **{window_stats['succeeded']} succeeded** / "
            f"{window_stats['failed']} failed / {window_stats['cancelled']} cancelled. This is the "
            "denominator for the streak above -- do not read the streak as a zero-success outage "
            "without checking this line."
        )
        if window_stats["succeeded"] > 0:
            sample = ", ".join(ts.isoformat() for ts in window_stats["succeeded_timestamps"])
            lines.append(f"Succeeded runs for this seat in this window include: {sample}.")
    if recovered_at is not None:
        minutes_since = (recovered_at - last).total_seconds() / 60
        lines.append(
            f"\n**Self-recovered**: this seat succeeded again at {recovered_at.isoformat()} "
            f"({minutes_since:.0f} min after the streak above ended), before this finding was "
            "ever reported. This is historical evidence of a resolved streak, not an ongoing outage."
        )
    return "\n".join(lines)


def agent_names(company_id):
    try:
        agents = api_get(f"/api/companies/{company_id}/agents")
        return {a["id"]: a.get("name") or a.get("displayName") or a["id"] for a in agents}
    except Exception:
        return {}


def finding_marker(agent_id):
    """Stable per-seat dedupe tag embedded in the tracking issue's title."""
    return f"[{agent_id}]"


def finding_title(finding, agent_names=None, recovered_at=None):
    names = agent_names or {}
    label = names.get(finding["agentId"], finding["agentId"])
    n = len(finding["runs"])
    suffix = " (self-recovered)" if recovered_at is not None else ""
    return (
        f"Seat-health: {label} -- {n} consecutive no-evidence crashes{suffix} "
        f"{finding_marker(finding['agentId'])}"
    )


def is_finding_tracking_issue(issue, agent_id):
    """True if `issue` is this seat's open tracking issue -- matched on the
    stable `[agentId]` marker in the title, not on the human-readable label
    or count, both of which change between re-pokes."""
    return finding_marker(agent_id) in (issue.get("title") or "")


def _finding_matches(company_id, agent_id, api_get_fn=api_get):
    """Raw list of this watchdog's own issues carrying `agent_id`'s exact
    `[agentId]` marker in the title, across ALL statuses, in whatever order
    the search endpoint returns them -- no picking, no tie-break. Split out
    of `find_existing_finding_issue` (DAN-531) so `reconcile_duplicate_
    trackers` can see every match a race may have produced, not just the
    one canonical pick."""
    listing = api_get_fn(
        f"/api/companies/{company_id}/issues"
        f"?q={urllib.parse.quote(finding_marker(agent_id))}&limit=50"
    )
    items = listing if isinstance(listing, list) else listing.get("issues", listing.get("data", []))
    return [item for item in items if is_finding_tracking_issue(item, agent_id)]


def _pick_canonical_match(matches):
    """Of `matches` (same seat marker, any statuses), picks the one
    `find_existing_finding_issue` treats as canonical: an open one wins over
    a closed one; among ties, the EARLIEST-created wins. Returns None for an
    empty list.

    DAN-526: closed-match tie-break used to pick the most-recently-UPDATED
    one. Live incident: a race once produced two closed trackers for the
    same seat/marker, DAN-330 and DAN-337. Picking "most recently updated"
    turns that into a self-reinforcing loop -- resuming/commenting on a
    tracker is itself an update, so whichever duplicate this function picks
    becomes more likely to be picked again next time, forever, regardless
    of which one is actually canonical. DAN-337 got reopened and
    re-commented on repeatedly (while a human kept re-closing it as a
    duplicate of DAN-330) for exactly this reason. Earliest-created breaks
    the loop: the original filing is always preferred, independent of how
    many times either has been touched since.

    DAN-531: the open-match tie-break had the identical flaw (most-recently-
    updated), just never triggered by a live incident yet -- two freshly
    raced OPEN trackers would otherwise let whichever one this function
    first picks keep winning forever (every pick posts an update, which
    only entrenches it further), silently orphaning the other as a
    never-touched duplicate. Earliest-created for the open tie-break closes
    that gap the same way, and is also what `reconcile_duplicate_trackers`
    treats as the survivor when it merges a race-produced duplicate down."""
    if not matches:
        return None
    open_matches = [m for m in matches if m.get("status") in FINDING_OPEN_STATUSES]
    pool = open_matches if open_matches else matches
    return min(pool, key=lambda m: m.get("createdAt") or m.get("updatedAt") or "")


def find_existing_finding_issue(company_id, agent_id, api_get_fn=api_get):
    """Searches this watchdog's own issues for `agent_id`'s tracking issue,
    across ALL statuses -- a closed tracker still must be found so its
    already-reported streak doesn't look brand new on the next tick (DAN-339:
    searching only open statuses let a tracker that had just closed get
    re-filed as a fresh sibling for the identical historical evidence).
    Scoped to the company's issue-search endpoint, filtered client side on
    the exact marker so an unrelated title substring match can't misfire.
    See `_pick_canonical_match` for the tie-break when multiple matches
    exist."""
    return _pick_canonical_match(_finding_matches(company_id, agent_id, api_get_fn))


def reconcile_duplicate_trackers(company_id, agent_id, canonical, api_get_fn=api_get,
                                  api_patch_fn=api_patch):
    """DAN-531: closes every OPEN tracking issue for `agent_id` other than
    `canonical`.

    The dedupe this watchdog relies on (`find_existing_finding_issue`) is a
    read-then-write with no atomicity: two concurrent ticks can both query
    it, both get back the same answer (or both get None), and both then
    file or resume independently, producing two live trackers for one seat.
    Live data showed this happening with two CLOSED trackers per seat
    (DAN-330/DAN-337, DAN-329/DAN-336) -- already harmless, since
    `_pick_canonical_match` deterministically prefers the earliest-created
    one regardless. The open case is worse: left alone, the duplicate this
    function doesn't pick as canonical would never be touched again by this
    watchdog and would sit open forever.

    Rather than adding a lock or an idempotency key (the issue-create
    endpoint's request schema does not document support for one), this
    repairs the symptom on the next write this watchdog already makes:
    whenever more than one OPEN match for the seat's marker exists, every
    one besides `canonical` is closed (`status: cancelled`) with a comment
    pointing at it. Closing removes a reconciled duplicate from
    `FINDING_OPEN_STATUSES`, so it is never reconsidered on a later tick --
    no separate "already reconciled" marker needed. Already-closed
    historical duplicates are left alone; this only merges down live,
    currently-open siblings."""
    matches = _finding_matches(company_id, agent_id, api_get_fn)
    extra_open = [
        m for m in matches
        if m.get("status") in FINDING_OPEN_STATUSES and m.get("id") != canonical.get("id")
    ]
    label = canonical.get("identifier") or canonical.get("id")
    for dup in extra_open:
        api_patch_fn(
            f"/api/issues/{dup['id']}",
            {
                "status": "cancelled",
                "comment": (
                    "## Duplicate tracker (DAN-531)\n\n"
                    f"A concurrent watchdog tick raced this issue's filing or resume against "
                    f"{label}, which this watchdog's dedupe treats as canonical for this seat "
                    "(earliest-created). Closing this sibling so only one live tracker exists "
                    f"per seat; see {label} for the ongoing record."
                ),
            },
        )


def covered_cutoff(issue, api_get_fn=api_get):
    """The latest run timestamp already reported by `issue` (open or
    closed) -- recovered from its own description plus every comment,
    since `describe_finding` already embeds each covered run's timestamp
    in a `` `run-id` <iso-ts> `` bullet. Runs at or before this timestamp
    are already covered by this seat's finding and must not requalify just
    because they are still inside the lookback window. Returns None when
    `issue` is None or carries no parseable run lines."""
    if issue is None:
        return None
    texts = [issue.get("description") or ""]
    try:
        comments = api_get_fn(f"/api/issues/{issue['id']}/comments")
        items = comments if isinstance(comments, list) else comments.get("comments", comments.get("data", []))
        texts.extend(c.get("body") or "" for c in items)
    except Exception:
        pass
    timestamps = [
        ts
        for text in texts
        for m in RUN_LINE_RE.finditer(text)
        if (ts := parse_ts(m.group(1))) is not None
    ]
    return max(timestamps) if timestamps else None


def filter_already_covered(runs, company_id, api_get_fn=api_get):
    """Drops any run already covered by an existing (open OR closed)
    tracking issue for its seat, per `covered_cutoff`. This is what stops a
    resolved streak from re-qualifying once its tracker closes -- a closed
    tracker's evidence can never fire again; only genuinely new runs after
    its high-water mark can start a fresh streak."""
    cutoffs = {}
    kept = []
    for run in runs:
        agent_id = run["agentId"]
        if agent_id not in cutoffs:
            existing = find_existing_finding_issue(company_id, agent_id, api_get_fn)
            cutoffs[agent_id] = covered_cutoff(existing, api_get_fn)
        cutoff = cutoffs[agent_id]
        ts = run_ts(run)
        if cutoff is not None and ts is not None and ts <= cutoff:
            continue
        kept.append(run)
    return kept


def count_prior_resumes(issue, api_get_fn=api_get):
    """How many times this tracker has already been auto-resumed by this
    watchdog, counted from its own comment history via `RESUME_MARKER` --
    no separate store needed, same trick as `covered_cutoff`. Used to
    throttle further auto-resumes once a closed tracker's root cause is
    already diagnosed and repeat instances are just more of the same
    non-actionable evidence (DAN-339)."""
    if issue is None:
        return 0
    try:
        comments = api_get_fn(f"/api/issues/{issue['id']}/comments")
        items = comments if isinstance(comments, list) else comments.get("comments", comments.get("data", []))
    except Exception:
        return 0
    return sum(1 for c in items if RESUME_MARKER in (c.get("body") or ""))


# DAN-491 (closing the gap DAN-385 requirement 6 only half-closed): which
# file_or_update_finding outcomes are worth minting/keeping a fresh per-tick
# execution issue for, versus a quiet restatement against a tracker whose
# own state a human can already read elsewhere. Only "filed" creates a
# tracking issue a human has never seen before -- "updated" only adds a
# comment to a tracker that is already open and already on someone's radar,
# "resumed" only flips an already-known tracker's status back open (the
# tracker itself carries the news, not this tick), and "noted_closed"
# changes nothing at all. None of the latter three justify this tick's own
# execution issue surviving as `done`; live evidence on DAN-491 (DAN-468,
# DAN-482) showed "updated"/"resumed"-only ticks closing `done` with zero
# new findings for a human to read.
ACTIONABLE_ACTIONS = frozenset({"filed"})


def is_actionable(action):
    """True if `action` (file_or_update_finding's/predict_action's return)
    should count toward this tick's own execution issue per DAN-491."""
    return action in ACTIONABLE_ACTIONS


def predict_action(company_id, finding, api_get_fn=api_get, recovered_at=None):
    """What `file_or_update_finding` would do for `finding`, without
    writing anything -- same decision logic, read-only. Lets `--dry-run
    --file-findings` report accurate per-finding actions and the tick-level
    ACTIONABLE signal before anything is actually posted.

    DAN-526: when no tracker exists yet AND the seat has already succeeded
    again since this streak ended (`recovered_at` is not None), do not file
    a brand-new tracker at all -- filing is how this watchdog puts a live
    incident in front of a human for the first time, and an incident that
    is already over by the time it is discovered is not that. Only the
    "no existing tracker" branch is affected: a streak that resumes or
    updates an EXISTING tracker still does so (the tracker already has a
    human's attention; the recovery info lands in the comment body via
    `describe_finding`, not by suppressing the write)."""
    existing = find_existing_finding_issue(company_id, finding["agentId"], api_get_fn)
    if existing is None:
        if recovered_at is not None:
            return "self_recovered_noted"
        return "filed"
    if existing.get("status") in FINDING_OPEN_STATUSES:
        return "updated"
    if count_prior_resumes(existing, api_get_fn) >= MAX_AUTO_RESUMES:
        return "noted_closed"
    if recovered_at is not None:
        # DAN-526: this is the literal DAN-337 shape -- a closed tracker,
        # genuinely-new-by-timestamp evidence, but the seat had ALREADY
        # succeeded again before this tick ever ran. Flipping status back
        # to `todo` for an incident that is already over is what produced
        # the done -> todo -> done thrash a human had to escalate. Comment
        # only; never resume status for an already-recovered streak.
        return "self_recovered_not_resumed"
    return "resumed"


def file_or_update_finding(company_id, finding, names, project_id, assignee_agent_id,
                            api_get_fn=api_get, api_post_fn=api_post, api_patch_fn=api_patch,
                            window_stats=None, recovered_at=None, all_runs=None):
    """Dedupe on the finding (the seat), not the run:
      - an open tracking issue for this agentId gets an update comment;
      - a CLOSED tracking issue for this agentId is resumed (`resume: true`,
        reopened to `todo`) rather than a sibling filed -- reachable here
        only when `filter_already_covered` has already proven the evidence
        is genuinely new (past the closed tracker's high-water mark) -- but
        only up to `MAX_AUTO_RESUMES` times; past that, the tracker has
        already demonstrated its root cause is diagnosed-and-closed rather
        than actually fixed, so further repeat instances are recorded as a
        comment on the still-closed issue instead of thrashing its status
        again (DAN-339 changes-requested round);
      - a CLOSED tracking issue whose streak has ALREADY self-recovered by
        emit time (DAN-526) gets a comment only -- status is never flipped
        back to `todo` for an incident that is already over (this is the
        literal DAN-337 done -> todo -> done thrash);
      - a brand-new, already-self-recovered finding (DAN-526) is not filed
        at all -- see `predict_action`;
      - otherwise a new issue is filed, titled with the actual finding.
    Returns (issue, "filed"|"updated"|"resumed"|"noted_closed"
             |"self_recovered_noted"|"self_recovered_not_resumed"),
    with `issue` None for "self_recovered_noted" since nothing is written.

    DAN-531: whichever issue this call treats as canonical for the seat
    (`existing` if one was found, else the one just filed) also gets
    `reconcile_duplicate_trackers` run against it, merging down any OPEN
    sibling a concurrent tick raced into existence for the same seat."""
    body = describe_finding(finding, names, window_stats=window_stats, recovered_at=recovered_at, all_runs=all_runs)
    existing = find_existing_finding_issue(company_id, finding["agentId"], api_get_fn)
    action = predict_action(company_id, finding, api_get_fn, recovered_at=recovered_at)
    if existing is not None:
        reconcile_duplicate_trackers(company_id, finding["agentId"], existing, api_get_fn, api_patch_fn)
        if action == "updated":
            api_post_fn(f"/api/issues/{existing['id']}/comments", {"body": "## Update\n\n" + body})
            return existing, "updated"
        if action == "noted_closed":
            api_post_fn(
                f"/api/issues/{existing['id']}/comments",
                {
                    "body": "## New occurrence (tracker stays closed)\n\n" + body
                    + f"\n\nThis tracker has already been auto-resumed {MAX_AUTO_RESUMES} "
                    "time(s) for repeat instances of this same root cause. Recording this "
                    "occurrence here without reopening status to avoid thrash; reopen "
                    "manually if this needs fresh attention.",
                },
            )
            return existing, "noted_closed"
        if action == "self_recovered_not_resumed":
            api_post_fn(
                f"/api/issues/{existing['id']}/comments",
                {
                    "body": "## New occurrence (already self-recovered, not reopened)\n\n" + body
                    + "\n\nNot reopening status: this seat already succeeded again before this "
                    "tick ran, so the incident above is historical, not ongoing. Reopen manually "
                    "if this needs fresh attention.",
                },
            )
            return existing, "self_recovered_not_resumed"
        api_patch_fn(
            f"/api/issues/{existing['id']}",
            {
                "status": "todo",
                "comment": RESUME_MARKER + "\n\n" + body,
                "resume": True,
            },
        )
        return existing, "resumed"

    if action == "self_recovered_noted":
        return None, "self_recovered_noted"

    issue = api_post_fn(
        f"/api/companies/{company_id}/issues",
        {
            "title": finding_title(finding, names, recovered_at=recovered_at),
            "description": body,
            "projectId": project_id,
            "assigneeAgentId": assignee_agent_id,
            "priority": "medium",
            "status": "todo",
        },
    )
    # DAN-531: a concurrent tick can file its own tracker for this seat in
    # the gap between this tick's find_existing_finding_issue miss (above)
    # and this POST landing. Re-check now that a write has happened -- if a
    # race-created sibling is now visible, whichever of the two is
    # earliest-created is canonical (may not be the one just filed above),
    # and the other is closed as a duplicate.
    canonical = find_existing_finding_issue(company_id, finding["agentId"], api_get_fn) or issue
    reconcile_duplicate_trackers(company_id, finding["agentId"], canonical, api_get_fn, api_patch_fn)
    return canonical, "filed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--post-comment-on", help="Issue id to post a visibility comment on")
    parser.add_argument("--dry-run", action="store_true", help="Print findings, never post")
    parser.add_argument(
        "--file-findings", action="store_true",
        help="File-or-update a per-seat tracking issue for each finding (DAN-289)",
    )
    parser.add_argument("--project-id", help="Required with --file-findings")
    parser.add_argument("--assignee-agent-id", help="Required with --file-findings; the watchdog's own owner")
    args = parser.parse_args()
    if args.file_findings and not args.dry_run and not (args.project_id and args.assignee_agent_id):
        parser.error("--file-findings requires --project-id and --assignee-agent-id")

    company_id = os.environ["PAPERCLIP_COMPANY_ID"]
    # DAN-526: keep the UNFILTERED fetch around. `filter_already_covered`
    # drops runs this watchdog has already reported so streak detection
    # can't requalify stale evidence -- exactly right for detection, but
    # wrong as the source for "did this seat ever succeed in this window"
    # or "has it recovered since". Both of those must see everything.
    all_runs = api_get(f"/api/companies/{company_id}/heartbeat-runs?limit={args.limit}")
    runs = filter_already_covered(all_runs, company_id)
    findings = compute_findings(runs)

    if not findings:
        print("No qualifying bare-crash streaks found.")
        return 0

    names = agent_names(company_id)
    enriched = [
        (f, agent_window_stats(f["agentId"], all_runs), find_recovery(f, all_runs))
        for f in findings
    ]
    body = "## Seat-health watchdog (narrow arm) -- visibility only\n\n" + "\n\n".join(
        describe_finding(f, names, window_stats=ws, recovered_at=rec, all_runs=all_runs)
        for f, ws, rec in enriched
    )
    print(body)

    if args.file_findings and not args.dry_run:
        actions = []
        for finding, window_stats, recovered_at in enriched:
            issue, action = file_or_update_finding(
                company_id, finding, names, args.project_id, args.assignee_agent_id,
                window_stats=window_stats, recovered_at=recovered_at, all_runs=all_runs,
            )
            actions.append(action)
            label = issue.get("identifier", issue.get("id")) if issue else "(no tracker filed)"
            print(f"\n{action} {label} for seat {finding['agentId']}.")
        print(f"\nACTIONABLE: {'yes' if any(is_actionable(a) for a in actions) else 'no'}")
    elif args.file_findings:
        actions = []
        for finding, window_stats, recovered_at in enriched:
            action = predict_action(company_id, finding, recovered_at=recovered_at)
            actions.append(action)
            print(
                f"\n[dry-run] would {action} tracking issue titled: "
                f"{finding_title(finding, names, recovered_at=recovered_at)}"
            )
        print(f"\nACTIONABLE: {'yes' if any(is_actionable(a) for a in actions) else 'no'}")

    if args.post_comment_on and not args.dry_run:
        api_post(f"/api/issues/{args.post_comment_on}/comments", {"body": body})
        print(f"\nPosted visibility comment on {args.post_comment_on}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
