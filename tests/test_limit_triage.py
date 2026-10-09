"""DAN-926: limit-class run-failure triage tests.

`ops/limit-triage.py` keeps the hyphenated filename the objective and
`ops/seat_watchdog.py`'s own docstring/tests already cite verbatim, so it
cannot be imported as a normal dotted module (`ops.limit-triage` is not a
valid identifier path) -- loaded here via `importlib.util` instead, the
standard way to unit-test a script-shaped file with a non-identifier name.

Fixtures reconstruct the two real retry chains measured against the live
board on 2026-10-09 (see the module docstring): Beatrice's 2026-10-08
3-run `spawn E2BIG` chain (the known DAN-914/DAN-920 incident), and Cloud's
2026-09-29 7-row/3-chain cluster against DAN-1 ("Paperclip onboarding") --
the event DAN-920 mis-cited as "7 occurrences, 2026-09-30".
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

_MODULE_PATH = Path(__file__).resolve().parent.parent / "ops" / "limit-triage.py"
_spec = importlib.util.spec_from_file_location("limit_triage", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
limit_triage = importlib.util.module_from_spec(_spec)
sys.modules["limit_triage"] = limit_triage
_spec.loader.exec_module(limit_triage)

bucket_for = limit_triage.bucket_for
build_incidents = limit_triage.build_incidents
summarize_by_bucket = limit_triage.summarize_by_bucket
PaperclipClient = limit_triage.PaperclipClient
fetch_all_runs = limit_triage.fetch_all_runs


def _run(
    run_id: str,
    created_at: str,
    *,
    status: str = "failed",
    error_code: str = "acpx_session_init_failed",
    retry_of: Any = None,
    agent_id: str = "agent-1",
    issue_id: str = "issue-1",
) -> dict[str, Any]:
    return {
        "id": run_id,
        "createdAt": created_at,
        "status": status,
        "errorCode": error_code,
        "retryOfRunId": retry_of,
        "agentId": agent_id,
        "contextSnapshot": {"issueId": issue_id},
    }


class BucketForTests(unittest.TestCase):
    def test_named_error_code_is_its_own_bucket(self) -> None:
        self.assertEqual(bucket_for({"errorCode": "acpx_session_init_failed"}), "acpx_session_init_failed")
        self.assertEqual(bucket_for({"errorCode": "provider_quota"}), "provider_quota")

    def test_missing_error_code_is_other(self) -> None:
        self.assertEqual(bucket_for({"errorCode": None}), "other")
        self.assertEqual(bucket_for({}), "other")


class BuildIncidentsTests(unittest.TestCase):
    def test_beatrice_2026_10_08_chain_collapses_to_one_incident(self) -> None:
        runs = [
            _run("78587b49", "2026-10-08T01:43:08Z", retry_of=None, agent_id="beatrice"),
            _run("29d46b65", "2026-10-08T02:07:09Z", retry_of="78587b49", agent_id="beatrice"),
            _run("d19b4c0d", "2026-10-08T02:07:11Z", retry_of="29d46b65", agent_id="beatrice"),
        ]
        incidents = build_incidents(runs)
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc.row_count, 3)
        self.assertEqual(inc.bucket, "acpx_session_init_failed")
        self.assertEqual(inc.run_ids, ["78587b49", "29d46b65", "d19b4c0d"])
        self.assertEqual(inc.first_at, "2026-10-08T01:43:08Z")
        self.assertEqual(inc.last_at, "2026-10-08T02:07:11Z")

    def test_cloud_2026_09_29_seven_rows_collapse_to_three_incidents(self) -> None:
        # Reconstructs the real chain shape: two 3-run chains + one
        # standalone root, all against the same issue (DAN-1).
        runs = [
            _run("bde6a8ba", "2026-09-29T02:49:52Z", retry_of=None, agent_id="cloud", issue_id="dan-1"),
            _run("8a945ad7", "2026-09-29T02:49:53Z", retry_of="bde6a8ba", agent_id="cloud", issue_id="dan-1"),
            _run("5f822f50", "2026-09-29T02:50:27Z", retry_of="8a945ad7", agent_id="cloud", issue_id="dan-1"),
            _run("a7861c59", "2026-09-29T02:47:30Z", retry_of=None, agent_id="cloud", issue_id="dan-1"),
            _run("78b895dc", "2026-09-29T02:47:31Z", retry_of="a7861c59", agent_id="cloud", issue_id="dan-1"),
            _run("7a2f9834", "2026-09-29T02:48:27Z", retry_of="78b895dc", agent_id="cloud", issue_id="dan-1"),
            _run("2213fe3b", "2026-09-29T02:16:52Z", retry_of=None, agent_id="cloud", issue_id="dan-1"),
        ]
        incidents = build_incidents(runs)
        self.assertEqual(len(incidents), 3)
        self.assertEqual(sum(inc.row_count for inc in incidents), 7)
        self.assertEqual(sorted(inc.row_count for inc in incidents), [1, 3, 3])
        for inc in incidents:
            self.assertEqual(inc.issue_ids, ["dan-1"])

    def test_succeeded_and_cancelled_runs_excluded_from_chains(self) -> None:
        runs = [
            _run("r1", "2026-10-08T00:00:00Z", status="failed", retry_of=None),
            _run("r2", "2026-10-08T00:01:00Z", status="succeeded", retry_of="r1"),
            _run("r3", "2026-10-08T00:02:00Z", status="cancelled", retry_of=None),
        ]
        incidents = build_incidents(runs)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].run_ids, ["r1"])

    def test_retry_pointing_outside_fetched_window_becomes_its_own_root(self) -> None:
        # retryOfRunId references a run this fetch never saw (e.g. it fell
        # outside a seat's own 1000-row window) -- must not crash, and must
        # not be silently merged into an unrelated chain.
        runs = [_run("orphan-retry", "2026-10-01T00:00:00Z", retry_of="not-in-this-fetch")]
        incidents = build_incidents(runs)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].run_ids, ["orphan-retry"])

    def test_bucket_follows_terminal_run_in_chain(self) -> None:
        runs = [
            _run("r1", "2026-10-08T00:00:00Z", error_code="acpx_session_init_failed", retry_of=None),
            _run("r2", "2026-10-08T00:01:00Z", error_code="acpx_session_init_failed", retry_of="r1"),
        ]
        incidents = build_incidents(runs)
        self.assertEqual(incidents[0].bucket, "acpx_session_init_failed")


class SummarizeByBucketTests(unittest.TestCase):
    def test_counts_incidents_and_raw_rows_separately(self) -> None:
        runs = [
            _run("78587b49", "2026-10-08T01:43:08Z", retry_of=None),
            _run("29d46b65", "2026-10-08T02:07:09Z", retry_of="78587b49"),
            _run("d19b4c0d", "2026-10-08T02:07:11Z", retry_of="29d46b65"),
            _run("q1", "2026-10-09T00:00:00Z", error_code="provider_quota", retry_of=None),
        ]
        incidents = build_incidents(runs)
        summary = summarize_by_bucket(incidents)
        self.assertEqual(summary["acpx_session_init_failed"], {"incidents": 1, "rawRowCount": 3})
        self.assertEqual(summary["provider_quota"], {"incidents": 1, "rawRowCount": 1})


class FakePaperclipClient(PaperclipClient):
    def __init__(self, agents: list[dict[str, Any]], runs_by_agent: dict[str, list[dict[str, Any]]]) -> None:
        self._agents = agents
        self._runs_by_agent = runs_by_agent

    def list_agents(self, company_id: str) -> list[dict[str, Any]]:
        return self._agents

    def list_runs_for_agent(self, company_id: str, agent_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        return self._runs_by_agent.get(agent_id, [])


class FetchAllRunsTests(unittest.TestCase):
    def test_combines_every_agents_runs(self) -> None:
        client = FakePaperclipClient(
            agents=[{"id": "a1"}, {"id": "a2"}],
            runs_by_agent={
                "a1": [_run("r1", "2026-10-08T00:00:00Z", agent_id="a1")],
                "a2": [_run("r2", "2026-10-08T00:00:00Z", agent_id="a2")],
            },
        )
        runs = fetch_all_runs(client, "company-1")
        self.assertEqual({r["id"] for r in runs}, {"r1", "r2"})


if __name__ == "__main__":
    unittest.main()
