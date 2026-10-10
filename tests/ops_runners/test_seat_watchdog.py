#!/usr/bin/env python3
"""Plain-assert tests for ops/seat_watchdog.py (DAN-218).

No pytest dependency -- run directly:
    python3 ops/test_seat_watchdog.py

Covers the DAN-218 wake-comment acceptance bar directly:
  - a synthetic 3-crash sequence fires
  - a synthetic 3-probe sequence stays silent
plus the supporting exclusions named in the DAN-215 trigger-spec (cancelled
is transparent, terminal-limit-failure is excluded, succeeded resets), and a
regression check against the real captured 2026-10-03 10:00-14:43Z Cloud
seat band (13x access + 1x process_lost, zero limit, ~20-30min apart): it
must fire exactly once, not fourteen times.
"""
import itertools
import json
import os
from datetime import datetime, timedelta, timezone

from seat_watchdog import (
    agent_window_stats,
    classify_run,
    compute_findings,
    count_prior_resumes,
    covered_cutoff,
    describe_finding,
    file_or_update_finding,
    filter_already_covered,
    find_existing_finding_issue,
    find_recovery,
    finding_marker,
    finding_title,
    is_actionable,
    is_finding_tracking_issue,
    predict_action,
    reconcile_duplicate_trackers,
    true_streak_extent,
)

AGENT = "agent-under-test"
T0 = datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)

_ids = itertools.count(1)


