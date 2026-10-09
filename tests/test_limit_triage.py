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

DAN-1130: the file also carries the original workspace tool's documented
behaviours (ops/RUNBOOK-limit-deaths.md) -- the DAN-106 access-band rule, the
DAN-117/118 issue-aware burst report, `--limit N`, and the 0/1/2 exit-code
contract -- each with its own test class below.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

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
classify = limit_triage.classify
detect_access_bands = limit_triage.detect_access_bands
access_band_verdict = limit_triage.access_band_verdict
detect_start_bursts = limit_triage.detect_start_bursts
burst_verdict = limit_triage.burst_verdict
same_issue_cooldown_breaches = limit_triage.same_issue_cooldown_breaches
agent_cooldowns = limit_triage.agent_cooldowns
recent_sample = limit_triage.recent_sample
build_windows = limit_triage.build_windows
compute_exit_code = limit_triage.compute_exit_code


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
    def __init__(
        self,
        agents: list[dict[str, Any]],
        runs_by_agent: dict[str, list[dict[str, Any]]],
        me: Any = None,
    ) -> None:
        self._agents = agents
        self._runs_by_agent = runs_by_agent
        self._me = me

    def get(self, path: str) -> Any:
        if path == "/api/agents/me" and self._me is not None:
            return self._me
        raise OSError(f"fake client has no {path}")

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


def _turn_run(
    run_id: str,
    ts: str,
    *,
    agent: str = "a1",
    kind: str = "access",
    model: str = "m1",
    issue: Any = None,
    retry_of: Any = None,
) -> dict[str, Any]:
    """An `acpx_turn_failed` death (`access`/`limit`), or a `succeeded`/
    `failed` plain run when `kind` is that status."""
    run: dict[str, Any] = {
        "id": run_id,
        "agentId": agent,
        "createdAt": ts,
        "startedAt": ts,
        "retryOfRunId": retry_of,
        "usageJson": {"model": model, "costUsd": 0},
        "contextSnapshot": {"issueId": issue} if issue else {},
    }
    if kind in ("access", "limit"):
        run.update(
            status="failed",
            errorCode="acpx_turn_failed",
            error=f"ACP agent reported a terminal {kind} failure.",
        )
    else:
        run["status"] = kind
    return run


class ClassifyTests(unittest.TestCase):
    def test_discriminates_on_the_error_string(self) -> None:
        self.assertEqual(classify(_turn_run("r", "2026-10-09T00:00:00Z", kind="access")), "access")
        self.assertEqual(classify(_turn_run("r", "2026-10-09T00:00:00Z", kind="limit")), "limit")
        self.assertIsNone(classify(_turn_run("r", "2026-10-09T00:00:00Z", kind="succeeded")))

    def test_session_init_failure_has_its_own_class_not_other(self) -> None:
        self.assertEqual(classify(_run("r", "2026-10-09T00:00:00Z")), "init")
        self.assertEqual(
            classify({"status": "failed", "errorCode": "process_lost"}), "other"
        )


class AccessBandTests(unittest.TestCase):
    """DAN-106 band rule (RUNBOOK "Is this access death actually a credential problem?")."""

    def test_zero_in_band_successes_across_agents_is_account_wide(self) -> None:
        runs = [
            _turn_run("d1", "2026-09-30T04:26:42Z", agent="cloud", model="opus"),
            _turn_run("d2", "2026-09-30T04:27:30Z", agent="virgil", model="sonnet"),
            _turn_run("d3", "2026-09-30T04:28:05Z", agent="cloud", model="opus"),
        ]
        (band,) = detect_access_bands(runs, 900)
        self.assertEqual(band["deaths"], 3)
        self.assertEqual(band["agents"], {"cloud", "virgil"})
        self.assertEqual(access_band_verdict(band), "account_wide")

    def test_sole_agent_awake_is_under_determined(self) -> None:
        runs = [_turn_run(f"d{i}", f"2026-09-29T19:59:{i:02d}Z", agent="cloud") for i in (2, 5, 11)]
        (band,) = detect_access_bands(runs, 900)
        self.assertEqual(access_band_verdict(band), "under_determined_sole_agent")

    def test_other_agent_active_without_death_or_success_is_weak_evidence(self) -> None:
        runs = [
            _turn_run("d1", "2026-10-09T05:00:00Z", agent="a1"),
            _turn_run("d2", "2026-10-09T05:00:20Z", agent="a1"),
            _turn_run("f1", "2026-10-09T05:00:10Z", agent="a2", kind="failed"),
        ]
        (band,) = detect_access_bands(runs, 900)
        self.assertEqual(access_band_verdict(band), "under_determined_other_agents_unaffected")

    def test_in_band_success_is_the_per_agent_signature(self) -> None:
        runs = [
            _turn_run("d1", "2026-10-09T05:00:00Z", agent="a1"),
            _turn_run("ok", "2026-10-09T05:00:10Z", agent="a2", kind="succeeded"),
            _turn_run("d2", "2026-10-09T05:00:20Z", agent="a1"),
        ]
        (band,) = detect_access_bands(runs, 900)
        self.assertEqual(band["in_band_success"], 1)
        self.assertEqual(access_band_verdict(band), "per_agent_new")

    def test_gap_over_threshold_splits_bands_and_gap_at_threshold_does_not(self) -> None:
        runs = [
            _turn_run("d1", "2026-10-09T05:00:00Z"),
            _turn_run("d2", "2026-10-09T05:15:00Z"),  # exactly 900s later: same band
            _turn_run("d3", "2026-10-09T05:30:01Z"),  # 901s later: new band
        ]
        self.assertEqual([b["deaths"] for b in detect_access_bands(runs, 900)], [2, 1])
        self.assertEqual(len(detect_access_bands(runs, 2000)), 1)

    def test_time_to_recovery_is_gap_to_first_later_success(self) -> None:
        runs = [
            _turn_run("d1", "2026-10-09T05:00:00Z"),
            _turn_run("ok", "2026-10-09T05:00:26Z", agent="a2", kind="succeeded"),
        ]
        (band,) = detect_access_bands(runs, 900)
        self.assertEqual(band["time_to_recovery"], 26.0)


