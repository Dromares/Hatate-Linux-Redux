#!/usr/bin/env python3
"""Plain-assert tests for ops/routine_checkout_watchdog.py (DAN-666, DAN-703).

No pytest dependency -- run directly:
    python3 ops/test_routine_checkout_watchdog.py

Covers the DAN-666 acceptance bar:
  - a routine whose newest N runs are all no-checkout misses, spanning at
    least 2x its own cadence, fires
  - a single stale-but-checked-out run, or a single transient miss that has
    not yet crossed the 2x-cadence threshold, stays silent
  - the self routine (the watchdog's own) is always excluded from the scan
  - an inactive routine, or one with no enabled schedule trigger, is skipped
  - dedupe-and-file-or-update across filed/updated(+escalate)/resumed(+
    escalate)/throttled, mirroring seat_watchdog.py's/stranded_review.py's
    established shape for this watchdog's other arms

Covers the DAN-703 acceptance bar (the DAN-680 wedge shape DAN-666's
original arm cannot see -- `executionRunId` held by a queued-and-never-
started run instead of being null):
  - `queued_never_started`/`wedged_checkout_miss` key on the resolved run's
    own status/timestamps, not on `executionRunId` merely being non-null
  - a routine whose newest run is wedged this way, spanning at least 2x its
    own cadence, fires via the sibling `wedged_checkout_streak` arm
  - `evaluate_routine`/`compute_findings` without `fetch_run` behave exactly
    as before DAN-703 (backward compatible for the DAN-666 tests above)
  - `describe_finding` reports seat occupancy and a shared quota/clock
    boundary note for a wedged finding, instead of implying the queued run
    is dead
"""
import itertools
from datetime import datetime, timedelta, timezone

import routine_checkout_watchdog as rcw
from routine_checkout_watchdog import (
    ESCALATION_AGENT_ID,
    MARKER,
    RENOTIFY_INTERVAL_SECONDS,
    STALE_MULTIPLIER,
    agent_names,
    cadence_seconds,
    compute_findings,
    describe_finding,
    evaluate_routine,
    file_or_update_finding,
    find_existing_tracking_issue,
    finding_marker,
    finding_title,
    is_actionable,
    is_tracking_issue,
    no_checkout_miss,
    no_checkout_streak,
    queued_never_started,
    schedule_trigger,
    seat_occupancy_counts,
    shared_quota_boundary,
    should_notify,
    wedged_checkout_miss,
    wedged_checkout_streak,
)

NOW = datetime(2026, 10, 6, 22, 0, 0, tzinfo=timezone.utc)
ROUTINE_ID = "routine-under-test"

_ids = itertools.count(1)