def _ts(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _ts_dt(minutes):
    return T0 + timedelta(minutes=minutes)


def crash_run(minutes, agent_id=AGENT, error_code="acpx_turn_failed",
              error="ACP agent reported a terminal access failure."):
    """A bare crash-before-first-tool-call run: resultJson.summary mirrors
    the raw error verbatim (or is absent), i.e. the model never got to say
    anything of its own."""
    return {
        "id": f"run-{next(_ids)}",
        "agentId": agent_id,
        "status": "failed",
        "startedAt": _ts(minutes),
        "createdAt": _ts(minutes),
        "errorCode": error_code,
        "error": error,
        "resultJson": {"summary": error},
    }


def probe_run(minutes, agent_id=AGENT, summary="Re-probed identity via 3 channels; posted dispositive comment."):
    """The DAN-171 shape: non-succeeded, but the model ran a real probe and
    authored its own summary distinct from the raw error -- exempt."""
    return {
        "id": f"run-{next(_ids)}",
        "agentId": agent_id,
        "status": "failed",
        "startedAt": _ts(minutes),
        "createdAt": _ts(minutes),
        "errorCode": "acpx_turn_failed",
        "error": "ACP agent reported a terminal access failure.",
        "resultJson": {"summary": summary},
    }


def succeeded_run(minutes, agent_id=AGENT):
    return {
        "id": f"run-{next(_ids)}",
        "agentId": agent_id,
        "status": "succeeded",
        "startedAt": _ts(minutes),
        "createdAt": _ts(minutes),
        "errorCode": None,
        "error": None,
        "resultJson": {"summary": "Did the work."},
    }


def cancelled_run(minutes, agent_id=AGENT):
    return {
        "id": f"run-{next(_ids)}",
        "agentId": agent_id,
        "status": "cancelled",
        "startedAt": None,
        "createdAt": _ts(minutes),
        "errorCode": "issue_reassigned",
        "error": "Scheduled retry suppressed because issue ownership changed",
        "resultJson": None,
    }


def limit_run(minutes, agent_id=AGENT):
    return {
        "id": f"run-{next(_ids)}",
        "agentId": agent_id,
        "status": "failed",
        "startedAt": _ts(minutes),
        "createdAt": _ts(minutes),
        "errorCode": "acpx_turn_failed",
        "error": "ACP agent reported a terminal limit failure.",
        "resultJson": {"summary": "ACP agent reported a terminal limit failure."},
    }


def test_three_crash_sequence_fires():
    # Acceptance bar #1: 3 bare crashes spanning >=10min fire.
    runs = [crash_run(0), crash_run(5), crash_run(12)]
    findings = compute_findings(runs)
    assert len(findings) == 1, findings
    assert findings[0]["agentId"] == AGENT
    assert len(findings[0]["runs"]) == 3


def test_three_probe_sequence_stays_silent():
    # Acceptance bar #2: 3 (or even 5+) DAN-171-shape probes never fire,
    # regardless of count or span, because none of them is a bare crash.
    runs = [probe_run(0), probe_run(5), probe_run(12), probe_run(20), probe_run(40)]
    findings = compute_findings(runs)
    assert findings == []
    for r in runs:
        assert classify_run(r) == "transparent"


def test_crash_streak_too_tight_in_time_does_not_fire():
    # Spec §2: a fast retry burst (sub-10-minute spread) must not trip the
    # arm even if the count is 3+ -- this is the guard against quota-style
    # rapid retries that happen to not say "limit failure".
    runs = [crash_run(0), crash_run(1), crash_run(2)]
    assert compute_findings(runs) == []


def test_crash_streak_qualifies_once_span_grows_past_threshold():
    # Same tight-start streak as above, but a later crash pushes the span
    # over 10 minutes -- must fire exactly once at that point, not retroactively
    # multiple times and not for every run past the threshold.
    runs = [crash_run(0), crash_run(1), crash_run(2), crash_run(15), crash_run(20), crash_run(25)]
    findings = compute_findings(runs)
    assert len(findings) == 1
    assert len(findings[0]["runs"]) == 4  # fired as soon as span(first..4th) >= 10min


def test_succeeded_resets_the_streak():
    runs = [crash_run(0), crash_run(5), succeeded_run(8), crash_run(10), crash_run(15), crash_run(20)]
    # Without the reset this would fire at minute 15 (0,5,15 span 15min); with
    # the reset the only live streak is (10,15,20), span 10min -> fires once.
    findings = compute_findings(runs)
    assert len(findings) == 1
    assert len(findings[0]["runs"]) == 3
    assert findings[0]["runs"][0]["id"] == runs[3]["id"]


def test_cancelled_is_transparent_does_not_count_or_reset():
    runs = [crash_run(0), cancelled_run(3), crash_run(5), cancelled_run(7), crash_run(12)]
    findings = compute_findings(runs)
    assert len(findings) == 1
    assert len(findings[0]["runs"]) == 3  # the two cancelled runs are invisible to the counter
    assert all(r["status"] != "cancelled" for r in findings[0]["runs"])


def test_limit_failure_excluded_entirely():
    runs = [limit_run(0), limit_run(1), limit_run(2), limit_run(3), limit_run(20)]
    assert compute_findings(runs) == []


def test_mixed_seats_are_independent():
    runs = [
        crash_run(0, agent_id="seat-a"), crash_run(5, agent_id="seat-a"), crash_run(12, agent_id="seat-a"),
        probe_run(0, agent_id="seat-b"), probe_run(5, agent_id="seat-b"), probe_run(12, agent_id="seat-b"),
    ]
    findings = compute_findings(runs)
    assert len(findings) == 1
    assert findings[0]["agentId"] == "seat-a"


# --- DAN-526: denominator, invocationSource, self-recovery ---
#
# A real escalation (DAN-337) read several separate watchdog comments, each
# individually accurate, and concluded "14 hours, zero succeeded runs" for a
# seat that had in fact succeeded 28 times in that span and had already
# recovered 49 minutes before the comment citing it was written. These
# cover the fix: a denominator on every finding, invocationSource per run,
# and an explicit self-recovery check against the UNFILTERED run list.


def test_agent_window_stats_counts_succeeded_failed_cancelled_in_window():
    runs = [
        crash_run(0, agent_id="seat-a"),
        succeeded_run(5, agent_id="seat-a"),
        cancelled_run(10, agent_id="seat-a"),
        crash_run(15, agent_id="seat-a"),
        crash_run(0, agent_id="seat-b"),  # a different seat must not be counted
    ]
    stats = agent_window_stats("seat-a", runs)
    assert stats["succeeded"] == 1
    assert stats["failed"] == 2
    assert stats["cancelled"] == 1
    assert stats["window_start"] == _ts_dt(0)
    assert stats["window_end"] == _ts_dt(15)
    assert stats["succeeded_timestamps"] == [_ts_dt(5)]


def test_agent_window_stats_returns_none_for_unknown_agent():
    runs = [crash_run(0, agent_id="seat-a")]
    assert agent_window_stats("seat-nonexistent", runs) is None


def test_find_recovery_finds_a_later_success_for_the_same_seat():
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    all_runs = streak_runs + [succeeded_run(20), crash_run(0, agent_id="seat-b")]
    assert find_recovery(finding, all_runs) == _ts_dt(20)


def test_find_recovery_is_none_when_no_later_success_exists():
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    # A success for a DIFFERENT seat, or before the streak ended, must not count.
    all_runs = streak_runs + [succeeded_run(1, agent_id="seat-other"), succeeded_run(1, agent_id=AGENT)]
    assert find_recovery(finding, all_runs) is None


# --- DAN-777: true_streak_extent -- the frozen sample vs the real streak ---
#
# `compute_findings` freezes `finding["runs"]` at the qualifying 3(+) runs
# and never grows it, by design (fires once per streak). `true_streak_extent`
# re-derives how far that same contiguous streak actually went, from the
# unfiltered run list, without touching the frozen sample.


def test_true_streak_extent_matches_frozen_sample_when_streak_ends_there():
    # The streak resets (succeeds) right after the frozen sample -- the true
    # extent must equal the frozen sample exactly.
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    all_runs = streak_runs + [succeeded_run(20)]
    extent = true_streak_extent(finding, all_runs)
    assert extent["count"] == 3
    assert extent["start"] == _ts_dt(0)
    assert extent["end"] == _ts_dt(12)
    assert extent["ongoing"] is False


def test_true_streak_extent_grows_past_the_frozen_sample_until_reset():
    # This is the DAN-777 bug: 3 more bare crashes happen after the frozen
    # 3-run sample, then the seat succeeds. The frozen sample must stay at
    # 3 runs (untouched), but the true extent must see all 6.
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    extra_crashes = [crash_run(20), crash_run(30), crash_run(40)]
    all_runs = streak_runs + extra_crashes + [succeeded_run(50)]
    extent = true_streak_extent(finding, all_runs)
    assert len(finding["runs"]) == 3  # frozen sample is untouched
    assert extent["count"] == 6
    assert extent["start"] == _ts_dt(0)
    assert extent["end"] == _ts_dt(40)
    assert extent["ongoing"] is False


def test_true_streak_extent_is_ongoing_when_no_reset_follows():
    # No succeeded run after the streak at all -- as of this fetch, the
    # streak is still ongoing, not resolved.
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    all_runs = streak_runs + [crash_run(20), crash_run(30)]
    extent = true_streak_extent(finding, all_runs)
    assert extent["count"] == 5
    assert extent["end"] == _ts_dt(30)
    assert extent["ongoing"] is True


def test_true_streak_extent_ignores_transparent_and_other_seats():
    # Cancelled/probe runs and a different seat's crashes must not be
    # folded into this seat's true extent.
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    all_runs = (
        streak_runs
        + [cancelled_run(15), probe_run(18), crash_run(0, agent_id="seat-other")]
        + [crash_run(20)]
        + [succeeded_run(30)]
    )
    extent = true_streak_extent(finding, all_runs)
    assert extent["count"] == 4
    assert extent["end"] == _ts_dt(20)


def test_true_streak_extent_returns_none_when_start_run_not_found():
    finding = {"agentId": AGENT, "runs": [crash_run(0)]}
    assert true_streak_extent(finding, []) is None


def test_describe_finding_headline_uses_true_extent_when_all_runs_given():
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    extra_crashes = [crash_run(20), crash_run(30), crash_run(40)]
    all_runs = streak_runs + extra_crashes
    body = describe_finding(finding, all_runs=all_runs)
    assert "6 consecutive" in body
    assert "3 consecutive" not in body
    assert "**ongoing**" in body
    # the bulleted sample below the headline is still just the frozen 3
    assert body.count("`run-") == 3
    assert "Showing the first 3 runs" in body


def test_describe_finding_headline_falls_back_to_frozen_sample_without_all_runs():
    streak_runs = [crash_run(0), crash_run(5), crash_run(12)]
    finding = {"agentId": AGENT, "runs": streak_runs}
    body = describe_finding(finding)
    assert "3 consecutive" in body
    assert "Showing the first" not in body


def test_describe_finding_labels_ongoing_when_no_window_stats_or_recovery():
    finding = {"agentId": AGENT, "runs": [crash_run(0), crash_run(5), crash_run(12)]}
    body = describe_finding(finding)
    assert "**ongoing**" in body


def test_describe_finding_labels_intermittent_and_states_denominator():
    finding = {"agentId": AGENT, "runs": [crash_run(0), crash_run(5), crash_run(12)]}
    stats = {
        "window_start": _ts_dt(0), "window_end": _ts_dt(100),
        "succeeded": 5, "failed": 3, "cancelled": 2,
        "succeeded_timestamps": [_ts_dt(50)],
    }
    body = describe_finding(finding, window_stats=stats)
    assert "**intermittent**" in body
    assert "5 succeeded" in body
    assert "3 failed" in body
    assert "2 cancelled" in body
    assert _ts_dt(50).isoformat() in body


def test_describe_finding_labels_self_recovered_and_states_recovery_time():
    finding = {"agentId": AGENT, "runs": [crash_run(0), crash_run(5), crash_run(12)]}
    recovered_at = _ts_dt(40)
    body = describe_finding(finding, recovered_at=recovered_at)
    assert "**self-recovered**" in body
    assert recovered_at.isoformat() in body


def test_describe_finding_includes_invocation_source_per_run():
    runs = [
        crash_run(0),
        {**crash_run(5), "invocationSource": "timer"},
    ]
    runs[0]["invocationSource"] = "assignment"
    finding = {"agentId": AGENT, "runs": runs}
    body = describe_finding(finding)
    assert "[assignment]" in body
    assert "[timer]" in body


def test_describe_finding_invocation_source_defaults_to_unknown_when_absent():
    finding = {"agentId": AGENT, "runs": [crash_run(0)]}
    assert "[unknown]" in describe_finding(finding)


def test_predict_action_self_recovered_noted_when_no_tracker_and_already_recovered():
    def fake_get(path):
        return {"issues": []}

    finding = _finding(agent_id="seat-a")
    action = predict_action("company", finding, api_get_fn=fake_get, recovered_at=_ts_dt(999))
    assert action == "self_recovered_noted"
    assert not is_actionable(action)


def test_file_or_update_finding_does_not_file_a_tracker_when_already_self_recovered():
    posts = []

    def fake_get(path):
        return {"issues": []}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post,
        recovered_at=_ts_dt(999),
    )
    assert action == "self_recovered_noted"
    assert issue is None
    assert posts == []  # nothing written at all -- not a comment, not an issue


