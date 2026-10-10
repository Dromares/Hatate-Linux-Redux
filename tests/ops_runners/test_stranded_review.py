#!/usr/bin/env python3
"""Plain-assert tests for ops/stranded_review.py (DAN-267, DAN-380, DAN-385,
DAN-727/DAN-918).

No pytest dependency -- run directly:
    python3 ops/test_stranded_review.py

Covers the DAN-267 acceptance bar, the DAN-380/DAN-385 wake-reachability and
fingerprint fixes, and DAN-727's two false-escalation fixes:
  - DAN-551: a stale, sole unclaimed on-issue wake must not by itself prove
    `unreachable_participant` (staleness check).
  - DAN-858 (reported as DAN-918, folded in): a dropped on-issue wake must
    not override corroborating seat-level `heartbeat-runs` evidence showing
    the participant is actually active (stranded-wake classification).
  - The remedy ladder always pokes first; CEO escalation is reached only
    after a prior poke demonstrably produced no claimed wake, or for a
    genuine `reconciliation_hold`.
  - The read-to-post race is closed by a fresh re-read immediately before
    posting.
"""
from datetime import datetime, timedelta, timezone

import stranded_review
from stranded_review import (
    ESCALATION_AGENT_ID,
    FINGERPRINT_MARKER,
    MARKER,
    STALE_WAKE_EVIDENCE_SECONDS,
    can_advance_via_execution,
    classification_fingerprint,
    classify_finding,
    classify_seat_reachability,
    compute_findings,
    decide_strand_action,
    describe_escalation,
    describe_poke,
    evaluate_issue,
    refresh_is_resolved,
    should_poke,
    wake_reachability,
)

NOW = datetime(2026, 10, 3, 20, 37, 26, tzinfo=timezone.utc)
MINOS = "500355a5-9f7f-4881-8a9c-6ed3447f1878"
VIRGIL = "d67c9967-9a1c-43c9-b3e4-bc079aa8f74e"


