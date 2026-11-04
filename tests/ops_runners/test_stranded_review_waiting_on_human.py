#!/usr/bin/env python3
"""Plain-assert tests for the `waiting_on_human` strand class in
ops/stranded_review.py (DAN-2630).

Run like its sibling, with ops/ on PYTHONPATH:
    PYTHONPATH=ops python3 tests/ops_runners/test_stranded_review_waiting_on_human.py

Lives in its own file because test_stranded_review.py must stay
byte-identical to the original runner (tests/test_ops_watchdogs.py).

The bug: a stage whose `currentParticipant` is a human board user was
re-poked on every tick (DAN-2201: one comment a day since 11-01, 0 wakes --
no agent comment can clear a stage that waits on a user decision) and each
poke turned the tick into `ACTIONABLE: yes`. The 6h cap never bit because
the routine fires every 24h. The fix is one comment ever, then silence
while the issue stays listed with its age.
"""
import contextlib
import io
import os
import sys
from datetime import datetime, timedelta

import stranded_review
from stranded_review import classify_finding, decide_strand_action, evaluate_issue
from test_stranded_review import MINOS, NOW, make_record

BOARD_USER = "board-user-1"


def _user_record(issue_id="dan-2201", since_minutes_ago=60 * 24 * 4):
    record = make_record(issue_id=issue_id, since_minutes_ago=since_minutes_ago)
    record["executionState"]["currentParticipant"] = {"type": "user", "userId": BOARD_USER, "agentId": None}
    record["reviewAttention"]["paths"][0]["ref"] = BOARD_USER
    return record


def _agent_record(issue_id="dan-agent", since_minutes_ago=60 * 24 * 4):
    return make_record(issue_id=issue_id, since_minutes_ago=since_minutes_ago, participant_agent_id=MINOS)


class _FixedDatetime(datetime):
    fixed_now = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.fixed_now


def _run_tick(record, now, comments):
    """Runs stranded_review.main() --post against one record with the API
    mocked. Returns (comments_posted_to_the_issue, stdout)."""
    posted = []
    saved = {
        "datetime": stranded_review.datetime,
        "fetch_records": stranded_review.fetch_records,
        "agent_names": stranded_review.agent_names,
        "fetch_seat_runs": stranded_review.fetch_seat_runs,
        "api_get": stranded_review.api_get,
        "api_post": stranded_review.api_post,
        "refresh_is_resolved": stranded_review.refresh_is_resolved,
        "argv": sys.argv,
    }
    _FixedDatetime.fixed_now = now
    stranded_review.datetime = _FixedDatetime
    stranded_review.fetch_records = lambda company_id, limit: [record]
    stranded_review.agent_names = lambda company_id: {MINOS: "Minos"}
    stranded_review.fetch_seat_runs = lambda company_id, agent_id: []
    stranded_review.api_get = lambda path: list(comments)
    stranded_review.api_post = lambda path, body: posted.append((path, body["body"]))
    stranded_review.refresh_is_resolved = lambda issue_id: False
    sys.argv = ["stranded_review.py", "--post"]
    os.environ.setdefault("PAPERCLIP_COMPANY_ID", "company-under-test")
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = stranded_review.main()
    finally:
        stranded_review.datetime = saved["datetime"]
        stranded_review.fetch_records = saved["fetch_records"]
        stranded_review.agent_names = saved["agent_names"]
        stranded_review.fetch_seat_runs = saved["fetch_seat_runs"]
        stranded_review.api_get = saved["api_get"]
        stranded_review.api_post = saved["api_post"]
        stranded_review.refresh_is_resolved = saved["refresh_is_resolved"]
        sys.argv = saved["argv"]
    assert rc == 0
    return posted, out.getvalue()


def _as_comment(body, created_at):
    return {"body": body, "createdAt": created_at.isoformat().replace("+00:00", "Z")}


def test_user_participant_is_waiting_on_human():
    record = _user_record()
    finding = evaluate_issue(record, NOW)
    assert finding is not None
    classification = classify_finding(finding, NOW, None)
    assert classification["strand_class"] == "waiting_on_human"


def test_agent_participant_is_not_waiting_on_human():
    finding = evaluate_issue(_agent_record(), NOW)
    assert classify_finding(finding, NOW, None)["strand_class"] != "waiting_on_human"


def test_decide_strand_action_pokes_once_then_waits_for_waiting_on_human():
    record = _user_record()
    assert decide_strand_action("waiting_on_human", [], None, record, NOW) == "poke"
    prior = [_as_comment(f"x {stranded_review.MARKER}", NOW - timedelta(hours=24))]
    assert decide_strand_action("waiting_on_human", prior, None, record, NOW) == "wait"


def test_user_participant_not_repoked_on_second_tick_24h_later():
    record = _user_record()
    # Tick 1: nothing on the issue yet -> exactly one poke, tick is actionable.
    posted, out = _run_tick(record, NOW, [])
    assert len(posted) == 1, posted
    assert "ACTIONABLE: yes" in out
    first_body = posted[0][1]
    assert stranded_review.MARKER in first_body
    assert stranded_review.FINGERPRINT_MARKER in first_body

    # Tick 2, 24h later (past the 6h cap, as the daily routine is): the
    # tick-1 comment is in the history. No post, ACTIONABLE no.
    later = NOW + timedelta(hours=24)
    posted2, out2 = _run_tick(record, later, [_as_comment(first_body, NOW)])
    assert posted2 == [], posted2
    assert "ACTIONABLE: no" in out2
    # ...but the issue is still listed with its age for the needs-the-board block.
    assert "DAN-2201" in out2
    assert "waiting_on_human" in out2
    assert "pending 120.0h" in out2


def test_user_participant_with_legacy_marker_is_not_repoked():
    # DAN-2201 already carries daily pokes written before this class existed
    # (fingerprint of the old `reachable` class); the fix must not add one more.
    record = _user_record()
    legacy = _as_comment(
        f"old poke {stranded_review.MARKER}\n\n{stranded_review.FINGERPRINT_MARKER}"
        "strand=reachable,action=poke,can_advance=False -->",
        NOW - timedelta(hours=24),
    )
    posted, out = _run_tick(record, NOW, [legacy])
    assert posted == [], posted
    assert "ACTIONABLE: no" in out


def test_agent_participant_still_repoked_24h_later():
    record = _agent_record()
    posted, _ = _run_tick(record, NOW, [])
    assert len(posted) == 1
    later = NOW + timedelta(hours=24)
    posted2, out2 = _run_tick(record, later, [_as_comment(posted[0][1], NOW)])
    assert len(posted2) == 1, "agent-participant stages must still be re-poked after the window"
    assert "ACTIONABLE: yes" in out2


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
    sys.exit(run_all())
