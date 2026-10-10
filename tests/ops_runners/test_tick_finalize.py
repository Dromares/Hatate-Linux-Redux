#!/usr/bin/env python3
"""Plain-assert tests for ops/tick_finalize.py (DAN-289).

No pytest dependency -- run directly:
    python3 ops/test_tick_finalize.py

Covers the DAN-289 acceptance bar via the pure helpers (no live API calls):
  - find_rolling_log matches only on the exact marker, not a loose substring
  - ensure_rolling_log reuses an existing rolling log instead of creating a
    sibling (the "second run against the same thing updates, not re-files"
    shape, applied to the clean-tick rolling log rather than a finding)
  - clean_note is timestamped and distinct per call
"""
from datetime import datetime, timezone

from tick_finalize import clean_note, ensure_rolling_log, find_rolling_log, is_rolling_log

MARKER = "Seat-health watchdog -- rolling log"
COMPANY = "company-under-test"
ASSIGNEE = "dante-agent-id"
PROJECT = "project-under-test"


def test_is_rolling_log_matches_exact_marker():
    assert is_rolling_log({"title": MARKER}, MARKER)
    assert not is_rolling_log({"title": "Seat-health watchdog (narrow arm)"}, MARKER)
    assert not is_rolling_log({"title": None}, MARKER)


def test_find_rolling_log_returns_none_when_absent():
    def fake_get(path):
        return {"issues": [{"title": "Seat-health watchdog (narrow arm)", "id": "x"}]}

    assert find_rolling_log(COMPANY, MARKER, ASSIGNEE, api_get_fn=fake_get) is None


def test_find_rolling_log_finds_existing():
    target = {"title": MARKER, "id": "log-1", "identifier": "DAN-900"}

    def fake_get(path):
        return {"issues": [{"title": "unrelated", "id": "y"}, target]}

    found = find_rolling_log(COMPANY, MARKER, ASSIGNEE, api_get_fn=fake_get)
    assert found is target


def test_find_rolling_log_matches_regardless_of_status():
    # DAN-720: Paperclip's own disposition-handoff check has been observed
    # force-flipping the rolling log from in_progress to blocked within
    # seconds of a PATCH that sets it back -- a status-filtered lookup
    # intermittently misses the real log and mints a duplicate. The lookup
    # must find it no matter what status it is currently sitting at.
    target = {"title": MARKER, "id": "log-1", "identifier": "DAN-391", "status": "blocked",
              "createdAt": "2026-10-03T15:00:00Z"}
    seen_paths = []

    def fake_get(path):
        seen_paths.append(path)
        return {"issues": [target]}

    found = find_rolling_log(COMPANY, MARKER, ASSIGNEE, api_get_fn=fake_get)
    assert found is target
    assert "status=" not in seen_paths[0]


def test_find_rolling_log_prefers_oldest_on_duplicate():
    # DAN-720: if a duplicate was already minted (e.g. before this fix
    # landed) while the real log was mis-filtered out, prefer the original
    # -- never the fresh duplicate -- so history keeps accumulating in one
    # place instead of forking again on the next tick.
    original = {"title": MARKER, "id": "log-1", "identifier": "DAN-391",
                "createdAt": "2026-10-03T15:00:00Z"}
    duplicate = {"title": MARKER, "id": "log-2", "identifier": "DAN-726",
                 "createdAt": "2026-10-07T03:35:00Z"}

    def fake_get(path):
        return {"issues": [duplicate, original]}

    found = find_rolling_log(COMPANY, MARKER, ASSIGNEE, api_get_fn=fake_get)
    assert found is original


def test_find_rolling_log_scopes_query_by_marker_text():
    # DAN-789: a plain `assigneeAgentId&limit=100` listing reproduced the
    # exact DAN-720 duplicate-minting failure a second time (DAN-391 missed,
    # DAN-792 minted as a new duplicate on 2026-10-07) because the bulk
    # endpoint's default order is not createdAt/updatedAt monotonic, so a
    # dormant old issue falls outside a 100-row window even though plenty of
    # company history is older still. The real rolling log must be found via
    # a `q=<marker>` relevance search (reliable regardless of recency), not
    # by hoping it falls inside a fixed-size recency-biased page.
    original = {"title": MARKER, "id": "log-1", "identifier": "DAN-391",
                "createdAt": "2026-10-04T06:52:10.111Z"}
    seen_paths = []

    def fake_get(path):
        seen_paths.append(path)
        # Simulate the truncated bulk listing never containing the real log
        # unless the query is scoped by marker text.
        if "q=" in path:
            return {"issues": [original]}
        return {"issues": []}

    found = find_rolling_log(COMPANY, MARKER, ASSIGNEE, api_get_fn=fake_get)
    assert found is original
    assert "q=" in seen_paths[0]


def test_ensure_rolling_log_reuses_existing_without_posting():
    existing = {"title": MARKER, "id": "log-1", "identifier": "DAN-900"}
    posts = []

    def fake_get(path):
        return {"issues": [existing]}

    def fake_post(path, body):
        posts.append((path, body))
        return {"id": "should-not-happen"}

    issue, created = ensure_rolling_log(
        COMPANY, MARKER, "Seat-health watchdog (narrow arm)", PROJECT, ASSIGNEE,
        api_get_fn=fake_get, api_post_fn=fake_post,
    )
    assert issue is existing
    assert created is False
    assert posts == []  # second run against the same rolling log must not re-create it


def test_ensure_rolling_log_creates_when_absent():
    created_bodies = []

    def fake_get(path):
        return {"issues": []}

    def fake_post(path, body):
        created_bodies.append(body)
        return {"id": "new-log", "identifier": "DAN-901", **body}

    issue, created = ensure_rolling_log(
        COMPANY, MARKER, "Seat-health watchdog (narrow arm)", PROJECT, ASSIGNEE,
        api_get_fn=fake_get, api_post_fn=fake_post,
    )
    assert created is True
    assert issue["title"] == MARKER
    assert len(created_bodies) == 1
    assert created_bodies[0]["assigneeAgentId"] == ASSIGNEE
    assert created_bodies[0]["status"] == "in_progress"


def test_clean_note_is_timestamped():
    now = datetime(2026, 10, 4, 1, 0, 0, tzinfo=timezone.utc)
    note = clean_note(now)
    assert "2026-10-04T01:00:00" in note
    assert "clean" in note


def test_clean_note_detail_overrides_default_text():
    # DAN-385 requirement 6: a restated-only tick still uses the Clean path
    # (rolling log + cancelled, no new execution issue) but records *why*
    # distinctly from a truly empty tick.
    now = datetime(2026, 10, 4, 1, 0, 0, tzinfo=timezone.utc)
    note = clean_note(now, "restated-only -- no new/changed findings, see DAN-336")
    assert "2026-10-04T01:00:00" in note
    assert "restated-only" in note
    assert "clean -- no qualifying findings" not in note


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
