#!/usr/bin/env python3
"""Stranded-approval-stage detector -- DAN-267, corrected by DAN-380, DAN-385,
DAN-727 (folding in DAN-918).

Fires visibility-only when an `in_review` issue's execution-policy approval
stage is alive (`executionState.status == "pending"`, a non-null
`currentParticipant`) but **no run is driving it** -- the run that should
hand the stage back to its participant died (`recovery_needed`/`failed`/
`cancelled`), or there simply is no `activeRun` at all. This script never
changes status, reassigns, or creates an escalation issue -- see DAN-267 and
its parent DAN-256 for the two board-only/unavailable paths that were ruled
out before landing on "post a comment."

DAN-380: the original version always addressed the finding to the
participant with "this stage is yours to move" and asserted "nothing else
will move this -- a non-participant PATCH gets a 422" -- neither claim was
ever checked against the participant's actual reachability, and on
DAN-213/DAN-236 it poked a seat whose every wake on the issue was
`deferred_issue_execution` with `claimedAt: null` (never claimed), twice
each, 8.4h apart. Before naming/instructing the participant, the script now
reads `GET /api/issues/{id}/diagnostics/wakes`: if the most recent wake
addressed to the participant on this issue was never claimed, the finding
escalates to the CEO seat with that evidence instead of re-poking a seat
that cannot see the poke. Only when the participant *is* reachable does it
fall back to naming/notifying them, and even then without the unverified
absolute claims.

DAN-385 (both approver follow-ups): a 6h re-poke window throttles the
per-issue poke comment so a still-stranded issue is not re-reported every
15-minute tick, but a finding whose facts got *worse* inside that window
(e.g. participant flips reachable -> unreachable) must still force a
re-poke -- a `classification_fingerprint()` embedded in each posted comment
lets `should_poke()` detect that. The fingerprint deliberately excludes the
`dropped`-wake count, because posting a poke itself generates a wake to the
same participant, which gets dropped too when they are unreachable -- a
field the detector's own writes can move cannot be a valid change signal.

DAN-727 (folding in DAN-918) -- two independent false-escalation bugs found
in the DAN-380 fix, both against live `in_review` issues the detector was
actually watching:

  Defect 1 (DAN-551, 2026-10-07): `wake_reachability()` treated the single
  most recent wake to the participant on an issue as dispositive with no
  regard for its age. On DAN-551 that wake was requested 2026-10-05
  01:01:05.904Z -- ~2 days before the detector's read -- and was the *only*
  wake ever addressed to the participant on that issue. The detector
  escalated to the CEO as `unreachable=True`; 87 seconds later the
  escalation comment's own `issue_commented` wake was claimed and the stage
  was approved normally. A lone multi-day-old unclaimed wake is residue, not
  current evidence of unreachability -- see `wake_reachability()`'s `stale`
  flag below, gated on `STALE_WAKE_EVIDENCE_SECONDS`.

  Defect 2 (DAN-858, 2026-10-08, reported as DAN-918 and folded in here):
  staleness did not apply -- the cited wake was genuinely the only one ever
  addressed to the participant and genuinely never claimed -- yet the
  participant (Minos) had 11 *succeeded* runs across 11 other issues in the
  45 minutes before the escalation fired. `deferred_issue_execution` is a
  fact about the *issue* (it was execution-locked by another run at the
  moment the wake was enqueued and nothing re-enqueued it -- DAN-919), not
  about the *seat*. The detector had no seat-level evidence either way and
  escalated anyway. Fixed by corroborating a dropped on-issue wake against
  the participant's recent `GET .../heartbeat-runs?agentId=...` activity
  (`classify_seat_reachability()`): a seat with a recent succeeded or
  running heartbeat run is reachable regardless of what one issue's wake
  history says, and the finding is labelled a **stranded wake**, not an
  unreachable participant.

  Both defects share a root cause this fix generalizes rather than
  special-cases: a `deferred_issue_execution`/unclaimed wake is *consistent
  with* unreachability but does not *prove* it, and the fix in both cases is
  to require corroborating evidence (age for DAN-551, seat activity for
  DAN-858) before the strongest claim and the most expensive remedy (CEO
  escalation) are used.

  `evaluate_issue()`/`classify_finding()` now names three strand classes,
  each with its own remedy, instead of one `unreachable` boolean:

    - `reconciliation_hold`: the per-issue GET's `executionBlocker` is
      non-null (see DAN-496/held_issue_sweep.py and the standing "Wedged
      issues" note). Only a comment from a human board user clears this --
      an agent's own poke comment is a documented no-op
      (`admitExplicitNativeContinuation` gates on `actorType === "user"`).
      This detector still only posts a comment (it has no other channel),
      addressed to the CEO so a human continuation comment can be
      requested; it never recommends stage dissolution or a board
      `stalled-review-decision` call for what is still a live participant.
    - `unreachable_participant`: the on-issue wake evidence is fresh (not
      `stale`) AND seat evidence corroborates no recent activity. Per
      requirement 5 below, this still pokes first -- CEO escalation is only
      the second step, taken when a prior poke demonstrably produced no
      claimed wake.
    - `stranded_wake`: the on-issue wake looks dropped, but either it is
      `stale` (DAN-551) or seat evidence shows the participant is otherwise
      active (DAN-858). Always just a poke -- the corroborating evidence
      says the seat is fine, so an ordinary comment is expected to generate
      a wake that gets claimed, exactly as it did 0.3 seconds after DAN-551's
      own (mis-targeted) escalation comment.
    - `reachable` (the `strand_class` used when none of the above apply):
      the pre-DAN-380 default path, unchanged.

  Acceptance criteria satisfied:
    1/2. `wake_reachability()` returns `stale` and the cited event's age +
         `runId`; a stale sole-unclaimed wake cannot by itself support
         `unreachable_participant`, and the text always states the age.
    3. `_common_evidence_lines()` labels `permittedActions`/execution
       phase/lock fields as describing the named run, not the seat.
    4. `refresh_is_resolved()` re-reads the issue immediately before any
       post and suppresses it if the issue is already done/cancelled, its
       `executionState.status == "completed"`, it has a non-null
       `lastDecisionOutcome`, or `currentStageId` is null -- closing the
       read-to-post race that let DAN-551's comment land after the thing it
       reported.
    5. `decide_strand_action()` always returns `"poke"` for a first
       encounter of `unreachable_participant`; `"escalate"` only once a
       prior poke (found via `ACTION_MARKER` in comment history) produced
       no claimed wake within `MIN_PENDING_SECONDS` of being posted.
    6. See `test_stranded_review.py`'s DAN-551 replay fixtures.
    7. DAN-496's `held_issue_sweep.py` does **not** share this bug: it keys
       "held" purely on the per-issue GET's `executionBlocker` field (an
       authoritative signal, independent of status/assignee), and only uses
       wake history for a descriptive `held_since` timestamp -- never as
       the thing that decides whether the issue is held. No fix needed
       there.

Action on a finding is a wake or an escalation, nothing else: one comment on
the stranded issue (addressed to the participant if reachable, to the CEO
seat if not), plus a roll-up comment on this run's own issue
(`--post-comment-on`) naming every finding and its strand class. A
per-issue idempotency marker caps re-pokes to once per
`REPOKE_INTERVAL_SECONDS` (6h) so a still-stranded issue is not poked every
15-minute tick, unless its `classification_fingerprint()` (now keyed on
`strand_class`+`action`+`can_advance`) changed since the last posted marker.

Usage:
  PAPERCLIP_API_KEY=... PAPERCLIP_API_URL=... PAPERCLIP_COMPANY_ID=... \
      python3 ops/stranded_review.py [--limit 300] [--post-comment-on ISSUE_ID] [--dry-run] [--post]

Posting (both the per-issue pokes and the roll-up) only happens when
`--post` is given AND `--dry-run` is not. Without `--post`, the script only
ever prints -- that is the default, not an opt-out.

Exit code is always 0 -- this script only reports, it never signals failure
of the issues it is watching.
"""
import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