def _ts(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_record(
    status="in_review",
    origin_kind="manual",
    active_run=None,
    execution_phase="recovery_needed",
    participant_agent_id=MINOS,
    return_assignee_agent_id=VIRGIL,
    since_minutes_ago=45,
    last_activity_minutes_ago=None,
    use_execution_participant_path=True,
    last_decision_id=None,
    last_decision_outcome=None,
    issue_id="issue-under-test",
    wakes=None,
    permitted_actions=("inspect_run",),
    execution_blocker=None,
):
    """Builds a merged record in the exact shape fetch_records() produces,
    parameterised the way the real DAN-213/DAN-236 state looked."""
    since_dt = NOW - timedelta(minutes=since_minutes_ago)
    paths = []
    if use_execution_participant_path:
        paths.append(
            {
                "kind": "execution_participant",
                "label": "Execution review participant",
                "responder": "Minos",
                "since": _ts(since_dt),
                "ref": participant_agent_id,
            }
        )
    last_activity = (
        _ts(NOW - timedelta(minutes=last_activity_minutes_ago))
        if last_activity_minutes_ago is not None
        else _ts(since_dt)
    )
    execution_state = None
    if participant_agent_id is not None:
        execution_state = {
            "status": "pending",
            "currentStageId": "stage-1",
            "currentStageType": "approval",
            "currentParticipant": {"type": "agent", "userId": None, "agentId": participant_agent_id},
            "returnAssignee": {"type": "agent", "userId": None, "agentId": return_assignee_agent_id},
            "lastDecisionId": last_decision_id,
            "lastDecisionOutcome": last_decision_outcome,
        }
    execution = None
    if execution_phase is not None:
        execution = {
            "execution": {
                "phase": execution_phase,
                "cause": "legacy_execution_requires_reconciliation",
                "permittedActions": list(permitted_actions),
            }
        }
    return {
        "id": issue_id,
        "identifier": issue_id.upper(),
        "status": status,
        "originKind": origin_kind,
        "activeRun": active_run,
        "lastActivityAt": last_activity,
        "executionState": execution_state,
        "executionBlocker": execution_blocker,
        "reviewAttention": {"paths": paths},
        "checkoutRunId": None,
        "executionRunId": None,
        "executionLockedAt": None,
        "execution": execution,
        "wakes": wakes,
    }


def make_wake_event(agent_id, status, claimed=True, requested_minutes_ago=5, run_id=None):
    requested_at = NOW - timedelta(minutes=requested_minutes_ago)
    return {
        "agentId": agent_id,
        "status": status,
        "requestedAt": _ts(requested_at),
        "claimedAt": _ts(requested_at + timedelta(seconds=1)) if claimed else None,
        "runId": run_id,
    }


def make_wakes(events):
    """Builds a `wakes` payload in the shape of GET .../diagnostics/wakes,
    `events` already in newest-first order as the real endpoint returns."""
    return {"events": events}


def make_run(
    run_id,
    status="succeeded",
    error_code=None,
    finished_minutes_ago=10,
    retry_of_run_id=None,
    scheduled_retry_at=None,
):
    return {
        "id": run_id,
        "status": status,
        "errorCode": error_code,
        "finishedAt": _ts(NOW - timedelta(minutes=finished_minutes_ago)) if finished_minutes_ago is not None else None,
        "retryOfRunId": retry_of_run_id,
        "scheduledRetryAt": scheduled_retry_at,
    }


def classify(finding, seat_runs=None):
    return classify_finding(finding, NOW, seat_runs)


def test_stranded_issue_fires():
    record = make_record(since_minutes_ago=45, execution_phase="recovery_needed", active_run=None)
    finding = evaluate_issue(record, NOW)
    assert finding is not None
    assert finding["participant"]["agentId"] == MINOS


def test_pending_stage_with_live_run_stays_silent():
    live_run = {"id": "run-1", "status": "running"}
    record = make_record(since_minutes_ago=45, execution_phase="working", active_run=live_run)
    assert evaluate_issue(record, NOW) is None


def test_pending_stage_younger_than_threshold_stays_silent():
    # Mirrors the real DAN-255 shape seen live: phase already failed/dead,
    # but lastActivityAt (and the execution_participant path) is ~1 minute
    # old -- must not fire yet.
    record = make_record(since_minutes_ago=1, execution_phase="failed", active_run=None)
    assert evaluate_issue(record, NOW) is None


def test_done_issue_never_fires():
    record = make_record(status="done", since_minutes_ago=120)
    assert evaluate_issue(record, NOW) is None


def test_cancelled_issue_never_fires():
    record = make_record(status="cancelled", since_minutes_ago=120)
    assert evaluate_issue(record, NOW) is None


def test_routine_execution_issue_never_fires():
    record = make_record(origin_kind="routine_execution", since_minutes_ago=120)
    assert evaluate_issue(record, NOW) is None


def test_no_participant_does_not_fire():
    record = make_record(participant_agent_id=None, since_minutes_ago=120)
    assert evaluate_issue(record, NOW) is None


def test_threshold_boundary_exactly_30_minutes_fires():
    record = make_record(since_minutes_ago=30, execution_phase="failed", active_run=None)
    assert evaluate_issue(record, NOW) is not None


def test_falls_back_to_last_activity_when_no_execution_participant_path():
    record = make_record(
        since_minutes_ago=45,
        use_execution_participant_path=False,
        last_activity_minutes_ago=45,
        execution_phase="failed",
        active_run=None,
    )
    finding = evaluate_issue(record, NOW)
    assert finding is not None
    assert finding["since"] == NOW - timedelta(minutes=45)


def test_compute_findings_handles_mixed_batch():
    stranded = make_record(issue_id="stranded-one", since_minutes_ago=60, execution_phase="failed")
    silent_live_run = make_record(
        issue_id="live-one", since_minutes_ago=60, execution_phase="working", active_run={"id": "r"}
    )
    silent_too_young = make_record(issue_id="young-one", since_minutes_ago=5, execution_phase="failed")
    silent_done = make_record(issue_id="done-one", status="done", since_minutes_ago=120)
    findings = compute_findings([stranded, silent_live_run, silent_too_young, silent_done], NOW)
    assert len(findings) == 1
    assert findings[0]["record"]["id"] == "stranded-one"


# --- Re-poke dedup ---


FP_A = "strand=reachable,action=poke,can_advance=False"
FP_B = "strand=unreachable_participant,action=escalate,can_advance=False"


def test_should_poke_true_with_no_prior_marker():
    assert should_poke([], NOW, FP_A) is True
    assert should_poke([{"body": "unrelated comment", "createdAt": _ts(NOW)}], NOW, FP_A) is True


def test_should_poke_false_inside_repoke_window():
    comments = [{"body": f"poked earlier {MARKER}", "createdAt": _ts(NOW - timedelta(hours=2))}]
    assert should_poke(comments, NOW, FP_A) is False


def test_should_poke_true_after_repoke_window():
    comments = [{"body": f"poked earlier {MARKER}", "createdAt": _ts(NOW - timedelta(hours=7))}]
    assert should_poke(comments, NOW, FP_A) is True


def test_should_poke_uses_most_recent_marker():
    comments = [
        {"body": f"first poke {MARKER}", "createdAt": _ts(NOW - timedelta(hours=10))},
        {"body": f"second poke {MARKER}", "createdAt": _ts(NOW - timedelta(hours=1))},
    ]
    assert should_poke(comments, NOW, FP_A) is False


# --- DAN-385 approver follow-up: fingerprint change forces a re-poke even
# inside the 6h window; a legacy marker with no fingerprint line falls back
# to window-only behaviour.


def test_should_poke_false_inside_window_when_fingerprint_unchanged():
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n{FINGERPRINT_MARKER}{FP_A} -->",
            "createdAt": _ts(NOW - timedelta(hours=2)),
        }
    ]
    assert should_poke(comments, NOW, FP_A) is False