def test_predict_action_self_recovered_not_resumed_for_closed_tracker():
    existing = {
        "id": "tracking-1", "identifier": "DAN-337", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    finding = _finding(agent_id="seat-a")
    action = predict_action("company", finding, api_get_fn=fake_get, recovered_at=_ts_dt(999))
    assert action == "self_recovered_not_resumed"
    assert not is_actionable(action)


def test_file_or_update_finding_comments_without_resuming_when_closed_tracker_already_recovered():
    # The literal DAN-337 shape: a closed tracker, genuinely-new-by-timestamp
    # evidence, but the seat already succeeded again before this tick ran.
    # Must comment, and must NEVER PATCH status back to `todo`.
    existing = {
        "id": "tracking-1", "identifier": "DAN-337", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }
    posts, patches = [], []

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=fake_patch,
        recovered_at=_ts_dt(999),
    )
    assert action == "self_recovered_not_resumed"
    assert issue is existing
    assert patches == []  # status never touched -- this is the done->todo thrash fix
    assert len(posts) == 1
    assert posts[0][0] == "/api/issues/tracking-1/comments"


def test_find_existing_finding_issue_prefers_earliest_created_among_closed_duplicates():
    # Reproduces the exact DAN-330/DAN-337 shape: a race produced two closed
    # trackers sharing the same seat marker. Picking "most recently updated"
    # turns this into a feedback loop (touching one makes it win again next
    # time); picking earliest-created always prefers the original filing,
    # independent of how many times either has been touched since.
    marker_finding = _finding(agent_id="seat-a")
    older = {
        "id": "DAN-330", "status": "done", "title": finding_title(marker_finding),
        "createdAt": "2026-10-03T23:55:00Z", "updatedAt": "2026-10-03T23:55:00Z",
    }
    newer_but_more_recently_touched = {
        "id": "DAN-337", "status": "done", "title": finding_title(marker_finding),
        "createdAt": "2026-10-04T00:10:00Z", "updatedAt": "2026-10-04T15:10:00Z",
    }

    def fake_get(path):
        return {"issues": [older, newer_but_more_recently_touched]}

    found = find_existing_finding_issue("company", "seat-a", api_get_fn=fake_get)
    assert found["id"] == "DAN-330"