MIN_PENDING_SECONDS = 30 * 60
REPOKE_INTERVAL_SECONDS = 6 * 60 * 60
DEAD_RUN_PHASES = frozenset({"failed", "recovery_needed", "cancelled"})
MARKER = "<!-- stranded-review-detector:v1 -->"
# DAN-385 approver follow-up: embeds classification_fingerprint() in each
# posted poke/escalation comment so should_poke() can detect a finding that
# got worse inside the 6h repoke window, not just one that is still recent.
FINGERPRINT_MARKER = "<!-- stranded-review-fingerprint:"
# DAN-727 req 5: records which action (poke|escalate) a marker comment took,
# so decide_strand_action() can tell "we already tried a poke" from "this is
# the first time we've seen this finding".
ACTION_MARKER = "<!-- stranded-review-action:"
# Cloud, CEO -- DAN-380 escalation target when the participant is unreachable.
ESCALATION_AGENT_ID = "c28db8ef-7f04-4c21-b575-ddee819e050a"
# The only permittedActions value observed on a dead run (DAN-213/236/293,
# 2026-10-04) -- never a stage-advancing action. See module docstring.
NON_ADVANCING_ACTIONS = frozenset({"inspect_run"})
# A wake the participant was never woken to see: queued, never claimed.
DROPPED_WAKE_STATUS = "deferred_issue_execution"
# DAN-727 req 1/2: a lone unclaimed wake older than this cannot by itself
# support `unreachable_participant` -- it is residue, not current evidence.
# DAN-551's sole wake was ~2 days (172800s) old; DAN-858's was 69ms old and
# still wrong for an unrelated reason (seat evidence, handled separately).
# 2 hours is comfortably above any normal tick/claim latency and comfortably
# below "stale by days".
STALE_WAKE_EVIDENCE_SECONDS = 2 * 60 * 60
# DAN-727 (DAN-918): corroborating seat-level evidence window. 300 matches
# the limit already established as honouring the `agentId` filter (DAN-337,
# DAN-694).
SEAT_RUNS_LIMIT = 300
SEAT_SUCCESS_STATUSES = frozenset({"succeeded"})
SEAT_RUNNING_STATUSES = frozenset({"running"})


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


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def pending_since(record):
    """The `execution_participant` reviewAttention path's `since`, falling
    back to the issue's `lastActivityAt` (list-endpoint-only field) if that
    path is missing. Returns None if neither is available."""
    for path in (record.get("reviewAttention") or {}).get("paths") or []:
        if path.get("kind") == "execution_participant":
            ts = parse_ts(path.get("since"))
            if ts is not None:
                return ts
    return parse_ts(record.get("lastActivityAt"))