def test_should_poke_true_inside_window_when_fingerprint_changed():
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n{FINGERPRINT_MARKER}{FP_A} -->",
            "createdAt": _ts(NOW - timedelta(hours=2)),
        }
    ]
    assert should_poke(comments, NOW, FP_B) is True, (
        "a worsened finding (e.g. participant flips reachable -> unreachable) "
        "must not be silenced by the repoke window"
    )


def test_should_poke_legacy_marker_without_fingerprint_falls_back_to_window():
    comments = [{"body": f"poked earlier {MARKER}", "createdAt": _ts(NOW - timedelta(hours=2))}]
    assert should_poke(comments, NOW, FP_B) is False


def test_classification_fingerprint_changes_when_facts_change():
    reachable = classification_fingerprint(
        {"strand_class": "reachable", "dropped": 0, "can_advance": False}, "poke"
    )
    unreachable = classification_fingerprint(
        {"strand_class": "unreachable_participant", "dropped": 2, "can_advance": False}, "escalate"
    )
    assert reachable != unreachable
    assert reachable == classification_fingerprint(
        {"strand_class": "reachable", "dropped": 0, "can_advance": False}, "poke"
    )


def test_classification_fingerprint_ignores_dropped_count():
    base = {"strand_class": "unreachable_participant", "can_advance": False}
    same_dropped_0 = classification_fingerprint({**base, "dropped": 0}, "escalate")
    same_dropped_4 = classification_fingerprint({**base, "dropped": 4}, "escalate")
    same_dropped_5 = classification_fingerprint({**base, "dropped": 5}, "escalate")
    assert same_dropped_0 == same_dropped_4 == same_dropped_5, (
        "dropped climbing on its own (e.g. from this detector's own poke comments "
        "generating more dropped wakes) must not look like a changed finding"
    )


def test_classification_fingerprint_changes_when_action_changes():
    base = {"strand_class": "unreachable_participant", "dropped": 1, "can_advance": False}
    poke_fp = classification_fingerprint(base, "poke")
    escalate_fp = classification_fingerprint(base, "escalate")
    assert poke_fp != escalate_fp, "a poke->escalate transition must force a re-post"


def test_describe_poke_embeds_fingerprint_and_action_line():
    record = make_record(since_minutes_ago=45, wakes=make_wakes([make_wake_event(MINOS, "cancelled", claimed=True)]))
    finding = evaluate_issue(record, NOW)
    body = describe_poke(finding, NOW, {}, fingerprint=FP_A)
    assert f"{FINGERPRINT_MARKER}{FP_A} -->" in body
    assert "<!-- stranded-review-action:poke -->" in body