# --- DAN-531: find_existing_finding_issue race -- concurrent ticks can file
# duplicate OPEN trackers for the same seat (TOCTOU, no atomicity on the
# dedupe read-then-write). Fixed by (1) an earliest-created tie-break among
# open matches too, matching the closed-match tie-break DAN-526 already
# uses, and (2) reconcile_duplicate_trackers, which merges a race-produced
# OPEN duplicate down to the canonical pick by closing the rest. ---


def test_find_existing_finding_issue_prefers_earliest_created_among_open_duplicates():
    # Mirrors the closed-duplicate test above, but for two OPEN trackers --
    # the exact shape a filing race (not just a resume race) produces.
    # Picking "most recently updated" would let whichever duplicate this
    # function last touched (by posting an update comment to it) keep
    # winning forever, silently orphaning the other open sibling.
    marker_finding = _finding(agent_id="seat-a")
    older = {
        "id": "DAN-900", "status": "todo", "title": finding_title(marker_finding),
        "createdAt": "2026-10-04T01:08:00Z", "updatedAt": "2026-10-04T01:08:00Z",
    }
    newer_but_more_recently_touched = {
        "id": "DAN-901", "status": "todo", "title": finding_title(marker_finding),
        "createdAt": "2026-10-04T01:08:05Z", "updatedAt": "2026-10-04T15:10:00Z",
    }

    def fake_get(path):
        return {"issues": [newer_but_more_recently_touched, older]}

    found = find_existing_finding_issue("company", "seat-a", api_get_fn=fake_get)
    assert found["id"] == "DAN-900"


def test_reconcile_duplicate_trackers_closes_non_canonical_open_sibling():
    marker_finding = _finding(agent_id="seat-a")
    canonical = {
        "id": "DAN-900", "identifier": "DAN-900", "status": "todo",
        "title": finding_title(marker_finding), "createdAt": "2026-10-04T01:08:00Z",
    }
    duplicate = {
        "id": "DAN-901", "identifier": "DAN-901", "status": "todo",
        "title": finding_title(marker_finding), "createdAt": "2026-10-04T01:08:05Z",
    }

    def fake_get(path):
        return {"issues": [canonical, duplicate]}

    patches = []

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    reconcile_duplicate_trackers("company", "seat-a", canonical, api_get_fn=fake_get, api_patch_fn=fake_patch)
    assert len(patches) == 1
    assert patches[0][0] == "/api/issues/DAN-901"
    assert patches[0][1]["status"] == "cancelled"
    assert "DAN-900" in patches[0][1]["comment"]


def test_reconcile_duplicate_trackers_leaves_closed_duplicates_and_canonical_alone():
    marker_finding = _finding(agent_id="seat-a")
    canonical = {
        "id": "DAN-900", "identifier": "DAN-900", "status": "todo",
        "title": finding_title(marker_finding), "createdAt": "2026-10-04T01:08:00Z",
    }
    old_closed = {
        "id": "DAN-800", "identifier": "DAN-800", "status": "done",
        "title": finding_title(marker_finding), "createdAt": "2026-10-01T00:00:00Z",
    }

    def fake_get(path):
        return {"issues": [canonical, old_closed]}

    patches = []

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    reconcile_duplicate_trackers("company", "seat-a", canonical, api_get_fn=fake_get, api_patch_fn=fake_patch)
    assert patches == []  # historical closed duplicate is left alone; canonical is never patched


def test_file_or_update_finding_merges_race_created_duplicate_when_filing():
    # Reproduces the exact DAN-531 TOCTOU: this tick's pre-POST
    # find_existing_finding_issue call(s) miss (empty -- the concurrent
    # tick's write isn't visible yet), so this tick files its own issue.
    # By the time it re-checks AFTER that POST, the concurrent tick's own
    # filing for the identical seat has become visible too, and it was
    # created first. The earlier-created sibling must win as canonical, and
    # the one this tick just filed must be closed as the duplicate.
    finding = _finding(agent_id="seat-a")
    calls = {"get": 0}
    race_sibling = {
        "id": "race-sibling", "identifier": "DAN-900", "status": "todo",
        "title": finding_title(finding), "createdAt": "2026-10-04T01:08:00Z",
    }
    created = {}

    def fake_get(path):
        calls["get"] += 1
        if calls["get"] <= 2:  # file_or_update_finding + predict_action each check once, pre-POST
            return {"issues": []}
        return {"issues": [race_sibling, created["issue"]]}

    def fake_post(path, body):
        created["issue"] = {
            "id": "new-issue", "identifier": "DAN-901", "status": "todo",
            "createdAt": "2026-10-04T01:08:05Z", **body,
        }
        return created["issue"]

    patches = []

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=fake_patch,
    )
    assert action == "filed"
    assert issue is race_sibling  # earlier-created wins, not the one this tick just filed
    assert len(patches) == 1
    assert patches[0][0] == "/api/issues/new-issue"
    assert patches[0][1]["status"] == "cancelled"
    assert "DAN-900" in patches[0][1]["comment"]