def no_live_run(record):
    """True if nothing is currently driving the issue's execution state."""
    if record.get("activeRun") is None:
        return True
    execution = (record.get("execution") or {}).get("execution") or {}
    return execution.get("phase") in DEAD_RUN_PHASES


def execution_facts(record):
    """The `execution` key of GET /api/issues/{id}/execution, or {} if the
    call failed or was never made."""
    return (record.get("execution") or {}).get("execution") or {}


def can_advance_via_execution(record):
    """True if GET .../execution's `permittedActions` names something
    beyond `inspect_run` -- i.e. a verified, run-level action is actually
    available. This describes the named run only (DAN-727 req 3), not the
    participant's seat."""
    actions = execution_facts(record).get("permittedActions") or []
    return bool(set(actions) - NON_ADVANCING_ACTIONS)


def wake_reachability(record, participant_agent_id, now):
    """Classifies the on-issue wake evidence for `participant_agent_id`,
    from the `wakes` key (GET .../diagnostics/wakes payload, `events`
    newest-first).

    Returns a dict:
      unreachable: the most recent wake addressed to this agent on this
        issue is `DROPPED_WAKE_STATUS` with `claimedAt` null -- queued,
        never claimed.
      dropped: how many such wakes in a row (from the most recent) share
        that shape -- evidence text only, never a change-detection key
        (DAN-385 second approver follow-up: this detector's own poke
        comments move this count).
      stale: True when `unreachable` is True but the cited wake is older
        than STALE_WAKE_EVIDENCE_SECONDS (DAN-727/DAN-551) -- a sole old
        unclaimed wake is residue, not current proof of unreachability.
      newest_event: the most recent wake event addressed to this agent on
        this issue, or None -- carries `requestedAt`/`runId` for the
        evidence text (DAN-727 req 2).

    No wake history for this agent, or a most-recent wake that was claimed,
    means reachable (`unreachable=False`, `stale=False`).
    """
    if participant_agent_id is None:
        return {"unreachable": False, "dropped": 0, "stale": False, "newest_event": None}
    events = [
        event
        for event in (record.get("wakes") or {}).get("events") or []
        if event.get("agentId") == participant_agent_id
    ]
    if not events:
        return {"unreachable": False, "dropped": 0, "stale": False, "newest_event": None}
    newest = events[0]
    if not (newest.get("status") == DROPPED_WAKE_STATUS and newest.get("claimedAt") is None):
        return {"unreachable": False, "dropped": 0, "stale": False, "newest_event": newest}
    dropped = 0
    for event in events:
        if event.get("status") == DROPPED_WAKE_STATUS and event.get("claimedAt") is None:
            dropped += 1
        else:
            break
    requested_at = parse_ts(newest.get("requestedAt"))
    age_seconds = (now - requested_at).total_seconds() if requested_at else None
    stale = age_seconds is None or age_seconds > STALE_WAKE_EVIDENCE_SECONDS
    return {"unreachable": True, "dropped": dropped, "stale": stale, "newest_event": newest}