# --- DAN-380: wake reachability and age/staleness (DAN-727 req 1/2) ---


def test_wake_reachability_unreachable_when_most_recent_wake_dropped_and_fresh():
    record = make_record(
        wakes=make_wakes([make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=5)])
    )
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is True
    assert result["dropped"] == 1
    assert result["stale"] is False


def test_wake_reachability_reachable_when_most_recent_wake_was_claimed():
    record = make_record(wakes=make_wakes([make_wake_event(MINOS, "cancelled", claimed=True)]))
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is False
    assert result["dropped"] == 0


def test_wake_reachability_reachable_when_no_wake_history():
    record = make_record(wakes=make_wakes([]))
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is False
    assert result["dropped"] == 0


def test_wake_reachability_ignores_other_agents_wakes():
    record = make_record(
        wakes=make_wakes([make_wake_event(VIRGIL, "deferred_issue_execution", claimed=False)])
    )
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is False


def test_wake_reachability_counts_consecutive_dropped_only():
    record = make_record(
        wakes=make_wakes(
            [
                make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=5),
                make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=10),
                make_wake_event(MINOS, "completed", claimed=True, requested_minutes_ago=15),
                make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=20),
            ]
        )
    )
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is True
    assert result["dropped"] == 2


def test_wake_reachability_stale_when_sole_unclaimed_wake_is_old():
    # DAN-551: the sole wake addressed to the participant was ~2 days old.
    record = make_record(
        wakes=make_wakes(
            [
                make_wake_event(
                    MINOS,
                    "deferred_issue_execution",
                    claimed=False,
                    requested_minutes_ago=2 * 24 * 60,
                )
            ]
        )
    )
    result = wake_reachability(record, MINOS, NOW)
    assert result["unreachable"] is True
    assert result["stale"] is True, "a multi-day-old sole wake must be flagged stale, not current evidence"


def test_wake_reachability_not_stale_within_window():
    age_minutes = (STALE_WAKE_EVIDENCE_SECONDS // 60) - 1
    record = make_record(
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=age_minutes)]
        )
    )
    result = wake_reachability(record, MINOS, NOW)
    assert result["stale"] is False


def test_wake_reachability_stale_when_no_timestamp_at_all():
    event = {"agentId": MINOS, "status": "deferred_issue_execution", "claimedAt": None, "requestedAt": None}
    record = make_record(wakes=make_wakes([event]))
    result = wake_reachability(record, MINOS, NOW)
    assert result["stale"] is True


def test_can_advance_via_execution_false_for_inspect_run_only():
    record = make_record(permitted_actions=("inspect_run",))
    assert can_advance_via_execution(record) is False


def test_can_advance_via_execution_true_for_other_action():
    record = make_record(permitted_actions=("inspect_run", "resume"))
    assert can_advance_via_execution(record) is True


# --- DAN-727 req 1/2 (DAN-551): stale sole wake must not drive
# unreachable_participant, even with no seat evidence at all.


def test_classify_finding_stale_wake_is_stranded_wake_not_unreachable():
    record = make_record(
        since_minutes_ago=45,
        wakes=make_wakes(
            [
                make_wake_event(
                    MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=2 * 24 * 60
                )
            ]
        ),
    )
    finding = evaluate_issue(record, NOW)
    classification = classify(finding, seat_runs=None)
    assert classification["strand_class"] == "stranded_wake"
    assert classification["unreachable"] is False


# --- DAN-727 req (DAN-858/DAN-918): fresh dropped wake + healthy seat must
# not drive unreachable_participant either.


def test_classify_finding_fresh_dropped_wake_but_healthy_seat_is_stranded_wake():
    record = make_record(
        since_minutes_ago=45,
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=2)]
        ),
    )
    finding = evaluate_issue(record, NOW)
    healthy_seat_runs = [make_run(f"run-{i}", status="succeeded", finished_minutes_ago=i * 4) for i in range(11)]
    classification = classify(finding, seat_runs=healthy_seat_runs)
    assert classification["strand_class"] == "stranded_wake", (
        "a healthy seat (recent succeeded runs elsewhere) must downgrade a dropped "
        "on-issue wake to a stranded wake, not an unreachable participant"
    )
    assert classification["unreachable"] is False
    assert classification["seat"]["reachable"] is True
    assert classification["seat"]["succeeded"] == 11