# --- Regression fixture: real captured 2026-10-03 10:00-14:43Z Cloud band ---
# Pulled live from GET /api/companies/{companyId}/heartbeat-runs?limit=300
# during DAN-218 implementation: agent c28db8ef (Cloud), 13x acpx_turn_failed
# ("...terminal access failure.") + 1x process_lost, zero limit failures,
# ~20-30min apart, then a succeeded run. Must fire exactly once.
_CLOUD_AGENT = "c28db8ef-7f04-4c21-b575-ddee819e050a"
_REAL_CLOUD_BAND = [
    {"id": "d066b206", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T10:00:23.886Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run2", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T10:00:28.244Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run3", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T10:01:28.309Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run4", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T10:32:53.776Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run5", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T11:03:23.872Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run6", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T11:33:53.981Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run7", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T12:04:24.045Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run8", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T12:34:54.070Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run9", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T13:05:24.069Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run10", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T13:35:54.083Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run11", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T14:06:24.072Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run12", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T14:36:54.082Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run13", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T14:43:11.013Z",
     "errorCode": "acpx_turn_failed", "error": "ACP agent reported a terminal access failure.",
     "resultJson": {"summary": "ACP agent reported a terminal access failure."}},
    {"id": "run14", "agentId": _CLOUD_AGENT, "status": "failed", "startedAt": "2026-10-03T14:43:53.561Z",
     "errorCode": "process_lost",
     "error": "Process lost -- child pid 634784 is no longer running; retrying once",
     "resultJson": None},
    {"id": "run15", "agentId": _CLOUD_AGENT, "status": "succeeded", "startedAt": "2026-10-03T14:48:15.110Z",
     "errorCode": None, "error": None, "resultJson": {"summary": "Did the work."}},
]


def test_real_cloud_fixture_fires_exactly_once_not_fourteen_times():
    findings = compute_findings(_REAL_CLOUD_BAND)
    assert len(findings) == 1, f"expected exactly 1 finding, got {len(findings)}"
    assert findings[0]["agentId"] == _CLOUD_AGENT
    # Fired as soon as span crossed 10min: runs 1-4 (10:00:23 -> 10:32:53).
    assert len(findings[0]["runs"]) == 4
    assert findings[0]["runs"][-1]["id"] == "run4"


def test_real_cloud_fixture_with_interleaved_cancelled_runs_unaffected():
    # Synthetic cancelled issue_reassigned runs interleaved into the real
    # band must not trip or reset anything.
    band = list(_REAL_CLOUD_BAND)
    band.insert(2, cancelled_run(0.5, agent_id=_CLOUD_AGENT))
    band.insert(7, cancelled_run(0.6, agent_id=_CLOUD_AGENT))
    findings = compute_findings(band)
    assert len(findings) == 1
    assert len(findings[0]["runs"]) == 4


# --- DAN-289: dedupe-and-file-or-update for exception reporting ---


def _finding(agent_id=AGENT, n=3):
    runs = [crash_run(m, agent_id=agent_id) for m in (0, 6, 12)][:n]
    return {"agentId": agent_id, "runs": runs}


def test_is_finding_tracking_issue_matches_exact_marker_only():
    agent_id = "seat-a"
    assert is_finding_tracking_issue({"title": f"Seat-health: Foo -- 3 consecutive no-evidence crashes [{agent_id}]"}, agent_id)
    # A different seat's marker, even with a similar-looking id, must not match.
    assert not is_finding_tracking_issue({"title": "Seat-health: Foo -- 3 consecutive no-evidence crashes [seat-ab]"}, agent_id)
    assert not is_finding_tracking_issue({"title": None}, agent_id)


def test_find_existing_finding_issue_filters_by_exact_marker():
    target = {"id": "tracking-1", "title": finding_title(_finding(agent_id="seat-a"))}

    def fake_get(path):
        # Simulates the search endpoint returning a loose superset match too.
        return {"issues": [{"id": "other", "title": "Seat-health: Foo [seat-ab]"}, target]}

    found = find_existing_finding_issue("company", "seat-a", api_get_fn=fake_get)
    assert found is target


def test_find_existing_finding_issue_returns_none_when_absent():
    def fake_get(path):
        return {"issues": []}

    assert find_existing_finding_issue("company", "seat-a", api_get_fn=fake_get) is None


def test_file_or_update_finding_creates_when_none_exists():
    posts = []

    def fake_get(path):
        return {"issues": []}

    def fake_post(path, body):
        posts.append((path, body))
        if path.endswith("/issues"):
            return {"id": "new-issue", "identifier": "DAN-999", **body}
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id", api_get_fn=fake_get, api_post_fn=fake_post,
    )
    assert action == "filed"
    assert issue["identifier"] == "DAN-999"
    assert len(posts) == 1
    assert finding_marker("seat-a") in posts[0][1]["title"]
    assert posts[0][1]["assigneeAgentId"] == "dante-id"