def _burst_runs(spec: list[tuple[str, str, Any, Any]]) -> list[dict[str, Any]]:
    """(run_id, ts, issue, retry_of) tuples, all for agent a1."""
    return [
        _turn_run(rid, f"2026-10-09T05:00:{sec}Z", kind="succeeded", issue=issue, retry_of=retry)
        for rid, sec, issue, retry in spec
    ]


class StartBurstTests(unittest.TestCase):
    """DAN-105 burst detection, DAN-117 issue-aware verdict, DAN-118 retry split."""

    def _one_burst(self, spec: list[tuple[str, str, Any, Any]]) -> dict[str, Any]:
        bursts = detect_start_bursts(_burst_runs(spec), 6, 60)
        self.assertEqual(len(bursts), 1)
        return bursts[0]

    def test_below_threshold_is_not_a_burst(self) -> None:
        runs = _burst_runs([(f"r{i}", f"{i:02d}", f"i{i}", None) for i in range(5)])
        self.assertEqual(detect_start_bursts(runs, 6, 60), [])

    def test_one_start_per_distinct_issue_is_missing_ceiling_not_a_breach(self) -> None:
        burst = self._one_burst([(f"r{i}", f"{i:02d}", f"i{i}", None) for i in range(6)])
        verdict, distinct, max_one, fresh, retry = burst_verdict(burst, 30)
        self.assertEqual((verdict, distinct, max_one, fresh, retry), ("no_breach_one_per_issue", 6, 1, [], []))

    def test_timer_runs_without_an_issue_are_each_their_own_issue(self) -> None:
        burst = self._one_burst([(f"r{i}", f"{i:02d}", None, None) for i in range(6)])
        self.assertEqual(burst_verdict(burst, 30)[:2], ("no_breach_one_per_issue", 6))

    def test_fresh_same_issue_repeat_inside_cooldown_is_a_real_breach(self) -> None:
        spec = [(f"r{i}", f"{i:02d}", f"i{i}", None) for i in range(5)] + [("dup", "01", "i0", None)]
        verdict, _, max_one, fresh, retry = burst_verdict(self._one_burst(spec), 30)
        self.assertEqual((verdict, max_one, len(fresh), retry), ("real_breach", 2, 1, []))

    def test_retry_caused_repeat_inside_cooldown_is_no_agent_ceiling(self) -> None:
        spec = [(f"r{i}", f"{i:02d}", f"i{i}", None) for i in range(5)] + [("retry", "01", "i0", "r0")]
        verdict, _, _, fresh, retry = burst_verdict(self._one_burst(spec), 30)
        self.assertEqual((verdict, fresh, len(retry)), ("no_agent_ceiling", [], 1))

    def test_repeat_at_or_after_cooldown_respects_the_setting(self) -> None:
        spec = [(f"r{i}", f"{i:02d}", f"i{i}", None) for i in range(5)] + [("late", "40", "i0", None)]
        self.assertEqual(burst_verdict(self._one_burst(spec), 30)[0], "no_breach_repeat_within_cooldown")

    def test_same_issue_breaches_split_retry_from_fresh_and_use_per_agent_cooldown(self) -> None:
        runs = [
            _turn_run("a", "2026-10-09T05:00:00Z", kind="succeeded", issue="i1"),
            _turn_run("b", "2026-10-09T05:00:10Z", kind="succeeded", issue="i1", retry_of="a"),
            _turn_run("c", "2026-10-09T05:00:20Z", kind="succeeded", issue="i1"),
            _turn_run("d", "2026-10-09T05:01:00Z", kind="succeeded", issue="i1"),
            _turn_run("t1", "2026-10-09T05:00:00Z", kind="succeeded"),  # timers: never "same issue"
            _turn_run("t2", "2026-10-09T05:00:01Z", kind="succeeded"),
        ]
        breaches = same_issue_cooldown_breaches(runs, {"a1": (30, "test")})["a1"]
        self.assertEqual([(b["r1"]["id"], b["is_retry"]) for b in breaches], [("b", True), ("c", False)])
        # a tighter cooldown for the same data clears the fresh one
        tight = same_issue_cooldown_breaches(runs, {"a1": (5, "test")})
        self.assertEqual(tight, {})