def classify_seat_reachability(runs, now):
    """DAN-727 (DAN-918): corroborates on-issue wake evidence against the
    participant's own recent heartbeat-run activity, from
    `GET /api/companies/{id}/heartbeat-runs?agentId=...` (the `agentId`
    filter is honoured -- DAN-337/DAN-694). `runs` is that endpoint's raw
    list.

    `reachable` is True given any recent succeeded or currently-running run
    for this agent in the returned window -- a seat that is actively
    completing work on *other* issues is not "unreachable" just because one
    issue's wake was dropped (DAN-858: 11 succeeded runs in 45 minutes,
    escalated anyway).

    `quota_runs` follows each `provider_quota`-failed run to its
    `retryOfRunId` successor (`scheduledRetryAt` lives on the successor, not
    the failed run itself -- DAN-694 half 2) so a shared fleet-wide quota
    reset is not read as evidence the seat is down.
    """
    by_status = Counter(r.get("status") for r in runs)
    quota_runs = []
    for run in runs:
        if run.get("status") == "failed" and run.get("errorCode") == "provider_quota":
            successor = next((r for r in runs if r.get("retryOfRunId") == run.get("id")), None)
            quota_runs.append({"failed_id": run.get("id"), "successor": successor})
    success_timestamps = [
        parse_ts(r.get("finishedAt") or r.get("completedAt"))
        for r in runs
        if r.get("status") in SEAT_SUCCESS_STATUSES
    ]
    success_timestamps = [ts for ts in success_timestamps if ts is not None]
    most_recent_success_at = max(success_timestamps) if success_timestamps else None
    reachable = (
        by_status.get("succeeded", 0) > 0
        or sum(by_status.get(s, 0) for s in SEAT_RUNNING_STATUSES) > 0
    )
    return {
        "succeeded": by_status.get("succeeded", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
        "running": by_status.get("running", 0),
        "queued": by_status.get("queued", 0),
        "most_recent_success_at": most_recent_success_at.isoformat() if most_recent_success_at else None,
        "reachable": reachable,
        "quota_runs": quota_runs,
        "total": len(runs),
    }


def evaluate_issue(record, now):
    """record: a merged dict carrying list-endpoint fields (activeRun,
    lastActivityAt, originKind, status) plus single-issue-GET fields
    (executionState, reviewAttention, checkoutRunId, executionRunId,
    executionLockedAt, executionBlocker) plus an `execution` key holding the
    GET /api/issues/{id}/execution response (or None).

    Returns a finding dict or None.
    """
    if record.get("originKind") == "routine_execution":
        return None
    if record.get("status") != "in_review":
        return None

    execution_state = record.get("executionState")
    if not execution_state or execution_state.get("status") != "pending":
        return None

    participant = execution_state.get("currentParticipant")
    if not participant:
        return None

    if not no_live_run(record):
        return None

    since = pending_since(record)
    if since is None:
        return None
    if (now - since).total_seconds() < MIN_PENDING_SECONDS:
        return None

    return {"record": record, "participant": participant, "since": since}


def compute_findings(records, now):
    findings = []
    for record in records:
        finding = evaluate_issue(record, now)
        if finding is not None:
            findings.append(finding)
    return findings


def classify_finding(finding, now, seat_runs=None):
    """DAN-380/DAN-727: attaches reachability facts to a finding and names
    one of three strand classes (module docstring), so the caller can
    decide the action via `decide_strand_action()`.

    `seat_runs` is the participant's raw heartbeat-run list (already
    fetched, or `None` when unavailable/not fetched -- treated the same as
    "no corroborating evidence", which never upgrades a finding to
    `unreachable_participant` on its own).
    """
    record = finding["record"]
    participant_agent_id = finding["participant"].get("agentId")
    wake = wake_reachability(record, participant_agent_id, now)
    can_advance = can_advance_via_execution(record)
    execution_blocker = record.get("executionBlocker")
    seat = classify_seat_reachability(seat_runs, now) if seat_runs is not None else None

    if execution_blocker:
        strand_class = "reconciliation_hold"
    elif wake["unreachable"] and not wake["stale"] and seat is not None and not seat["reachable"]:
        strand_class = "unreachable_participant"
    elif wake["unreachable"]:
        # Either stale (DAN-551) or seat evidence contradicts it (DAN-858),
        # or no seat evidence was available to corroborate it at all.
        strand_class = "stranded_wake"
    else:
        strand_class = "reachable"

    return {
        "unreachable": strand_class == "unreachable_participant",
        "dropped": wake["dropped"],
        "can_advance": can_advance,
        "strand_class": strand_class,
        "wake": wake,
        "seat": seat,
        "execution_blocker": execution_blocker,
    }


def classification_fingerprint(classification, action):
    """A stable string summarizing the severity-relevant facts of a finding
    plus the action this tick took -- whether a still-pending finding has
    gotten worse (or its recommended action changed) since the last poke
    (DAN-385 approver follow-up; extended for DAN-727's strand classes),
    independent of how long it has been pending.

    Deliberately excludes `classification['dropped']` (DAN-385 second
    approver follow-up) -- see `wake_reachability()`'s docstring. Only
    `strand_class`, `action`, and `can_advance` are included -- none of
    which this detector's own posted comment can move on its own.
    """
    return f"strand={classification['strand_class']},action={action},can_advance={classification['can_advance']}"


def _fingerprint_line(fingerprint):
    return f"{FINGERPRINT_MARKER}{fingerprint} -->"


def _action_line(action):
    return f"{ACTION_MARKER}{action} -->"


def last_marker_fingerprint(comments):
    """Returns (last_marker_ts, last_fingerprint) from the most recent
    comment carrying MARKER. `last_fingerprint` is None when that comment
    predates the fingerprint line (legacy poke) or never matched one."""
    last_marker_ts = None
    last_fingerprint = None
    for comment in comments:
        body = comment.get("body") or ""
        if MARKER not in body:
            continue
        ts = parse_ts(comment.get("createdAt"))
        if ts is None or (last_marker_ts is not None and ts <= last_marker_ts):
            continue
        last_marker_ts = ts
        last_fingerprint = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(FINGERPRINT_MARKER):
                last_fingerprint = line[len(FINGERPRINT_MARKER) :].rstrip(" ->").strip()
    return last_marker_ts, last_fingerprint


def last_action_marker(comments):
    """Returns (last_marker_ts, last_action) from the most recent comment
    carrying MARKER -- `last_action` is `"poke"`/`"escalate"`/None (a
    pre-DAN-727 comment never carried this line). DAN-727 req 5: lets
    `decide_strand_action()` tell "a poke was already tried" from "this is
    the first time this finding has been seen"."""
    last_marker_ts = None
    last_action = None
    for comment in comments:
        body = comment.get("body") or ""
        if MARKER not in body:
            continue
        ts = parse_ts(comment.get("createdAt"))
        if ts is None or (last_marker_ts is not None and ts <= last_marker_ts):
            continue
        last_marker_ts = ts
        last_action = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(ACTION_MARKER):
                last_action = line[len(ACTION_MARKER) :].rstrip(" ->").strip()
    return last_marker_ts, last_action


def decide_strand_action(strand_class, comments, participant_agent_id, record, now):
    """DAN-727 req 5: the remedy ladder. A comment is strictly cheaper than
    a CEO escalation and is empirically the thing that actually unwedges a
    reachable seat (DAN-551: the escalation comment's own generated wake was
    claimed in 0.3s and closed the stage in 87s) -- so it is always tried
    first.

      - `reconciliation_hold`: escalate immediately. Only a human board
        comment clears a hold (`admitExplicitNativeContinuation` gates on
        `actorType === "user"`); this detector's own poke would be an
        inert no-op, so there is no "try a poke first" step here.
      - `unreachable_participant`: poke, unless a *prior* poke (found via
        `ACTION_MARKER`) already ran for at least `MIN_PENDING_SECONDS`
        without producing a claimed wake to the participant -- only then
        does it escalate.
      - everything else (`stranded_wake`, `reachable`): poke.
    """
    if strand_class == "reconciliation_hold":
        return "escalate"
    if strand_class != "unreachable_participant":
        return "poke"

    last_ts, last_action = last_action_marker(comments)
    if last_action != "poke" or last_ts is None:
        return "poke"

    events = [
        event
        for event in (record.get("wakes") or {}).get("events") or []
        if event.get("agentId") == participant_agent_id
    ]
    poke_worked = any(
        event.get("claimedAt") is not None
        and (parse_ts(event.get("requestedAt")) or last_ts) >= last_ts
        for event in events
    )
    if poke_worked:
        return "poke"
    if (now - last_ts).total_seconds() < MIN_PENDING_SECONDS:
        # Give the prior poke's wake a fair chance to be claimed before
        # concluding it failed.
        return "poke"
    return "escalate"


def should_poke(comments, now, fingerprint):
    """False if a marker comment was already posted within
    REPOKE_INTERVAL_SECONDS of `now` AND the finding's classification
    fingerprint has not changed since that poke -- caps re-pokes to once
    per window for an unchanged finding. A changed fingerprint (DAN-385
    approver follow-up: the finding got worse, e.g. the participant flipped
    reachable -> unreachable, or DAN-727: the strand class or recommended
    action changed) forces True regardless of the window. A legacy marker
    comment with no recorded fingerprint cannot be compared, so it falls
    back to the window-only check that applied before this fix."""
    last_marker_ts, last_fingerprint = last_marker_fingerprint(comments)
    if last_marker_ts is None:
        return True
    if last_fingerprint is not None and last_fingerprint != fingerprint:
        return True
    return (now - last_marker_ts).total_seconds() >= REPOKE_INTERVAL_SECONDS


def refresh_is_resolved(issue_id):
    """DAN-727 req 4: re-reads the issue immediately before posting (not
    from the sweep's cached snapshot) and reports True if it has already
    resolved itself in the interim -- closing the read-to-post race that
    let DAN-551's escalation comment land a minute after the stage it
    described had already been approved by the wake that comment itself
    generated."""
    try:
        fresh = api_get(f"/api/issues/{issue_id}")
    except Exception:
        # Can't confirm resolution either way -- do not block the post on an
        # unrelated read failure.
        return False
    if fresh.get("status") in ("done", "cancelled"):
        return True
    execution_state = fresh.get("executionState") or {}
    if execution_state.get("status") == "completed":
        return True
    if execution_state.get("lastDecisionOutcome") is not None:
        return True
    if execution_state.get("currentStageId") is None:
        return True
    return False


def _fmt_elapsed(now, since):
    minutes = (now - since).total_seconds() / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{minutes / 60:.1f}h"


def _fmt_age(now, ts):
    if ts is None:
        return "unknown age"
    seconds = (now - ts).total_seconds()
    if seconds < 3600:
        return f"{seconds / 60:.0f}m old"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h old"
    return f"{seconds / 86400:.1f}d old"


def _participant_label(participant, agent_names):
    agent_id = participant.get("agentId")
    if agent_id is None:
        return participant.get("userId") or "unknown"
    return agent_names.get(agent_id, agent_id)


def _common_evidence_lines(record, execution_state, execution):
    # DAN-727 req 3: these fields describe the *named run* this detector
    # last observed via GET .../execution, not the participant's seat --
    # labelled explicitly so a reader (or a future diagnosis) does not
    # mistake run-scoped evidence for seat-level reachability, which is
    # what caused both DAN-551 and DAN-858.
    return [
        f"- Lock fields are all null: `checkoutRunId={record.get('checkoutRunId')}`, "
        f"`executionRunId={record.get('executionRunId')}`, `executionLockedAt={record.get('executionLockedAt')}`",
        f"- Of the last run observed on *this issue* -- `activeRun={record.get('activeRun')}`, last known "
        f"execution phase `{execution.get('phase')}` (cause: `{execution.get('cause')}`), "
        f"`permittedActions={execution.get('permittedActions')}`. This describes that run, not the "
        "participant's seat.",
        f"- Last recorded decision: `lastDecisionId={execution_state.get('lastDecisionId')}`, "
        f"`lastDecisionOutcome={execution_state.get('lastDecisionOutcome')}`",
    ]


def _wake_evidence_line(wake, label, now):
    newest = wake["newest_event"] or {}
    requested_at = parse_ts(newest.get("requestedAt"))
    age = _fmt_age(now, requested_at)
    staleness = " -- **stale, not current evidence**" if wake["stale"] else ""
    return (
        f"- `GET .../diagnostics/wakes`: the {wake['dropped']} most recent wake(s) addressed to {label} on "
        f"this issue are `{DROPPED_WAKE_STATUS}` with `claimedAt: null` -- queued, never claimed. Most recent "
        f"request: `requestedAt={newest.get('requestedAt')}` ({age}), `runId={newest.get('runId')}`{staleness}."
    )


def _seat_evidence_line(seat, label):
    if seat is None:
        return f"- No corroborating seat-level `heartbeat-runs` evidence was fetched for {label}."
    quota_note = ""
    if seat["quota_runs"]:
        resolved = sum(1 for q in seat["quota_runs"] if (q["successor"] or {}).get("status") == "succeeded")
        quota_note = (
            f" ({len(seat['quota_runs'])} `provider_quota` failure(s), {resolved} with a succeeded "
            "retry successor -- a quota window, not a seat fault)"
        )
    return (
        f"- `GET .../heartbeat-runs?agentId=...` over the last {seat['total']} run(s) for {label}: "
        f"{seat['succeeded']} succeeded, {seat['failed']} failed, {seat['cancelled']} cancelled, "
        f"{seat['running']} running, {seat['queued']} queued; most recent success "
        f"`{seat['most_recent_success_at']}`{quota_note}. Seat judged "
        f"**{'reachable' if seat['reachable'] else 'not reachable from this evidence'}**."
    )


def describe_poke(finding, now, agent_names, classification=None, fingerprint=None):
    """The per-issue comment body when the action is a poke (DAN-380): names
    the participant, states only the verified facts, and offers the normal
    review-decision PATCH as an unverified next step. `classification`
    (DAN-727), when given, adds the strand-class label and corroborating
    evidence so a reader can see why this is "just a poke" rather than an
    escalation. `fingerprint`, when given, is embedded as a hidden line so a
    later tick's should_poke() can detect a worsened finding inside the
    repoke window."""
    record = finding["record"]
    participant = finding["participant"]
    agent_id = participant.get("agentId")
    label = _participant_label(participant, agent_names)
    mention = f"[@{label}](agent://{agent_id})" if agent_id else f"**{label}**"
    execution_state = record["executionState"]
    execution = execution_facts(record)
    issue_id = record["id"]

    lines = [
        f"## Stranded-approval-stage detector -- participant action needed {MARKER}",
        "",
        f"{mention} -- the `{execution_state.get('currentStageType')}` stage on "
        f"[{record.get('identifier', issue_id)}](/DAN/issues/{record.get('identifier', issue_id)}) has been "
        f"pending since {finding['since'].isoformat()} ({_fmt_elapsed(now, finding['since'])} ago), and "
        "no run currently holds this issue. You are its `currentParticipant`; this comment itself generates "
        "a claimable wake to you, which is the cheapest and first-tried remedy here:",
        "",
        *_common_evidence_lines(record, execution_state, execution),
    ]
    if classification is not None:
        strand_class = classification["strand_class"]
        if strand_class == "stranded_wake":
            lines.append(
                f"- Classified as a **stranded wake**, not an unreachable participant: "
                f"{_wake_evidence_line(classification['wake'], label, now)[2:]}"
            )
            lines.append(_seat_evidence_line(classification["seat"], label))
    lines += [
        "",
        "This detector has not independently verified what moves this stage. The usual "
        "review-decision path, if it applies here:",
        "",
        f'- Approve: `PATCH /api/issues/{issue_id}` with `{{"status": "done", "comment": "Approved: ..."}}`',
        f'- Request changes: `PATCH /api/issues/{issue_id}` with `{{"status": "in_progress", "comment": "Changes requested: ..."}}`',
    ]
    lines.append("")
    lines.append(_action_line("poke"))
    if fingerprint is not None:
        lines += ["", _fingerprint_line(fingerprint)]
    return "\n".join(lines)


def describe_escalation(finding, now, agent_names, classification, fingerprint=None):
    """The per-issue comment body when the action is a CEO escalation
    (DAN-727 req 5: only reached for `reconciliation_hold`, or for
    `unreachable_participant` after a prior poke demonstrably failed).
    Addresses the CEO seat with the corroborating evidence -- on-issue wake
    age/runId, and seat-level heartbeat-run activity -- not just the single
    dropped wake that was DAN-551/DAN-858's false signal on its own."""
    record = finding["record"]
    participant = finding["participant"]
    label = _participant_label(participant, agent_names)
    execution_state = record["executionState"]
    execution = execution_facts(record)
    issue_id = record["id"]
    ceo_label = agent_names.get(ESCALATION_AGENT_ID, "Cloud")
    ceo_mention = f"[@{ceo_label}](agent://{ESCALATION_AGENT_ID})"
    strand_class = classification["strand_class"]

    lines = [
        f"## Stranded-approval-stage detector -- {strand_class}, escalating {MARKER}",
        "",
        f"{ceo_mention} -- the `{execution_state.get('currentStageType')}` stage on "
        f"[{record.get('identifier', issue_id)}](/DAN/issues/{record.get('identifier', issue_id)}) has been "
        f"pending since {finding['since'].isoformat()} ({_fmt_elapsed(now, finding['since'])} ago). "
        f"Strand class: **{strand_class}**.",
        "",
    ]
    if strand_class == "reconciliation_hold":
        lines += [
            f"- `executionBlocker` is populated on the per-issue GET: `{classification['execution_blocker']}`.",
            "- Only a plain comment from a **human board user** clears this hold -- an agent's own comment "
            "is a documented no-op (`admitExplicitNativeContinuation` gates on `actorType === \"user\"`). "
            "This is not a dissolution or a `stalled-review-decision` call; it is a request for a human "
            "to comment on the issue.",
        ]
    else:
        lines.append(
            f"A prior poke was already tried and produced no claimed wake within {MIN_PENDING_SECONDS // 60} "
            f"minutes. Its participant **{label}**:"
        )
        lines.append(_wake_evidence_line(classification["wake"], label, now))
        lines.append(_seat_evidence_line(classification["seat"], label))
    if strand_class == "reconciliation_hold":
        not_reinstructing_reason = (
            "a poke would be an inert no-op against a reconciliation hold (only a human board comment clears it)"
        )
    else:
        not_reinstructing_reason = "a poke was already tried and did not produce a claimed wake"
    lines += [
        *_common_evidence_lines(record, execution_state, execution),
        "",
        f"This detector is not re-instructing {label} to move this stage directly -- "
        f"{not_reinstructing_reason}. This needs a human/CEO-level unwedge.",
    ]
    lines.append("")
    lines.append(_action_line("escalate"))
    if fingerprint is not None:
        lines += ["", _fingerprint_line(fingerprint)]
    return "\n".join(lines)


def describe_rollup(findings, classifications, actions, now, agent_names):
    lines = [f"## Stranded-approval-stage sweep -- {len(findings)} finding(s)", ""]
    for finding, classification, action in zip(findings, classifications, actions, strict=True):
        record = finding["record"]
        label = _participant_label(finding["participant"], agent_names)
        lines.append(
            f"- [{record.get('identifier', record['id'])}](/DAN/issues/{record.get('identifier', record['id'])}) "
            f"-- participant **{label}**, strand class **{classification['strand_class']}**, "
            f"action **{action}**, pending {_fmt_elapsed(now, finding['since'])} "
            f"(since {finding['since'].isoformat()})"
        )
    return "\n".join(lines)


def agent_names(company_id):
    try:
        agents = api_get(f"/api/companies/{company_id}/agents")
        return {a["id"]: a.get("name") or a.get("displayName") or a["id"] for a in agents}
    except Exception:
        return {}


def fetch_seat_runs(company_id, agent_id):
    """DAN-727 (DAN-918): the participant's raw heartbeat-run list, for
    `classify_seat_reachability()`. Returns [] on any fetch failure --
    missing corroborating evidence never upgrades a finding to
    `unreachable_participant` (see `classify_finding()`)."""
    try:
        payload = api_get(f"/api/companies/{company_id}/heartbeat-runs?limit={SEAT_RUNS_LIMIT}&agentId={agent_id}")
    except Exception:
        return []
    if isinstance(payload, list):
        return payload
    return payload.get("runs", payload.get("data", [])) or []


def fetch_records(company_id, limit):
    """Pulls in_review candidates from the list endpoint (cheap: activeRun,
    lastActivityAt, originKind), then merges in the full single-issue GET
    (executionState, reviewAttention, lock fields, executionBlocker -- the
    list endpoint always returns executionState: null, verified against live
    data) and the /execution endpoint for every candidate that still has a
    live currentParticipant after the cheap checks, to decide/describe
    condition 3, plus (DAN-380) the /diagnostics/wakes endpoint for the same
    candidates, to decide whether `currentParticipant` is actually reachable
    before any finding names or instructs them."""
    listing = api_get(
        f"/api/companies/{company_id}/issues?status=in_review&limit={limit}"
    )
    candidates = listing if isinstance(listing, list) else listing.get("issues", listing.get("data", []))

    records = []
    for entry in candidates:
        if entry.get("originKind") == "routine_execution":
            continue
        full = api_get(f"/api/issues/{entry['id']}")
        execution_state = full.get("executionState")
        if not execution_state or execution_state.get("status") != "pending":
            continue
        if not execution_state.get("currentParticipant"):
            continue
        execution = None
        try:
            execution = api_get(f"/api/issues/{entry['id']}/execution")
        except Exception:
            execution = None
        wakes = None
        try:
            wakes = api_get(f"/api/issues/{entry['id']}/diagnostics/wakes")
        except Exception:
            wakes = None
        records.append(
            {
                "id": full["id"],
                "identifier": full.get("identifier"),
                "status": full.get("status"),
                "originKind": entry.get("originKind"),
                "activeRun": entry.get("activeRun"),
                "lastActivityAt": entry.get("lastActivityAt"),
                "executionState": execution_state,
                "executionBlocker": full.get("executionBlocker"),
                "reviewAttention": full.get("reviewAttention"),
                "checkoutRunId": full.get("checkoutRunId"),
                "executionRunId": full.get("executionRunId"),
                "executionLockedAt": full.get("executionLockedAt"),
                "execution": execution,
                "wakes": wakes,
            }
        )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--post-comment-on", help="Issue id for the roll-up comment (this run's own issue)")
    parser.add_argument("--dry-run", action="store_true", help="Print findings, never post")
    parser.add_argument("--post", action="store_true", help="Explicit opt-in: actually post poke + roll-up comments")
    args = parser.parse_args()

    company_id = os.environ["PAPERCLIP_COMPANY_ID"]
    now = datetime.now(timezone.utc)

    records = fetch_records(company_id, args.limit)
    findings = compute_findings(records, now)

    if not findings:
        print("No stranded approval stages found.")
        return 0

    names = agent_names(company_id)
    will_post = args.post and not args.dry_run

    seat_runs_cache = {}
    classifications = []
    for finding in findings:
        participant_agent_id = finding["participant"].get("agentId")
        seat_runs = None
        if participant_agent_id is not None:
            if participant_agent_id not in seat_runs_cache:
                seat_runs_cache[participant_agent_id] = fetch_seat_runs(company_id, participant_agent_id)
            seat_runs = seat_runs_cache[participant_agent_id]
        classifications.append(classify_finding(finding, now, seat_runs))

    # DAN-385 requirement 6: a still-stranded issue inside its 6h repoke
    # window is a pure restatement of an already-reported finding -- it must
    # not count toward this tick's own execution issue, even though it is
    # still printed for visibility. `due` (computed from a real, always-
    # fetched read, dry-run included) is the single source of truth for
    # that distinction; `any_actionable` folds it across every finding so
    # main() can print one tick-level signal.
    any_actionable = False
    actions = []
    for finding, classification in zip(findings, classifications, strict=True):
        record = finding["record"]
        participant_agent_id = finding["participant"].get("agentId")
        comments = api_get(f"/api/issues/{record['id']}/comments")
        comments = comments if isinstance(comments, list) else comments.get("comments", comments.get("data", []))

        action = decide_strand_action(
            classification["strand_class"], comments, participant_agent_id, record, now
        )
        actions.append(action)
        fingerprint = classification_fingerprint(classification, action)

        if action == "escalate":
            poke_body = describe_escalation(finding, now, names, classification, fingerprint)
        else:
            poke_body = describe_poke(finding, now, names, classification, fingerprint)
        print(poke_body)
        print()

        due = should_poke(comments, now, fingerprint)
        identifier = record.get("identifier", record["id"])

        if due and will_post:
            # DAN-727 req 4: close the read-to-post race -- re-check the
            # issue is still actually in this state immediately before
            # writing anything.
            if refresh_is_resolved(record["id"]):
                due = False
                print(f"Skipped {identifier}: resolved itself since this tick's read; not posting.")
            else:
                api_post(f"/api/issues/{record['id']}/comments", {"body": poke_body})
                print(f"Posted {action} comment on {identifier}.")
        elif will_post:
            print(f"Skipped {identifier}: poked within the last 6h already.")
        elif due:
            print(f"[dry-run] would post {action} comment on {identifier}.")
        else:
            print(f"[dry-run] would skip {identifier}: poked within the last 6h already.")
        any_actionable = any_actionable or due
        print()

    rollup_body = describe_rollup(findings, classifications, actions, now, names)
    print(rollup_body)
    if args.post_comment_on and will_post:
        api_post(f"/api/issues/{args.post_comment_on}/comments", {"body": rollup_body})
        print(f"\nPosted roll-up comment on {args.post_comment_on}.")

    print(f"\nACTIONABLE: {'yes' if any_actionable else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