def test_file_or_update_finding_updates_existing_instead_of_filing_sibling():
    existing = {
        "id": "tracking-1", "identifier": "DAN-900", "status": "todo",
        "title": finding_title(_finding(agent_id="seat-a")),
    }
    posts = []

    def fake_get(path):
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id", api_get_fn=fake_get, api_post_fn=fake_post,
    )
    assert action == "updated"
    assert issue is existing
    # Only a comment posted, never POST .../issues (no sibling created).
    assert len(posts) == 1
    assert posts[0][0] == "/api/issues/tracking-1/comments"


# --- DAN-289 review (changes requested): dedupe must survive a closed
# tracker, via a per-seat high-water mark read back from the tracker's own
# description/comments. See DAN-339 for the incident this reproduces. ---


def test_file_or_update_finding_resumes_closed_tracker_instead_of_filing_sibling():
    # The exact DAN-329 -> DAN-336 shape: a CLOSED tracker for this seat
    # exists (found because find_existing_finding_issue now searches all
    # statuses), and genuinely new evidence has arrived for it. Must reopen
    # the existing tracker via resume, never file a sibling issue.
    existing = {
        "id": "tracking-1", "identifier": "DAN-329", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }
    posts, patches = [], []

    def fake_get(path):
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=fake_patch,
    )
    assert action == "resumed"
    assert issue is existing
    assert posts == []  # never files a sibling via POST .../issues
    assert len(patches) == 1
    assert patches[0][0] == "/api/issues/tracking-1"
    assert patches[0][1]["status"] == "todo"
    assert patches[0][1]["resume"] is True


def test_covered_cutoff_reads_max_timestamp_from_description_and_comments():
    issue = {
        "id": "tracking-1",
        "description": "Seat **Foo** has 3 crashes:\n- `run-1` 2026-10-03T18:00:00+00:00 x: y",
    }

    def fake_get(path):
        assert path == "/api/issues/tracking-1/comments"
        return {"comments": [{"body": "## Update\n\n- `run-9` 2026-10-03T22:15:06.344000+00:00 x: y"}]}

    cutoff = covered_cutoff(issue, api_get_fn=fake_get)
    assert cutoff is not None
    assert cutoff.isoformat() == "2026-10-03T22:15:06.344000+00:00"


def test_covered_cutoff_is_none_when_no_issue_or_no_run_lines():
    assert covered_cutoff(None) is None

    def fake_get(path):
        return {"comments": []}

    assert covered_cutoff({"id": "x", "description": "nothing here"}, api_get_fn=fake_get) is None


def _closed_tracker_for(runs, agent_id="seat-a"):
    finding = {"agentId": agent_id, "runs": runs}
    return {
        "id": "tracking-1", "status": "done",
        "title": finding_title(finding),
        "description": describe_finding(finding),
    }


def test_filter_already_covered_drops_runs_at_or_before_the_cutoff():
    # Reproduces DAN-339: a closed tracker already reported the 18:00-18:30
    # band for this seat. The next tick's lookback window still contains
    # those same runs (they haven't aged out of --limit yet) plus one
    # genuinely new crash after the tracker's high-water mark. Only the new
    # one should survive filtering.
    old_runs = [crash_run(0, agent_id="seat-a"), crash_run(6, agent_id="seat-a"), crash_run(12, agent_id="seat-a")]
    closed_tracker = _closed_tracker_for(old_runs)
    new_run = crash_run(600, agent_id="seat-a")  # 10 hours later, past the cutoff

    def fake_get(path):
        if "/comments" in path:
            return {"comments": []}
        return {"issues": [closed_tracker]}

    kept = filter_already_covered(old_runs + [new_run], "company", api_get_fn=fake_get)
    assert kept == [new_run]


def test_filter_already_covered_keeps_everything_when_no_prior_tracker():
    runs = [crash_run(0, agent_id="seat-b"), crash_run(6, agent_id="seat-b")]

    def fake_get(path):
        return {"issues": []}

    assert filter_already_covered(runs, "company", api_get_fn=fake_get) == runs


def test_closing_a_tracker_does_not_immediately_refile_the_same_streak():
    # End-to-end regression for DAN-339/DAN-289: once a seat's streak has
    # been filed and the tracker closed, re-running compute_findings against
    # the SAME historical runs (as the next tick's --limit window would) must
    # find nothing -- the real production bug was the tracker closing and the
    # very next tick re-filing the identical evidence as a sibling.
    old_runs = [crash_run(0, agent_id="seat-a"), crash_run(6, agent_id="seat-a"), crash_run(12, agent_id="seat-a")]
    closed_tracker = _closed_tracker_for(old_runs)

    def fake_get(path):
        if "/comments" in path:
            return {"comments": []}
        return {"issues": [closed_tracker]}

    filtered = filter_already_covered(old_runs, "company", api_get_fn=fake_get)
    assert compute_findings(filtered) == []


# --- DAN-339 changes-requested round: throttle auto-resume so repeat
# instances of an already-diagnosed, already-closed root cause don't keep
# thrashing the tracker's status back to todo on every tick. ---