class AgentCooldownTests(unittest.TestCase):
    def test_own_record_is_live_and_everyone_else_falls_back(self) -> None:
        client = FakePaperclipClient(
            agents=[], runs_by_agent={}, me={"id": "me", "runtimeConfig": {"heartbeat": {"cooldownSec": 45}}}
        )
        names = {"me": "Someone", "v": "Virgil", "x": "Unknown"}
        got = agent_cooldowns(client, {"me", "v", "x"}, names)
        self.assertEqual(got["me"], (45, "live (own record)"))
        self.assertEqual(got["v"], (30, "fallback, board-confirmed"))
        self.assertEqual(got["x"], (60, "fallback, unconfirmed default"))

    def test_unreachable_own_record_is_not_fatal(self) -> None:
        client = FakePaperclipClient(agents=[], runs_by_agent={})
        self.assertEqual(agent_cooldowns(client, {"c"}, {"c": "Cloud"})["c"][0], 60)


class RecentSampleTests(unittest.TestCase):
    def test_limit_keeps_the_newest_n_runs(self) -> None:
        runs = [_run(f"r{i}", f"2026-10-09T0{i}:00:00Z") for i in range(5)]
        self.assertEqual([r["id"] for r in recent_sample(runs, 2)], ["r4", "r3"])
        self.assertEqual(len(recent_sample(runs, 200)), 5)


def _main(
    runs_by_agent: dict[str, list[dict[str, Any]]], *argv: str
) -> tuple[int, str]:
    """Run main() against a fake client; returns (exit code, stdout)."""
    agents = [{"id": a, "name": a} for a in runs_by_agent]
    client = FakePaperclipClient(agents, runs_by_agent)
    env = {"PAPERCLIP_API_URL": "http://x/api", "PAPERCLIP_API_KEY": "k", "PAPERCLIP_COMPANY_ID": "c"}
    out = io.StringIO()
    with (
        mock.patch.dict(os.environ, env),
        mock.patch.object(limit_triage, "PaperclipClient", lambda *a, **k: client),
        contextlib.redirect_stdout(out),
    ):
        code = limit_triage.main(list(argv))
    return code, out.getvalue()