def _ts(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_trigger(last_fired_minutes_ago=15, next_run_in_minutes=0, enabled=True, archived=False, kind="schedule"):
    """A 15-minute-cadence schedule trigger by default: lastFiredAt 15min
    ago, nextRunAt `next_run_in_minutes` from now -- cadence_seconds reads
    nextRunAt - lastFiredAt, so the default here is 30min unless overridden
    by passing matching values. Tests that need an exact cadence pass both
    explicitly via `trigger_with_cadence`."""
    return {
        "kind": kind,
        "enabled": enabled,
        "archived": archived,
        "lastFiredAt": _ts(NOW - timedelta(minutes=last_fired_minutes_ago)),
        "nextRunAt": _ts(NOW + timedelta(minutes=next_run_in_minutes)),
    }


def trigger_with_cadence(cadence_minutes):
    """A trigger whose nextRunAt - lastFiredAt is exactly `cadence_minutes`."""
    return {
        "kind": "schedule",
        "enabled": True,
        "archived": False,
        "lastFiredAt": _ts(NOW - timedelta(minutes=cadence_minutes)),
        "nextRunAt": _ts(NOW),
    }


def make_routine(routine_id=ROUTINE_ID, status="active", triggers=None, title="Some routine", owner="owner-1"):
    return {
        "id": routine_id,
        "title": title,
        "status": status,
        "assigneeAgentId": owner,
        "triggers": triggers if triggers is not None else [make_trigger()],
    }


def make_run(triggered_minutes_ago, linked_issue_id=None, run_id=None):
    return {
        "id": run_id or f"run-{next(_ids)}",
        "triggeredAt": _ts(NOW - timedelta(minutes=triggered_minutes_ago)),
        "linkedIssueId": linked_issue_id or f"issue-{next(_ids)}",
    }


def miss_issue(issue_id="issue-x"):
    return {"id": issue_id, "status": "in_progress", "checkoutRunId": None, "executionRunId": None}


def checked_out_issue(issue_id="issue-x", status="in_progress"):
    return {"id": issue_id, "status": status, "checkoutRunId": "run-abc", "executionRunId": "run-abc"}


def wedged_issue(issue_id="issue-x", execution_run_id="heartbeat-run-x"):
    """The DAN-680 shape: never checked out itself, but `executionRunId`
    is held by a (separately resolved) queued-and-never-started run."""
    return {"id": issue_id, "status": "in_progress", "checkoutRunId": None, "executionRunId": execution_run_id}


def queued_never_started_run(run_id="heartbeat-run-x", agent_id="seat-1", **overrides):
    run = {"id": run_id, "status": "queued", "startedAt": None, "claimedAt": None, "agentId": agent_id}
    run.update(overrides)
    return run


def dispatched_run(run_id="heartbeat-run-x", status="running", agent_id="seat-1"):
    return {"id": run_id, "status": status, "startedAt": _ts(NOW), "claimedAt": _ts(NOW), "agentId": agent_id}


def _issue_map_fetcher(mapping):
    def fetch(issue_id):
        return mapping.get(issue_id)
    return fetch


def _run_map_fetcher(mapping):
    def fetch(run_id):
        return mapping.get(run_id)
    return fetch


# --- schedule_trigger / cadence_seconds ---

def test_schedule_trigger_picks_enabled_non_archived_schedule_trigger():
    webhook = {"kind": "webhook", "enabled": True, "archived": False}
    disabled = {"kind": "schedule", "enabled": False, "archived": False}
    archived = {"kind": "schedule", "enabled": True, "archived": True}
    good = make_trigger()
    routine = make_routine(triggers=[webhook, disabled, archived, good])
    assert schedule_trigger(routine) is good


def test_schedule_trigger_is_none_when_no_eligible_trigger():
    routine = make_routine(triggers=[{"kind": "webhook", "enabled": True, "archived": False}])
    assert schedule_trigger(routine) is None


def test_cadence_seconds_reads_next_minus_last_fired():
    trigger = trigger_with_cadence(20)
    assert cadence_seconds(trigger) == 20 * 60


def test_cadence_seconds_is_none_when_timestamps_missing():
    assert cadence_seconds({"lastFiredAt": None, "nextRunAt": _ts(NOW)}) is None
    assert cadence_seconds({"lastFiredAt": _ts(NOW), "nextRunAt": None}) is None


def test_cadence_seconds_is_none_when_non_positive():
    trigger = {"lastFiredAt": _ts(NOW), "nextRunAt": _ts(NOW - timedelta(minutes=5))}
    assert cadence_seconds(trigger) is None


# --- no_checkout_miss / no_checkout_streak ---

def test_no_checkout_miss_true_for_in_progress_with_both_lock_fields_null():
    assert no_checkout_miss(miss_issue()) is True


def test_no_checkout_miss_false_when_checked_out():
    assert no_checkout_miss(checked_out_issue()) is False


def test_no_checkout_miss_false_when_terminal():
    assert no_checkout_miss({"status": "done", "checkoutRunId": None, "executionRunId": None}) is False


def test_no_checkout_miss_false_when_issue_is_none():
    assert no_checkout_miss(None) is False


def test_no_checkout_streak_stops_at_first_non_miss():
    runs = [make_run(5, "a"), make_run(20, "b"), make_run(35, "c")]
    issues = {"a": miss_issue("a"), "b": checked_out_issue("b"), "c": miss_issue("c")}
    streak, oldest = no_checkout_streak(runs, _issue_map_fetcher(issues))
    assert [r["linkedIssueId"] for r in streak] == ["a"]
    assert oldest == NOW - timedelta(minutes=5)


def test_no_checkout_streak_is_empty_when_newest_run_is_not_a_miss():
    runs = [make_run(5, "a")]
    issues = {"a": checked_out_issue("a")}
    streak, oldest = no_checkout_streak(runs, _issue_map_fetcher(issues))
    assert streak == []
    assert oldest is None


def test_no_checkout_streak_stops_at_run_with_no_linked_issue():
    runs = [make_run(5, "a"), {"id": "coalesced", "triggeredAt": _ts(NOW), "linkedIssueId": None}, make_run(40, "c")]
    issues = {"a": miss_issue("a"), "c": miss_issue("c")}
    streak, oldest = no_checkout_streak(runs, _issue_map_fetcher(issues))
    assert [r["linkedIssueId"] for r in streak] == ["a"]


def test_no_checkout_streak_covers_whole_run_list_when_all_miss():
    runs = [make_run(5, "a"), make_run(20, "b"), make_run(35, "c")]
    issues = {k: miss_issue(k) for k in ("a", "b", "c")}
    streak, oldest = no_checkout_streak(runs, _issue_map_fetcher(issues))
    assert len(streak) == 3
    assert oldest == NOW - timedelta(minutes=35)


# --- queued_never_started / wedged_checkout_miss / wedged_checkout_streak (DAN-703) ---

def test_queued_never_started_true_for_queued_run_with_no_timestamps():
    assert queued_never_started(queued_never_started_run()) is True


def test_queued_never_started_false_when_run_is_terminal():
    for status in ("succeeded", "failed", "cancelled", "interrupted", "timed_out"):
        assert queued_never_started({"status": status, "startedAt": None, "claimedAt": None}) is False


def test_queued_never_started_false_when_run_has_started():
    assert queued_never_started({"status": "running", "startedAt": _ts(NOW), "claimedAt": _ts(NOW)}) is False


def test_queued_never_started_false_when_run_is_none():
    assert queued_never_started(None) is False


def test_wedged_checkout_miss_true_for_in_progress_no_checkout_run_id_and_queued_never_started_execution_run():
    assert wedged_checkout_miss(wedged_issue(), queued_never_started_run()) is True


def test_wedged_checkout_miss_false_when_checkout_run_id_is_set():
    # checked_out_issue() has both lock fields set -- this is NOT the wedge
    # shape, it is a genuine checkout, regardless of what the run resolves to.
    assert wedged_checkout_miss(checked_out_issue(), queued_never_started_run()) is False


def test_wedged_checkout_miss_false_when_execution_run_id_is_null():
    assert wedged_checkout_miss(miss_issue(), queued_never_started_run()) is False


def test_wedged_checkout_miss_false_when_resolved_run_has_already_dispatched():
    assert wedged_checkout_miss(wedged_issue(), dispatched_run()) is False


def test_wedged_checkout_miss_false_when_issue_is_none():
    assert wedged_checkout_miss(None, queued_never_started_run()) is False


def test_wedged_checkout_streak_stops_at_first_non_wedge():
    runs = [make_run(5, "a"), make_run(20, "b"), make_run(35, "c")]
    issues = {
        "a": wedged_issue("a", "run-a"),
        "b": checked_out_issue("b"),
        "c": wedged_issue("c", "run-c"),
    }
    exec_runs = {"run-a": queued_never_started_run("run-a"), "run-c": queued_never_started_run("run-c")}
    streak, oldest = wedged_checkout_streak(runs, _issue_map_fetcher(issues), _run_map_fetcher(exec_runs))
    assert [r["linkedIssueId"] for r in streak] == ["a"]
    assert oldest == NOW - timedelta(minutes=5)


def test_wedged_checkout_streak_is_empty_when_newest_run_is_not_wedged():
    runs = [make_run(5, "a")]
    issues = {"a": miss_issue("a")}
    streak, oldest = wedged_checkout_streak(runs, _issue_map_fetcher(issues), _run_map_fetcher({}))
    assert streak == []
    assert oldest is None


def test_wedged_checkout_streak_covers_whole_run_list_when_all_wedged():
    runs = [make_run(5, "a"), make_run(20, "b")]
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    exec_runs = {"run-a": queued_never_started_run("run-a"), "run-b": queued_never_started_run("run-b")}
    streak, oldest = wedged_checkout_streak(runs, _issue_map_fetcher(issues), _run_map_fetcher(exec_runs))
    assert len(streak) == 2
    assert oldest == NOW - timedelta(minutes=20)


# --- seat_occupancy_counts / shared_quota_boundary (DAN-703) ---

def test_seat_occupancy_counts_tallies_running_and_queued():
    runs = [{"status": "running"}, {"status": "queued"}, {"status": "queued"}, {"status": "succeeded"}]
    assert seat_occupancy_counts(runs) == {"running": 1, "queued": 2}


def test_seat_occupancy_counts_is_zeroes_for_empty_list():
    assert seat_occupancy_counts([]) == {"running": 0, "queued": 0}


def test_shared_quota_boundary_finds_matching_instant_on_another_seat():
    stuck = {"retryOfRunId": "prior-run", "scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-1"}
    others = [{"scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-2"}]
    assert shared_quota_boundary(stuck, others) == "2026-10-07T02:00:00.000Z"


def test_shared_quota_boundary_none_when_run_is_not_a_retry_successor():
    stuck = {"retryOfRunId": None, "scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-1"}
    others = [{"scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-2"}]
    assert shared_quota_boundary(stuck, others) is None


def test_shared_quota_boundary_none_when_no_scheduled_retry():
    stuck = {"retryOfRunId": "prior-run", "scheduledRetryAt": None, "agentId": "seat-1"}
    others = [{"scheduledRetryAt": None, "agentId": "seat-2"}]
    assert shared_quota_boundary(stuck, others) is None


def test_shared_quota_boundary_none_when_only_the_same_seat_shares_the_instant():
    stuck = {"retryOfRunId": "prior-run", "scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-1"}
    others = [{"scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-1"}]
    assert shared_quota_boundary(stuck, others) is None


def test_shared_quota_boundary_none_when_run_is_none():
    assert shared_quota_boundary(None, [{"scheduledRetryAt": "x", "agentId": "seat-2"}]) is None


# --- evaluate_routine ---

def test_evaluate_routine_fires_when_streak_spans_at_least_2x_cadence():
    trigger = trigger_with_cadence(15)  # 2x threshold = 30min
    runs = [make_run(5, "a"), make_run(31, "b")]
    issues = {"a": miss_issue("a"), "b": miss_issue("b")}
    finding = evaluate_routine(make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW)
    assert finding is not None
    assert finding["cadence_seconds"] == 15 * 60
    assert len(finding["streak"]) == 2
    assert finding["kind"] == "no_checkout"


def test_evaluate_routine_silent_below_2x_cadence_threshold():
    trigger = trigger_with_cadence(15)  # 2x threshold = 30min
    runs = [make_run(5, "a"), make_run(20, "b")]  # oldest only 20min back
    issues = {"a": miss_issue("a"), "b": miss_issue("b")}
    finding = evaluate_routine(make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW)
    assert finding is None


def test_evaluate_routine_silent_when_no_streak_at_all():
    trigger = trigger_with_cadence(15)
    runs = [make_run(5, "a")]
    issues = {"a": checked_out_issue("a")}
    finding = evaluate_routine(make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW)
    assert finding is None


def test_evaluate_routine_silent_when_cadence_unknown():
    trigger = {"lastFiredAt": None, "nextRunAt": None}
    runs = [make_run(100, "a")]
    issues = {"a": miss_issue("a")}
    finding = evaluate_routine(make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW)
    assert finding is None


# --- evaluate_routine: DAN-680 wedge arm (DAN-703) ---

def test_evaluate_routine_fires_for_wedged_queued_never_started_run():
    """The DAN-680 shape itself: an `in_progress` issue with
    `checkoutRunId: null` whose `executionRunId` resolves to a run that is
    queued and has never started, elapsed past 2x the routine's cadence.
    `no_checkout_streak` alone cannot see this (`executionRunId` is not
    null) -- only passing `fetch_run` activates the sibling wedge arm that
    does."""
    trigger = trigger_with_cadence(15)  # 2x threshold = 30min
    runs = [make_run(5, "a"), make_run(31, "b")]
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    exec_runs = {"run-a": queued_never_started_run("run-a"), "run-b": queued_never_started_run("run-b")}
    finding = evaluate_routine(
        make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW,
        fetch_run=_run_map_fetcher(exec_runs),
    )
    assert finding is not None
    assert finding["kind"] == "wedged"
    assert len(finding["streak"]) == 2


def test_evaluate_routine_without_fetch_run_does_not_see_the_wedge_shape():
    """Backward compatibility: omitting `fetch_run` (as every DAN-666-era
    caller does) must behave exactly as before DAN-703 -- the wedge shape
    stays invisible rather than silently misclassified as a no-checkout
    miss."""
    trigger = trigger_with_cadence(15)
    runs = [make_run(5, "a"), make_run(31, "b")]
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    finding = evaluate_routine(make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW)
    assert finding is None


def test_evaluate_routine_wedge_arm_silent_below_2x_cadence_threshold():
    trigger = trigger_with_cadence(15)  # 2x threshold = 30min
    runs = [make_run(5, "a"), make_run(20, "b")]  # oldest only 20min back
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    exec_runs = {"run-a": queued_never_started_run("run-a"), "run-b": queued_never_started_run("run-b")}
    finding = evaluate_routine(
        make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW,
        fetch_run=_run_map_fetcher(exec_runs),
    )
    assert finding is None


def test_evaluate_routine_wedge_arm_silent_once_run_has_dispatched():
    trigger = trigger_with_cadence(15)
    runs = [make_run(5, "a"), make_run(31, "b")]
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    exec_runs = {"run-a": dispatched_run("run-a"), "run-b": dispatched_run("run-b")}
    finding = evaluate_routine(
        make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW,
        fetch_run=_run_map_fetcher(exec_runs),
    )
    assert finding is None


def test_evaluate_routine_wedge_finding_attaches_seat_occupancy_context():
    trigger = trigger_with_cadence(15)
    runs = [make_run(5, "a"), make_run(31, "b")]
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-b")}
    exec_runs = {
        "run-a": queued_never_started_run("run-a", agent_id="seat-9"),
        "run-b": queued_never_started_run("run-b", agent_id="seat-9"),
    }
    seat_runs = [{"status": "running"}, {"status": "queued"}, {"status": "queued"}]
    finding = evaluate_routine(
        make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW,
        fetch_run=_run_map_fetcher(exec_runs),
        fetch_seat_runs=lambda seat_id: seat_runs if seat_id == "seat-9" else [],
    )
    assert finding is not None
    assert finding["wedge_context"]["seat_agent_id"] == "seat-9"
    assert finding["wedge_context"]["running"] == 1
    assert finding["wedge_context"]["queued"] == 2


def test_evaluate_routine_wedge_finding_attaches_quota_boundary_when_shared():
    trigger = trigger_with_cadence(15)
    runs = [make_run(5, "a"), make_run(31, "b")]
    stuck_run = queued_never_started_run(
        "run-a", agent_id="seat-9", retryOfRunId="prior-run", scheduledRetryAt="2026-10-07T02:00:00.000Z",
    )
    issues = {"a": wedged_issue("a", "run-a"), "b": wedged_issue("b", "run-a")}
    company_runs = [{"scheduledRetryAt": "2026-10-07T02:00:00.000Z", "agentId": "seat-2"}]
    finding = evaluate_routine(
        make_routine(), trigger, runs, _issue_map_fetcher(issues), NOW,
        fetch_run=_run_map_fetcher({"run-a": stuck_run}),
        fetch_company_runs=lambda: company_runs,
    )
    assert finding is not None
    assert finding["wedge_context"]["quota_boundary_ts"] == "2026-10-07T02:00:00.000Z"


# --- compute_findings ---

def test_compute_findings_excludes_self_routine():
    trigger = trigger_with_cadence(15)
    self_routine = make_routine(routine_id="self-id", triggers=[trigger])
    routines = [self_routine]
    runs = [make_run(5, "a"), make_run(40, "b")]
    issues = {"a": miss_issue("a"), "b": miss_issue("b")}
    findings = compute_findings(
        routines, lambda rid: runs, _issue_map_fetcher(issues), NOW, self_routine_id="self-id",
    )
    assert findings == []


def test_compute_findings_skips_inactive_routines():
    trigger = trigger_with_cadence(15)
    routine = make_routine(status="paused", triggers=[trigger])
    runs = [make_run(5, "a"), make_run(40, "b")]
    issues = {"a": miss_issue("a"), "b": miss_issue("b")}
    findings = compute_findings(
        [routine], lambda rid: runs, _issue_map_fetcher(issues), NOW, self_routine_id="other-self-id",
    )
    assert findings == []


def test_compute_findings_skips_routines_without_a_schedule_trigger():
    routine = make_routine(triggers=[{"kind": "webhook", "enabled": True, "archived": False}])
    findings = compute_findings(
        [routine], lambda rid: [], _issue_map_fetcher({}), NOW, self_routine_id="other-self-id",
    )
    assert findings == []


def test_compute_findings_reports_only_qualifying_routines():
    stale_trigger = trigger_with_cadence(15)
    healthy_trigger = trigger_with_cadence(15)
    stale_routine = make_routine(routine_id="stale", triggers=[stale_trigger])
    healthy_routine = make_routine(routine_id="healthy", triggers=[healthy_trigger], title="Healthy routine")

    def fetch_runs(routine_id):
        if routine_id == "stale":
            return [make_run(5, "a"), make_run(40, "b")]
        return [make_run(5, "c")]

    issues = {"a": miss_issue("a"), "b": miss_issue("b"), "c": checked_out_issue("c")}
    findings = compute_findings(
        [stale_routine, healthy_routine], fetch_runs, _issue_map_fetcher(issues), NOW,
        self_routine_id="other-self-id",
    )
    assert len(findings) == 1
    assert findings[0]["routine"]["id"] == "stale"


# --- finding_marker / finding_title / is_tracking_issue ---

def test_finding_marker_is_stable_per_routine_id():
    assert finding_marker("abc") == "[abc]"


def test_is_tracking_issue_matches_exact_marker_only():
    assert is_tracking_issue({"title": "Routine checkout watchdog: X -- 1 tick(s) [abc]"}, "abc") is True
    assert is_tracking_issue({"title": "Routine checkout watchdog: X -- 1 tick(s) [abcd]"}, "abc") is False
    assert is_tracking_issue({"title": None}, "abc") is False


def test_finding_title_embeds_routine_marker_and_streak_count():
    finding = {"routine": {"id": "abc", "title": "My Routine"}, "streak": [1, 2, 3]}
    title = finding_title(finding)
    assert "[abc]" in title
    assert "3" in title


# --- find_existing_tracking_issue ---

def test_find_existing_tracking_issue_prefers_open_over_closed():
    closed = {"id": "closed-1", "status": "done", "title": "x [abc]", "createdAt": _ts(NOW - timedelta(days=1))}
    open_ = {"id": "open-1", "status": "todo", "title": "x [abc]", "createdAt": _ts(NOW)}

    def fake_get(path):
        return [closed, open_]

    found = find_existing_tracking_issue("company", "abc", api_get_fn=fake_get)
    assert found["id"] == "open-1"


def test_find_existing_tracking_issue_ties_break_on_earliest_created():
    a = {"id": "a", "status": "todo", "title": "x [abc]", "createdAt": _ts(NOW)}
    b = {"id": "b", "status": "todo", "title": "x [abc]", "createdAt": _ts(NOW - timedelta(hours=1))}

    def fake_get(path):
        return [a, b]

    found = find_existing_tracking_issue("company", "abc", api_get_fn=fake_get)
    assert found["id"] == "b"


def test_find_existing_tracking_issue_filters_out_non_matching_marker():
    other = {"id": "other", "status": "todo", "title": "x [xyz]", "createdAt": _ts(NOW)}

    def fake_get(path):
        return [other]

    assert find_existing_tracking_issue("company", "abc", api_get_fn=fake_get) is None


def test_find_existing_tracking_issue_returns_none_when_no_matches():
    def fake_get(path):
        return []

    assert find_existing_tracking_issue("company", "abc", api_get_fn=fake_get) is None


# --- describe_finding ---

def _finding(routine_id="abc", title="My Routine", owner="owner-1", streak_len=2, cadence_min=15, elapsed_min=40):
    streak = [make_run(5 * (i + 1), f"issue-{i}") for i in range(streak_len)]
    return {
        "routine": {"id": routine_id, "title": title, "assigneeAgentId": owner},
        "cadence_seconds": cadence_min * 60,
        "streak": streak,
        "oldest_triggered_at": NOW - timedelta(minutes=elapsed_min),
        "elapsed_seconds": elapsed_min * 60,
    }


def test_describe_finding_mentions_owner_and_states_the_marker():
    body = describe_finding(_finding(), {"owner-1": "Virgil"})
    assert MARKER in body
    assert "[@Virgil](agent://owner-1)" in body


def test_describe_finding_states_no_owner_when_routine_is_unassigned():
    finding = _finding(owner=None)
    body = describe_finding(finding, {})
    assert "no owner" in body


def test_describe_finding_omits_cloud_mention_when_not_escalating():
    body = describe_finding(_finding(), {}, escalate=False)
    assert ESCALATION_AGENT_ID not in body


def test_describe_finding_mentions_cloud_when_escalating():
    body = describe_finding(_finding(), {}, escalate=True)
    assert ESCALATION_AGENT_ID in body
    assert "escalating" in body.lower()


def _wedge_finding(seat_agent_id="seat-9", running=1, queued=2, quota_boundary_ts=None, **kwargs):
    finding = _finding(**kwargs)
    finding["kind"] = "wedged"
    finding["wedge_context"] = {"seat_agent_id": seat_agent_id, "running": running, "queued": queued}
    if quota_boundary_ts:
        finding["wedge_context"]["quota_boundary_ts"] = quota_boundary_ts
    return finding


def test_describe_finding_states_queued_never_started_shape_for_wedged_kind():
    body = describe_finding(_wedge_finding(), {})
    assert "DAN-680" in body
    assert "queued-and-never-started" in body or "queued and has never started" in body


def test_describe_finding_states_seat_occupancy_counts_for_wedged_kind():
    body = describe_finding(_wedge_finding(seat_agent_id="seat-9", running=1, queued=2), {"seat-9": "Minos"})
    assert "[@Minos](agent://seat-9)" in body
    assert "1" in body and "2" in body
    assert "not by itself evidence that it is dead" in body


def test_describe_finding_states_quota_boundary_note_when_present():
    body = describe_finding(_wedge_finding(quota_boundary_ts="2026-10-07T02:00:00.000Z"), {})
    assert "2026-10-07T02:00:00.000Z" in body
    assert "quota" in body.lower()


def test_describe_finding_omits_quota_boundary_note_when_absent():
    body = describe_finding(_wedge_finding(quota_boundary_ts=None), {})
    assert "quota/clock reset boundary" not in body


def test_describe_finding_non_wedged_kind_keeps_the_original_no_checkout_wording():
    finding = _finding()
    finding["kind"] = "no_checkout"
    body = describe_finding(finding, {})
    assert "never checked out" in body
    assert "DAN-680" not in body


# --- should_notify ---

def test_should_notify_true_when_no_prior_marker_comment():
    assert should_notify([], NOW) is True


def test_should_notify_false_within_renotify_window():
    comments = [{"body": f"x {MARKER}", "createdAt": _ts(NOW - timedelta(minutes=10))}]
    assert should_notify(comments, NOW) is False


def test_should_notify_true_once_renotify_window_has_elapsed():
    comments = [{"body": f"x {MARKER}", "createdAt": _ts(NOW - timedelta(seconds=RENOTIFY_INTERVAL_SECONDS + 1))}]
    assert should_notify(comments, NOW) is True


def test_should_notify_ignores_comments_without_the_marker():
    comments = [{"body": "unrelated", "createdAt": _ts(NOW)}]
    assert should_notify(comments, NOW) is True


# --- is_actionable ---

def test_is_actionable_true_only_for_filed():
    assert is_actionable("filed") is True
    for action in ("updated", "resumed", "throttled"):
        assert is_actionable(action) is False


# --- file_or_update_finding ---

def test_file_or_update_finding_files_a_new_tracker_when_none_exists():
    posts = []

    def fake_get(path):
        return []

    def fake_post(path, body):
        posts.append((path, body))
        return {"id": "new-tracker", "identifier": "DAN-900", "status": "todo"}

    issue, action = file_or_update_finding(
        "company", _finding(), {}, "project-x", "dante-id", NOW,
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=lambda *a: {},
    )
    assert action == "filed"
    assert issue["id"] == "new-tracker"
    assert len(posts) == 1
    assert posts[0][1]["assigneeAgentId"] == "dante-id"
    assert posts[0][1]["projectId"] == "project-x"


def test_file_or_update_finding_updates_and_escalates_an_open_tracker_past_renotify_window():
    existing = {"id": "open-1", "status": "todo", "title": finding_title(_finding())}
    posts = []

    def fake_get(path):
        if path.endswith("/comments"):
            return []
        return [existing]

    def fake_post(path, body):
        posts.append((path, body))
        return {}

    issue, action = file_or_update_finding(
        "company", _finding(), {}, "project-x", "dante-id", NOW,
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=lambda *a: {},
    )
    assert action == "updated"
    assert issue["id"] == "open-1"
    assert len(posts) == 1
    assert ESCALATION_AGENT_ID in posts[0][1]["body"]


def test_file_or_update_finding_throttles_an_open_tracker_within_renotify_window():
    existing = {"id": "open-1", "status": "todo", "title": finding_title(_finding())}
    recent_comment = [{"body": f"x {MARKER}", "createdAt": _ts(NOW - timedelta(minutes=5))}]
    posts = []

    def fake_get(path):
        if path.endswith("/comments"):
            return recent_comment
        return [existing]

    def fake_post(path, body):
        posts.append((path, body))
        return {}

    issue, action = file_or_update_finding(
        "company", _finding(), {}, "project-x", "dante-id", NOW,
        api_get_fn=fake_get, api_post_fn=fake_post, api_patch_fn=lambda *a: {},
    )
    assert action == "throttled"
    assert posts == []


def test_file_or_update_finding_resumes_and_escalates_a_closed_tracker():
    existing = {"id": "closed-1", "status": "done", "title": finding_title(_finding())}
    patches = []

    def fake_get(path):
        if path.endswith("/comments"):
            return []
        return [existing]

    def fake_patch(path, body):
        patches.append((path, body))
        return {}

    issue, action = file_or_update_finding(
        "company", _finding(), {}, "project-x", "dante-id", NOW,
        api_get_fn=fake_get, api_post_fn=lambda *a: {}, api_patch_fn=fake_patch,
    )
    assert action == "resumed"
    assert issue["id"] == "closed-1"
    assert len(patches) == 1
    assert patches[0][1]["status"] == "todo"
    assert patches[0][1]["resume"] is True
    assert ESCALATION_AGENT_ID in patches[0][1]["comment"]


# --- agent_names (monkeypatches the module-level api_get, like main() uses it) ---

def test_agent_names_maps_id_to_display_name():
    original = rcw.api_get
    rcw.api_get = lambda path: [{"id": "a1", "name": "Virgil"}, {"id": "a2", "displayName": "Minos"}]
    try:
        names = agent_names("company")
    finally:
        rcw.api_get = original
    assert names == {"a1": "Virgil", "a2": "Minos"}


def test_agent_names_is_empty_dict_on_failure_not_an_exception():
    original = rcw.api_get

    def boom(path):
        raise RuntimeError("network down")

    rcw.api_get = boom
    try:
        names = agent_names("company")
    finally:
        rcw.api_get = original
    assert names == {}


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