def test_classify_finding_fresh_dropped_wake_and_dead_seat_is_unreachable_participant():
    record = make_record(
        since_minutes_ago=45,
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=2)]
        ),
    )
    finding = evaluate_issue(record, NOW)
    dead_seat_runs = [make_run("run-1", status="failed", error_code="unrelated")]
    classification = classify(finding, seat_runs=dead_seat_runs)
    assert classification["strand_class"] == "unreachable_participant"
    assert classification["unreachable"] is True


def test_classify_finding_no_seat_evidence_never_upgrades_to_unreachable():
    record = make_record(
        since_minutes_ago=45,
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=2)]
        ),
    )
    finding = evaluate_issue(record, NOW)
    classification = classify(finding, seat_runs=None)
    assert classification["strand_class"] == "stranded_wake", (
        "missing seat evidence must never by itself support an unreachable_participant claim"
    )


def test_classify_finding_execution_blocker_is_reconciliation_hold():
    record = make_record(since_minutes_ago=45, execution_blocker={"cause": "x", "runId": "r", "agentId": MINOS})
    finding = evaluate_issue(record, NOW)
    classification = classify(finding, seat_runs=None)
    assert classification["strand_class"] == "reconciliation_hold"
    assert classification["unreachable"] is False


# --- classify_seat_reachability ---


def test_classify_seat_reachability_reachable_with_succeeded_runs():
    runs = [make_run(f"r{i}", status="succeeded") for i in range(3)]
    result = classify_seat_reachability(runs, NOW)
    assert result["reachable"] is True
    assert result["succeeded"] == 3


def test_classify_seat_reachability_reachable_with_running_run():
    runs = [make_run("r1", status="running", finished_minutes_ago=None)]
    result = classify_seat_reachability(runs, NOW)
    assert result["reachable"] is True


def test_classify_seat_reachability_not_reachable_with_no_runs():
    result = classify_seat_reachability([], NOW)
    assert result["reachable"] is False
    assert result["total"] == 0


def test_classify_seat_reachability_follows_retry_of_run_id_for_quota():
    failed = make_run("failed-1", status="failed", error_code="provider_quota", finished_minutes_ago=180)
    successor = make_run(
        "successor-1", status="succeeded", retry_of_run_id="failed-1", finished_minutes_ago=30
    )
    result = classify_seat_reachability([failed, successor], NOW)
    assert len(result["quota_runs"]) == 1
    assert result["quota_runs"][0]["successor"]["id"] == "successor-1"
    assert result["reachable"] is True


# --- decide_strand_action: the remedy ladder (DAN-727 req 5) ---


def test_decide_strand_action_pokes_on_first_encounter_even_when_unreachable():
    record = make_record(since_minutes_ago=180)
    assert decide_strand_action("unreachable_participant", [], MINOS, record, NOW) == "poke"


def test_decide_strand_action_escalates_only_after_prior_poke_failed():
    old_poke_ts = NOW - timedelta(minutes=45)
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n<!-- stranded-review-action:poke -->",
            "createdAt": _ts(old_poke_ts),
        }
    ]
    # No wake claimed since the poke.
    record = make_record(
        since_minutes_ago=180,
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=40)]
        ),
    )
    assert decide_strand_action("unreachable_participant", comments, MINOS, record, NOW) == "escalate"


def test_decide_strand_action_stays_poke_when_grace_period_not_elapsed():
    recent_poke_ts = NOW - timedelta(minutes=5)
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n<!-- stranded-review-action:poke -->",
            "createdAt": _ts(recent_poke_ts),
        }
    ]
    record = make_record(since_minutes_ago=180)
    assert decide_strand_action("unreachable_participant", comments, MINOS, record, NOW) == "poke"


def test_decide_strand_action_stays_poke_when_prior_poke_was_claimed():
    old_poke_ts = NOW - timedelta(minutes=45)
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n<!-- stranded-review-action:poke -->",
            "createdAt": _ts(old_poke_ts),
        }
    ]
    record = make_record(
        since_minutes_ago=180,
        wakes=make_wakes([make_wake_event(MINOS, "completed", claimed=True, requested_minutes_ago=40)]),
    )
    assert decide_strand_action("unreachable_participant", comments, MINOS, record, NOW) == "poke"