def test_count_prior_resumes_counts_resume_marker_comments_only():
    def fake_get(path):
        assert path == "/api/issues/tracking-1/comments"
        return {"comments": [
            {"body": "## New occurrence (resumed)\n\nsome evidence"},
            {"body": "## Update\n\nunrelated comment"},
            {"body": "## New occurrence (resumed)\n\nmore evidence"},
        ]}

    assert count_prior_resumes({"id": "tracking-1"}, api_get_fn=fake_get) == 2


def test_count_prior_resumes_is_zero_when_issue_is_none_or_has_no_comments():
    assert count_prior_resumes(None) == 0

    def fake_get(path):
        return {"comments": []}

    assert count_prior_resumes({"id": "tracking-1"}, api_get_fn=fake_get) == 0


def test_file_or_update_finding_throttles_after_max_auto_resumes():
    # The DAN-339 changes-requested shape: a closed tracker that has ALREADY
    # been auto-resumed once before (one RESUME_MARKER comment on record) and
    # genuinely-new (past-cutoff) evidence for the same already-diagnosed root
    # cause arrives again. Must NOT flip status back to todo a second time --
    # only record a comment on the still-closed issue.
    existing = {
        "id": "tracking-1", "identifier": "DAN-336", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }
    posts, patches = [], []

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": [{"body": "## New occurrence (resumed)\n\nprior evidence"}]}
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=fake_patch,
    )
    assert action == "noted_closed"
    assert issue is existing
    assert patches == []  # status never touched
    assert len(posts) == 1
    assert posts[0][0] == "/api/issues/tracking-1/comments"


def test_file_or_update_finding_still_resumes_on_first_closed_occurrence():
    # Guards against a regression that throttles even the FIRST auto-resume:
    # zero prior RESUME_MARKER comments means this is still allowed through.
    existing = {
        "id": "tracking-1", "identifier": "DAN-329", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }
    patches = []

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    finding = _finding(agent_id="seat-a")
    issue, action = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=lambda *a: {"ok": True}, api_patch_fn=fake_patch,
    )
    assert action == "resumed"
    assert len(patches) == 1
    assert patches[0][1]["resume"] is True


def test_is_actionable_true_only_for_filed():
    # DAN-491: only "filed" puts a tracking issue in front of a human for
    # the first time. "updated" (comment on an already-open tracker),
    # "resumed" (reopen an already-known tracker), and "noted_closed"
    # (nothing changed) must all leave this tick's own execution issue on
    # the Clean path, not `done` -- see DAN-468/DAN-482 live evidence.
    assert is_actionable("filed")
    assert not is_actionable("updated")
    assert not is_actionable("resumed")
    assert not is_actionable("noted_closed")


def test_predict_action_matches_actual_action_when_none_exists():
    def fake_get(path):
        return {"issues": []}

    finding = _finding(agent_id="seat-a")
    assert predict_action("company", finding, api_get_fn=fake_get) == "filed"


def test_predict_action_matches_actual_action_for_open_tracker():
    existing = {
        "id": "tracking-1", "identifier": "DAN-335", "status": "todo",
        "title": finding_title(_finding(agent_id="seat-a")),
    }

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    finding = _finding(agent_id="seat-a")
    assert predict_action("company", finding, api_get_fn=fake_get) == "updated"