class ExitCodeContractTests(unittest.TestCase):
    """RUNBOOK "Exit codes": 0 clean, 1 self-resolving, 2 per-agent signature."""

    def test_clean_sample_exits_0(self) -> None:
        code, _ = _main({"a1": [_turn_run("ok", "2026-10-09T05:00:00Z", kind="succeeded")]})
        self.assertEqual(code, 0)

    def test_limit_death_in_newest_window_exits_1(self) -> None:
        code, out = _main({"a1": [_turn_run("q", "2026-10-09T05:00:00Z", kind="limit")]})
        self.assertEqual(code, 1)
        self.assertIn("QUOTA EXHAUSTED", out)

    def test_account_wide_access_band_exits_1(self) -> None:
        runs = {
            "a1": [_turn_run("d1", "2026-10-09T05:00:00Z", agent="a1", model="m1")],
            "a2": [_turn_run("d2", "2026-10-09T05:00:20Z", agent="a2", model="m2")],
        }
        code, out = _main(runs)
        self.assertEqual(code, 1)
        self.assertIn("ACCOUNT-WIDE GATE CLOSURE", out)

    def test_access_band_with_in_band_success_exits_2(self) -> None:
        runs = {
            "a1": [
                _turn_run("d1", "2026-10-09T05:00:00Z", agent="a1"),
                _turn_run("d2", "2026-10-09T05:00:20Z", agent="a1"),
            ],
            "a2": [_turn_run("ok", "2026-10-09T05:00:10Z", agent="a2", kind="succeeded")],
        }
        code, out = _main(runs)
        self.assertEqual(code, 2)
        self.assertIn("GENUINELY PER-AGENT", out)

    def test_exit_code_is_the_newest_band_not_an_older_one(self) -> None:
        old_per_agent = [
            _turn_run("d1", "2026-10-08T05:00:00Z", agent="a1"),
            _turn_run("ok", "2026-10-08T05:00:10Z", agent="a2", kind="succeeded"),
        ]
        new_account_wide = [
            _turn_run("d2", "2026-10-09T05:00:00Z", agent="a1"),
            _turn_run("d3", "2026-10-09T05:00:10Z", agent="a2", model="m2"),
        ]
        code, _ = _main({"a1": old_per_agent[:1] + new_account_wide[:1], "a2": old_per_agent[1:] + new_account_wide[1:]})
        self.assertEqual(code, 1)

    def test_burst_and_cooldown_sections_do_not_change_the_exit_code(self) -> None:
        runs = [
            _turn_run(f"r{i}", f"2026-10-09T05:00:{i:02d}Z", kind="succeeded", issue="same")
            for i in range(8)
        ]
        code, out = _main({"a1": runs})
        self.assertEqual(code, 0)
        self.assertIn("REAL PER-ISSUE COOLDOWN BREACH", out)

    def test_session_init_failures_do_not_change_the_exit_code(self) -> None:
        code, out = _main({"a1": [_run("i1", "2026-10-09T05:00:00Z")]})
        self.assertEqual(code, 0)
        self.assertIn("acpx_session_init_failed", out)

    def test_json_mode_reports_the_same_exit_code(self) -> None:
        runs = {
            "a1": [
                _turn_run("d1", "2026-10-09T05:00:00Z", agent="a1"),
                _turn_run("d2", "2026-10-09T05:00:20Z", agent="a1"),
            ],
            "a2": [_turn_run("ok", "2026-10-09T05:00:10Z", agent="a2", kind="succeeded")],
        }
        code, out = _main(runs, "--json")
        payload = json.loads(out)
        self.assertEqual((code, payload["exitCode"]), (2, 2))
        self.assertEqual(payload["accessBands"][0]["verdict"], "per_agent_new")
        self.assertIn("acpx_turn_failed", payload["summary"])


class LimitFlagTests(unittest.TestCase):
    """RUNBOOK "Running the triage": `--limit N` samples the newest N runs."""

    def _runs(self) -> dict[str, list[dict[str, Any]]]:
        # an old per-agent band (a2 succeeds inside it), then 3 newer successes
        return {
            "a1": [
                _turn_run("d1", "2026-10-08T05:00:00Z", agent="a1"),
                _turn_run("d1b", "2026-10-08T05:00:20Z", agent="a1"),
            ]
            + [_turn_run(f"ok{i}", f"2026-10-09T05:00:0{i}Z", agent="a1", kind="succeeded") for i in range(3)],
            "a2": [_turn_run("ok", "2026-10-08T05:00:10Z", agent="a2", kind="succeeded")],
        }

    def test_limit_excludes_older_runs_from_the_sample_report(self) -> None:
        code, out = _main(self._runs(), "--limit", "3")
        self.assertEqual(code, 0)
        self.assertIn("last 3 runs", out)
        self.assertIn("no access deaths in this sample", out)

    def test_default_limit_is_200_and_includes_them(self) -> None:
        code, out = _main(self._runs())
        self.assertEqual(code, 2)
        self.assertIn("last 6 runs", out)

    def test_incident_report_ignores_limit(self) -> None:
        _, out = _main(self._runs(), "--limit", "1", "--json")
        self.assertEqual(json.loads(out)["summary"]["acpx_turn_failed"]["incidents"], 2)

    def test_band_gap_flag_is_honoured(self) -> None:
        runs = {"a1": [_turn_run("d1", "2026-10-09T05:00:00Z"), _turn_run("d2", "2026-10-09T05:10:00Z")]}
        _, wide = _main(runs, "--json")
        _, narrow = _main(runs, "--json", "--band-gap-sec", "300")
        self.assertEqual(len(json.loads(wide)["accessBands"]), 1)
        self.assertEqual(len(json.loads(narrow)["accessBands"]), 2)


if __name__ == "__main__":
    unittest.main()