def test_decide_strand_action_reconciliation_hold_escalates_immediately():
    record = make_record(since_minutes_ago=45)
    assert decide_strand_action("reconciliation_hold", [], MINOS, record, NOW) == "escalate"


def test_decide_strand_action_stranded_wake_never_escalates():
    record = make_record(since_minutes_ago=180)
    # Even with a prior "poke" long ago and nothing claimed, stranded_wake
    # (seat corroborated healthy, or wake stale) must still just poke.
    comments = [
        {
            "body": f"poked earlier {MARKER}\n\n<!-- stranded-review-action:poke -->",
            "createdAt": _ts(NOW - timedelta(hours=10)),
        }
    ]
    assert decide_strand_action("stranded_wake", comments, MINOS, record, NOW) == "poke"


def test_decide_strand_action_reachable_pokes():
    record = make_record(since_minutes_ago=45)
    assert decide_strand_action("reachable", [], MINOS, record, NOW) == "poke"


# --- describe_escalation / describe_poke content ---


def test_describe_escalation_never_says_yours_to_move_and_names_ceo():
    record = make_record(
        since_minutes_ago=180,
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=m) for m in (5, 10, 15)]
        ),
    )
    finding = evaluate_issue(record, NOW)
    classification = classify(finding, seat_runs=[make_run("r1", status="failed")])
    assert classification["strand_class"] == "unreachable_participant"
    body = describe_escalation(finding, NOW, {}, classification)
    assert "this stage is yours to move" not in body
    assert "a non-participant PATCH gets a 422" not in body
    assert ESCALATION_AGENT_ID in body


def test_describe_escalation_reconciliation_hold_names_human_comment_remedy():
    record = make_record(since_minutes_ago=180, execution_blocker={"cause": "x", "runId": "r", "agentId": MINOS})
    finding = evaluate_issue(record, NOW)
    classification = classify(finding, seat_runs=None)
    body = describe_escalation(finding, NOW, {}, classification)
    assert "human board user" in body
    assert "not a dissolution or a `stalled-review-decision` call" in body
    assert "a poke would be an inert no-op" in body, "must not falsely claim a poke was already tried"


def test_describe_poke_never_asserts_unverified_422_claim():
    record = make_record(
        since_minutes_ago=45,
        wakes=make_wakes([make_wake_event(MINOS, "cancelled", claimed=True)]),
    )
    finding = evaluate_issue(record, NOW)
    body = describe_poke(finding, NOW, {})
    assert "this stage is yours to move" not in body
    assert "a non-participant PATCH gets a 422" not in body
    assert "Nothing else will move this" not in body
    assert MINOS in body


def test_describe_poke_labels_run_scoped_evidence():
    record = make_record(since_minutes_ago=45, wakes=make_wakes([make_wake_event(MINOS, "cancelled", claimed=True)]))
    finding = evaluate_issue(record, NOW)
    body = describe_poke(finding, NOW, {})
    assert "describes that run, not the" in body


# --- refresh_is_resolved (DAN-727 req 4): mocked network ---


def _with_mocked_api_get(payload, fn):
    original = stranded_review.api_get
    stranded_review.api_get = lambda path: payload
    try:
        return fn()
    finally:
        stranded_review.api_get = original


def test_refresh_is_resolved_true_when_status_done():
    assert _with_mocked_api_get({"status": "done", "executionState": {}}, lambda: refresh_is_resolved("x")) is True


def test_refresh_is_resolved_true_when_execution_completed():
    payload = {"status": "in_review", "executionState": {"status": "completed", "currentStageId": "s1"}}
    assert _with_mocked_api_get(payload, lambda: refresh_is_resolved("x")) is True


def test_refresh_is_resolved_true_when_decision_recorded():
    payload = {
        "status": "in_review",
        "executionState": {"status": "pending", "currentStageId": "s1", "lastDecisionOutcome": "approved"},
    }
    assert _with_mocked_api_get(payload, lambda: refresh_is_resolved("x")) is True


