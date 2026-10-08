"""The MCP tool registry: names, tiers, gating, and refusal text.

No Qt here, and no network - this is the part of the embedded MCP server
(see core/mcp_server.py, gui/mcp_bridge.py) that is pure data and pure
logic, which is what makes it testable without a display. It mirrors
core/shortcuts.py's REVIEW_ACTIONS registry rather than inventing a
second one: most write tools map onto an existing, documented review
action id, so the Shortcuts tab and the AI cannot drift apart.

Every tool belongs to exactly one tier, and every tier but "read" and
"local_write" is gated behind a McpSettings flag that defaults to off.
A disabled tier refuses the call with a message naming the setting that
would allow it - never a stack trace, never a silent no-op.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

from .config import McpSettings
from .shortcuts import ACTIONS_BY_ID

TIER_READ = "read"
TIER_LOCAL_WRITE = "local_write"
TIER_RESEARCH = "research"
TIER_HYDRUS_WRITE = "hydrus_write"
TIER_DESTRUCTIVE = "destructive"

# Tiers that change something outside the in-memory queue, i.e. everything
# dry_run applies to. Read tools and the two reversible local writes are
# cheap and instantly undoable, so dry_run has nothing useful to simulate
# for them - see core/mcp_tools.py's own module docstring and the ticket's
# "dry_run applies to every write tier" line, which names the three tiers
# below, not all five.
DRY_RUNNABLE_TIERS: FrozenSet[str] = frozenset({
    TIER_RESEARCH, TIER_HYDRUS_WRITE, TIER_DESTRUCTIVE,
})


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: its tier, the setting that gates it (if any), and the
    core/shortcuts.py REVIEW_ACTIONS id it mirrors (if any)."""
    name: str
    tier: str
    gate_field: Optional[str]    # McpSettings attribute name, or None if never gated
    gate_label: str              # the checkbox label (spec's `setting_label`) named in the refusal message
    review_action_id: Optional[str] = None


TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec("list_queue", TIER_READ, None, ""),
    ToolSpec("get_entry", TIER_READ, None, ""),
    ToolSpec("get_candidates", TIER_READ, None, ""),
    ToolSpec("get_images", TIER_READ, None, ""),
    ToolSpec("get_diff", TIER_READ, None, ""),
    ToolSpec("list_actions", TIER_READ, None, ""),
    ToolSpec("select_candidate", TIER_LOCAL_WRITE, None, ""),
    ToolSpec("toggle_reviewed", TIER_LOCAL_WRITE, None, "", "toggle_reviewed"),
    ToolSpec("research", TIER_RESEARCH, "allow_research",
             "Let it re-search a stuck entry", "research"),
    ToolSpec("send_upload", TIER_HYDRUS_WRITE, "allow_hydrus_writes",
             "Let it send files and tags to Hydrus", "send_upload"),
    ToolSpec("send_url", TIER_HYDRUS_WRITE, "allow_hydrus_writes",
             "Let it send files and tags to Hydrus", "send_url"),
    ToolSpec("download_send", TIER_HYDRUS_WRITE, "allow_hydrus_writes",
             "Let it send files and tags to Hydrus", "download_send"),
    ToolSpec("remove_row", TIER_DESTRUCTIVE, "allow_destructive",
             "Let it remove rows and reset results", "remove_row"),
    ToolSpec("reset_result", TIER_DESTRUCTIVE, "allow_destructive",
             "Let it remove rows and reset results", "reset_result"),
)

# mcp-settings-spec.md §8's fill-in table: the tier label and its grammatical
# number (`is` for the singular "Re-search", `are` for the plural noun
# phrases) used in refusal_message() below. Deliberately not templated with
# one hardcoded verb - the spec is explicit that "is" vs "are" is intentional
# per row, not a mechanical substitution.
_TIER_REFUSAL_COPY: Dict[str, Tuple[str, str]] = {
    TIER_RESEARCH: ("Re-search", "is"),
    TIER_HYDRUS_WRITE: ("Hydrus writes", "are"),
    TIER_DESTRUCTIVE: ("Destructive actions", "are"),
}

TOOLS_BY_NAME: Dict[str, ToolSpec] = {t.name: t for t in TOOLS}

# core/shortcuts.py action ids deliberately left out of the tool surface,
# and why - see DAN-707's description for the full reasoning:
#   compare, open_match, show_in_hydrus - superseded by get_images/get_diff,
#     or pointless on a machine the user is away from (opening a browser or
#     a Hydrus window nobody is there to look at).
#   next_row/prev_row/next_unreviewed/prev_unreviewed/next_candidate/
#     prev_candidate - relative, selection-driven navigation. Every tool
#     here takes a `row` instead, so there is nothing to navigate.
EXCLUDED_REVIEW_ACTION_IDS: FrozenSet[str] = frozenset({
    "compare", "open_match", "show_in_hydrus",
    "next_row", "prev_row", "next_unreviewed", "prev_unreviewed",
    "next_candidate", "prev_candidate",
})


def mapped_review_action_ids() -> FrozenSet[str]:
    """The REVIEW_ACTIONS ids that have a tool mapped onto them."""
    return frozenset(t.review_action_id for t in TOOLS if t.review_action_id)


def unaccounted_review_action_ids() -> List[str]:
    """REVIEW_ACTIONS ids that are neither mapped to a tool nor in the
    documented exclusion list - i.e. the registry drifted and nobody
    updated this module. Empty means parity holds.

    This is deliberately a function over ACTIONS_BY_ID rather than a
    frozen constant: a new action id landing in core/shortcuts.py without
    a corresponding change here is exactly the rot this exists to catch,
    and a hardcoded list on this side could never notice it.
    """
    accounted = mapped_review_action_ids() | EXCLUDED_REVIEW_ACTION_IDS
    return sorted(set(ACTIONS_BY_ID) - accounted)


def refusal_message(spec: ToolSpec) -> str:
    """The exact text shown when a disabled tier refuses a call, built from
    mcp-settings-spec.md §8's tier-disabled template - names the setting
    that would allow it, never a stack trace, and never the raw
    `mcp.<field>` attribute name (that's an implementation detail, not
    something a client or the audit log's outcome text should see)."""
    tier_label, verb = _TIER_REFUSAL_COPY[spec.tier]
    return (
        f'"{spec.name}" refused: {tier_label} {verb} off. '
        f'Turn on "{spec.gate_label}" in Settings → MCP to allow it.'
    )


def gate(tool_name: str, settings: McpSettings) -> Optional[str]:
    """None if the call is allowed right now, else the refusal message.

    Evaluated against the LIVE settings object on every call, not cached
    at server start - so flipping a switch in Settings takes effect on
    the very next tool call, no restart needed.
    """
    spec = TOOLS_BY_NAME[tool_name]
    if spec.gate_field is None:
        return None
    if getattr(settings, spec.gate_field):
        return None
    return refusal_message(spec)


def is_dry_runnable(tool_name: str) -> bool:
    return TOOLS_BY_NAME[tool_name].tier in DRY_RUNNABLE_TIERS