def test_predict_action_is_noted_closed_once_max_auto_resumes_is_hit():
    # DAN-385 requirement 6 regression shape: this is exactly DAN-336's live
    # state (closed, already auto-resumed once) -- a further genuinely-new
    # (past-cutoff) streak for the same seat must predict "noted_closed",
    # not "resumed", so the tick-level ACTIONABLE signal can stay "no" and
    # the routine does not mint a fresh execution issue for it.
    existing = {
        "id": "tracking-1", "identifier": "DAN-336", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": [{"body": "## New occurrence (resumed)\n\nprior evidence"}]}
        return {"issues": [existing]}

    finding = _finding(agent_id="seat-a")
    action = predict_action("company", finding, api_get_fn=fake_get)
    assert action == "noted_closed"
    assert not is_actionable(action)


def test_predict_action_matches_file_or_update_finding_without_writing(monkeypatch=None):
    # predict_action must never call api_post_fn/api_patch_fn -- it only
    # takes api_get_fn, so a write during prediction would be a TypeError,
    # not a silent bug. Confirm its prediction for a closed-but-not-yet-
    # throttled tracker agrees with what file_or_update_finding actually does.
    existing = {
        "id": "tracking-1", "identifier": "DAN-329", "status": "done",
        "title": finding_title(_finding(agent_id="seat-a")),
    }

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    finding = _finding(agent_id="seat-a")
    predicted = predict_action("company", finding, api_get_fn=fake_get)
    issue, actual = file_or_update_finding(
        "company", finding, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=lambda *a: {"ok": True},
        api_patch_fn=lambda *a: {"ok": True},
    )
    assert predicted == actual == "resumed"


# --- DAN-526 acceptance #5: replay the real 2026-10-04 Cloud run history ---
#
# Pulled live from GET /api/companies/{companyId}/heartbeat-runs?limit=300
# &agentId=c28db8ef-... (Cloud), trimmed to the 2026-10-03T23:55Z ->
# 2026-10-04T15:10Z window the DAN-337 escalation covered. This is the
# EXACT data the escalation chain read as "over 14 hours, zero succeeded
# runs" when the seat in fact succeeded 20 times in this window (138/89/67
# across the full 300-run fetch, per the issue) and the one real streak the
# escalation was built on top of (07:15:08 -> 10:01:08, the literal body of
# DAN-337's 14:55Z "resumed" comment) had already self-recovered at
# 14:15:38Z -- 40min before that comment, and well before the 14:55Z one.
_CLOUD_AGENT_OCT04 = "c28db8ef-7f04-4c21-b575-ddee819e050a"


def _load_real_cloud_oct04_window():
    path = os.path.join(os.path.dirname(__file__), "dan526_cloud_window_fixture.json")
    with open(path) as f:
        return json.load(f)


def test_dan526_real_cloud_window_has_far_more_than_one_qualifying_streak_denominator():
    runs = _load_real_cloud_oct04_window()
    stats = agent_window_stats(_CLOUD_AGENT_OCT04, runs)
    # This is the fact the escalation's "zero succeeded runs" claim is false
    # against: in the exact window it cited, the seat succeeded repeatedly.
    assert stats["succeeded"] > 0
    assert stats["succeeded"] >= 20
    assert stats["failed"] > 0
    assert stats["cancelled"] > 0


def test_dan526_real_cloud_window_classifies_the_long_streak_as_self_recovered_not_ongoing():
    runs = _load_real_cloud_oct04_window()
    findings = compute_findings(runs)
    # Two distinct streaks in this window (23:55-00:40 and 07:15-10:01), not
    # one undifferentiated 14-hour band -- the succeeded runs between and
    # after them are real resets, not noise to be stitched over.
    assert len(findings) == 2

    long_streak = findings[1]  # the 07:15Z -> 10:01Z streak behind DAN-337's real 14:55Z comment
    assert long_streak["runs"][0]["startedAt"] == "2026-10-04T07:15:08.073Z"
    assert long_streak["runs"][-1]["startedAt"] == "2026-10-04T10:01:08.775Z"

    recovered_at = find_recovery(long_streak, runs)
    window_stats = agent_window_stats(_CLOUD_AGENT_OCT04, runs)

    # This is the DAN-526 acceptance bar: at emit time, this streak must
    # classify as self-recovered, not as an ongoing zero-success outage.
    assert recovered_at is not None
    assert recovered_at.isoformat() == "2026-10-04T14:15:38.205000+00:00"

    body = describe_finding(long_streak, window_stats=window_stats, recovered_at=recovered_at)
    assert "**self-recovered**" in body
    assert "**ongoing**" not in body
    assert "20 succeeded" in body  # the denominator, visible on this one comment
    assert "[timer]" not in body  # this particular 3-run sample predates the timer-heavy tail
    assert "[assignment]" in body or "[automation]" in body


def test_dan526_real_cloud_window_would_not_resume_the_closed_tracker():
    # End-to-end: if DAN-337 (closed, "done") were found as the existing
    # tracker for this exact streak at 14:55Z, file_or_update_finding must
    # comment-only and never flip its status back to `todo`.
    runs = _load_real_cloud_oct04_window()
    findings = compute_findings(runs)
    long_streak = findings[1]
    recovered_at = find_recovery(long_streak, runs)
    window_stats = agent_window_stats(_CLOUD_AGENT_OCT04, runs)

    existing = {
        "id": "DAN-337", "status": "done",
        "title": finding_title(long_streak, recovered_at=recovered_at),
    }
    posts, patches = [], []

    def fake_get(path):
        if path.endswith("/comments"):
            return {"comments": []}
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"ok": True}

    def fake_patch(path, body):
        patches.append((path, body))
        return {"ok": True}

    issue, action = file_or_update_finding(
        "company", long_streak, {}, "project-x", "dante-id",
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=fake_patch,
        window_stats=window_stats, recovered_at=recovered_at,
    )
    assert action == "self_recovered_not_resumed"
    assert patches == []


def test_dan777_real_cloud_window_true_extent_reveals_the_full_outage():
    # The DAN-777 bug, against the real DAN-526 fixture: the frozen 3-run
    # sample for this streak only covers 07:15:08Z -> 10:01:08Z (~2h46m),
    # but the seat kept crashing well past that -- 12 bare-crash runs in
    # total, through 14:06:08Z (~6h51m) -- before it finally succeeded
    # again at 14:15:38Z. A reader of the frozen headline alone would
    # undersell this outage by 4x on count and >2x on duration.
    runs = _load_real_cloud_oct04_window()
    findings = compute_findings(runs)
    long_streak = findings[1]
    assert len(long_streak["runs"]) == 3  # frozen sample, unchanged by this fix

    extent = true_streak_extent(long_streak, runs)
    assert extent["count"] == 12
    assert extent["start"].isoformat() == "2026-10-04T07:15:08.073000+00:00"
    assert extent["end"].isoformat() == "2026-10-04T14:06:08.758000+00:00"
    assert extent["ongoing"] is False  # it did reset -- the 14:15:38Z success

    recovered_at = find_recovery(long_streak, runs)
    window_stats = agent_window_stats(_CLOUD_AGENT_OCT04, runs)
    body = describe_finding(long_streak, window_stats=window_stats, recovered_at=recovered_at, all_runs=runs)
    assert "12 consecutive" in body
    assert "3 consecutive" not in body
    assert "**self-recovered**" in body  # status classification is unaffected by this fix
    assert "Showing the first 3 runs" in body
    # the bulleted sample stays the pre-timer-tail frozen 3, same guarantee
    # the original DAN-526 regression test relies on
    assert "[timer]" not in body


def run_all():
    tests = [v for name, v in list(globals().items()) if name.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(run_all())