def test_refresh_is_resolved_true_when_no_current_stage():
    payload = {"status": "in_review", "executionState": {"status": "pending", "currentStageId": None}}
    assert _with_mocked_api_get(payload, lambda: refresh_is_resolved("x")) is True


def test_refresh_is_resolved_false_when_still_genuinely_pending():
    payload = {
        "status": "in_review",
        "executionState": {"status": "pending", "currentStageId": "s1", "lastDecisionOutcome": None},
    }
    assert _with_mocked_api_get(payload, lambda: refresh_is_resolved("x")) is False


def test_refresh_is_resolved_false_on_fetch_failure():
    def boom(path):
        raise RuntimeError("network down")

    original = stranded_review.api_get
    stranded_review.api_get = boom
    try:
        assert refresh_is_resolved("x") is False
    finally:
        stranded_review.api_get = original


# --- DAN-727 req 6: replay DAN-551's recorded state ---
#
# Facts from the ticket (GET /api/issues/d5445c7f-2be1-407d-ac55-e8dd84ad04c3
# and GET .../diagnostics/wakes, 2026-10-07):
#   - sole wake to Minos on this issue: requested 2026-10-05T01:01:05.904Z,
#     deferred_issue_execution, claimedAt null.
#   - at 03:35:20.072Z (the old detector's escalation time) the issue was
#     still executionState.status == "pending", lastDecisionOutcome null.
#   - at 03:36:48.159Z (87s later) the issue was done, executionState
#     completed, lastDecisionOutcome approved.


DAN_551_READ_TIME = datetime(2026, 10, 7, 3, 35, 20, 72000, tzinfo=timezone.utc)
DAN_551_WAKE_REQUESTED_AT = "2026-10-05T01:01:05.904Z"


def test_dan_551_replay_at_escalation_time_pokes_not_escalates():
    record = make_record(
        issue_id="dan-551",
        since_minutes_ago=180,
        wakes=make_wakes(
            [{"agentId": MINOS, "status": "deferred_issue_execution", "requestedAt": DAN_551_WAKE_REQUESTED_AT, "claimedAt": None, "runId": None}]
        ),
    )
    finding = evaluate_issue(record, DAN_551_READ_TIME)
    assert finding is not None
    classification = classify_finding(finding, DAN_551_READ_TIME, seat_runs=None)
    # ~2.1 days old -- must be flagged stale, so this cannot be
    # unreachable_participant on its own.
    assert classification["wake"]["stale"] is True
    assert classification["strand_class"] != "unreachable_participant"
    action = decide_strand_action(classification["strand_class"], [], MINOS, record, DAN_551_READ_TIME)
    assert action == "poke", "corrected logic must poke, never jump straight to CEO escalation, on this state"


def test_dan_551_replay_current_state_is_suppressed_before_posting():
    # 87 seconds later: approved, done.
    payload = {
        "status": "done",
        "executionState": {
            "status": "completed",
            "currentStageId": None,
            "lastDecisionOutcome": "approved",
        },
    }
    assert _with_mocked_api_get(payload, lambda: refresh_is_resolved("dan-551")) is True


# --- DAN-858/DAN-918 replay: healthy seat, fresh-but-genuine dropped wake ---


def test_dan_858_replay_healthy_seat_never_escalates():
    record = make_record(
        issue_id="dan-858",
        since_minutes_ago=190,  # ~3h10m, matches the reported 10.1h-strand-adjacent shape loosely
        wakes=make_wakes(
            [make_wake_event(MINOS, "deferred_issue_execution", claimed=False, requested_minutes_ago=3)]
        ),
    )
    finding = evaluate_issue(record, NOW)
    # 11 succeeded runs across 11 other issues in the 45 minutes before the
    # real detector's read, per the comment's reported evidence.
    healthy_seat_runs = [make_run(f"r{i}", status="succeeded", finished_minutes_ago=5 + i * 4) for i in range(11)]
    classification = classify(finding, seat_runs=healthy_seat_runs)
    assert classification["strand_class"] == "stranded_wake"
    action = decide_strand_action(classification["strand_class"], [], MINOS, record, NOW)
    assert action == "poke"


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
        except Exception as e:  # noqa: BLE001
            failures.append(t.__name__)
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(run_all())
