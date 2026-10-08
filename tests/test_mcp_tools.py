"""The MCP tool registry: tier gating, refusal text, and the parity
check against core/shortcuts.py's REVIEW_ACTIONS (DAN-707).

Entirely Qt-free and `mcp`-free, matching core/mcp_tools.py's own design
goal: every one of these runs with no display and no optional dependency
installed.
"""
import unittest

from . import _path  # noqa: F401
from core.config import McpSettings
from core.mcp_tools import (
    DRY_RUNNABLE_TIERS,
    EXCLUDED_REVIEW_ACTION_IDS,
    TIER_DESTRUCTIVE,
    TIER_HYDRUS_WRITE,
    TIER_LOCAL_WRITE,
    TIER_READ,
    TIER_RESEARCH,
    TOOLS,
    TOOLS_BY_NAME,
    gate,
    is_dry_runnable,
    mapped_review_action_ids,
    refusal_message,
    unaccounted_review_action_ids,
)
from core.shortcuts import ACTIONS_BY_ID


class TestEveryToolIsAccountedFor(unittest.TestCase):
    def test_tool_names_are_unique(self):
        names = [t.name for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_is_findable_by_name(self):
        for spec in TOOLS:
            self.assertIs(TOOLS_BY_NAME[spec.name], spec)

    def test_a_gated_tool_names_a_real_mcpsettings_field(self):
        defaults = McpSettings()
        for spec in TOOLS:
            if spec.gate_field is not None:
                self.assertTrue(
                    hasattr(defaults, spec.gate_field),
                    f"{spec.name} is gated on {spec.gate_field!r}, which McpSettings has no field for",
                )


class TestReviewActionParity(unittest.TestCase):
    """The thing most likely to rot: a new id lands in REVIEW_ACTIONS and
    nobody updates the tool mapping or the exclusion list to match."""

    def test_nothing_is_unaccounted_for(self):
        self.assertEqual(unaccounted_review_action_ids(), [])

    def test_mapped_and_excluded_ids_do_not_overlap(self):
        self.assertEqual(mapped_review_action_ids() & EXCLUDED_REVIEW_ACTION_IDS, set())

    def test_every_mapped_id_is_a_real_review_action(self):
        for action_id in mapped_review_action_ids():
            self.assertIn(action_id, ACTIONS_BY_ID)

    def test_every_excluded_id_is_a_real_review_action(self):
        """Catches the exclusion list going stale the other way - naming
        an id that stopped existing says just as much as missing one."""
        for action_id in EXCLUDED_REVIEW_ACTION_IDS:
            self.assertIn(action_id, ACTIONS_BY_ID)

    def test_select_candidate_is_deliberately_not_a_mapped_review_action(self):
        """select_candidate is new and absolute, replacing the relative
        next_candidate/prev_candidate pair - it has no REVIEW_ACTIONS id
        of its own, by design, not by omission."""
        self.assertIsNone(TOOLS_BY_NAME["select_candidate"].review_action_id)


class TestGating(unittest.TestCase):
    def test_read_tools_are_never_gated(self):
        settings = McpSettings()
        for spec in TOOLS:
            if spec.tier == TIER_READ:
                self.assertIsNone(gate(spec.name, settings))

    def test_local_write_tools_are_never_gated(self):
        settings = McpSettings()
        for spec in TOOLS:
            if spec.tier == TIER_LOCAL_WRITE:
                self.assertIsNone(gate(spec.name, settings))

    def test_research_is_refused_by_default(self):
        settings = McpSettings()
        self.assertIsNotNone(gate("research", settings))

    def test_research_is_allowed_once_turned_on(self):
        settings = McpSettings(allow_research=True)
        self.assertIsNone(gate("research", settings))

    def test_hydrus_write_tools_are_allowed_by_default(self):
        """allow_hydrus_writes defaults on - DAN-702 board decision."""
        settings = McpSettings()
        for name in ("send_upload", "send_url", "download_send"):
            self.assertIsNone(gate(name, settings), name)

    def test_hydrus_write_tools_are_refused_once_turned_off(self):
        settings = McpSettings(allow_hydrus_writes=False)
        for name in ("send_upload", "send_url", "download_send"):
            self.assertIsNotNone(gate(name, settings), name)

    def test_destructive_tools_are_refused_by_default(self):
        settings = McpSettings()
        for name in ("remove_row", "reset_result"):
            self.assertIsNotNone(gate(name, settings), name)

    def test_destructive_tools_are_allowed_once_turned_on(self):
        settings = McpSettings(allow_destructive=True)
        for name in ("remove_row", "reset_result"):
            self.assertIsNone(gate(name, settings), name)

    def test_turning_one_tier_on_does_not_turn_on_another(self):
        settings = McpSettings(allow_research=True, allow_hydrus_writes=False)
        self.assertIsNotNone(gate("send_upload", settings))
        self.assertIsNotNone(gate("remove_row", settings))

    def test_gating_reads_the_live_settings_object(self):
        """No restart needed: flipping the switch is seen on the very
        next call against the same settings object."""
        settings = McpSettings(allow_hydrus_writes=False)
        self.assertIsNotNone(gate("send_upload", settings))
        settings.allow_hydrus_writes = True
        self.assertIsNone(gate("send_upload", settings))


class TestRefusalMessage(unittest.TestCase):
    """mcp-settings-spec.md §8's tier-disabled template, matched exactly
    against the spec's own worked examples (DAN-822)."""

    def test_names_the_tool_and_the_setting(self):
        spec = TOOLS_BY_NAME["send_upload"]
        message = refusal_message(spec)
        self.assertIn('"send_upload"', message)
        self.assertIn('"Let it send files and tags to Hydrus"', message)

    def test_does_not_leak_the_raw_mcp_settings_attribute_name(self):
        """§8's template is client/audit-log-facing; `mcp.allow_hydrus_writes`
        is an implementation detail that must not appear in it."""
        for spec in TOOLS:
            if spec.gate_field is not None:
                self.assertNotIn(f"mcp.{spec.gate_field}", refusal_message(spec))

    def test_matches_the_spec_s_worked_examples_verbatim(self):
        self.assertEqual(
            refusal_message(TOOLS_BY_NAME["research"]),
            '"research" refused: Re-search is off. Turn on '
            '"Let it re-search a stuck entry" in Settings → MCP to allow it.',
        )
        self.assertEqual(
            refusal_message(TOOLS_BY_NAME["send_upload"]),
            '"send_upload" refused: Hydrus writes are off. Turn on '
            '"Let it send files and tags to Hydrus" in Settings → MCP to allow it.',
        )
        self.assertEqual(
            refusal_message(TOOLS_BY_NAME["remove_row"]),
            '"remove_row" refused: Destructive actions are off. Turn on '
            '"Let it remove rows and reset results" in Settings → MCP to allow it.',
        )

    def test_every_gated_tool_produces_a_distinct_message(self):
        messages = {
            refusal_message(spec) for spec in TOOLS if spec.gate_field is not None
        }
        gated = [spec for spec in TOOLS if spec.gate_field is not None]
        self.assertEqual(len(messages), len({s.name for s in gated}))


class TestDryRun(unittest.TestCase):
    def test_read_tools_are_not_dry_runnable(self):
        for spec in TOOLS:
            if spec.tier == TIER_READ:
                self.assertFalse(is_dry_runnable(spec.name))

    def test_local_write_tools_are_not_dry_runnable(self):
        """select_candidate/toggle_reviewed are cheap and reversible on
        their own - dry_run has nothing useful to simulate for them."""
        for spec in TOOLS:
            if spec.tier == TIER_LOCAL_WRITE:
                self.assertFalse(is_dry_runnable(spec.name))

    def test_research_hydrus_write_and_destructive_are_dry_runnable(self):
        for tier in (TIER_RESEARCH, TIER_HYDRUS_WRITE, TIER_DESTRUCTIVE):
            self.assertIn(tier, DRY_RUNNABLE_TIERS)
        for spec in TOOLS:
            if spec.tier in (TIER_RESEARCH, TIER_HYDRUS_WRITE, TIER_DESTRUCTIVE):
                self.assertTrue(is_dry_runnable(spec.name), spec.name)


if __name__ == "__main__":
    unittest.main()
