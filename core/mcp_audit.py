"""The MCP server's audit log: one line per tool call, kept forever (up
to a size cap), redacted, and append-only.

The point is the morning-after question: "what did it do, and why." A
refusal is part of that answer as much as an action is - a tool the AI
tried and could not use says something about what it was attempting -
so this is called for every call, allowed, refused, or dry-run, never
only for the ones that changed something.

JSON Lines rather than a single JSON document: a crash mid-write loses at
most the one line being appended, never the file, and nothing has to be
parsed and rewritten whole just to add an entry.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .applog import get_logger
from .paths import CONFIG_DIR

log = get_logger("mcp_audit")

AUDIT_LOG_FILE = CONFIG_DIR / "mcp_audit.jsonl"

# Keys redacted wherever they appear in a call's arguments or outcome,
# however deeply nested - matched case-insensitively since a tool's
# keyword arguments and a serialized settings dict don't necessarily
# agree on case. Bearer token and Hydrus access key are the two secrets
# this server ever touches; everything else here is queue state the
# audit log exists to show, not hide.
REDACTED_KEYS = frozenset({
    "token", "bearer", "access_key", "accesskey", "api_key", "apikey",
    "authorization",
})
REDACTED_VALUE = "<redacted>"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (REDACTED_VALUE if key.lower() in REDACTED_KEYS else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def record(
    *,
    tool: str,
    tier: str,
    allowed: bool,
    arguments: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    dry_run: bool = False,
    row: Optional[int] = None,
    candidate_index: Optional[int] = None,
    outcome: Any = None,
    path: Path = AUDIT_LOG_FILE,
    max_bytes: int = 5_000_000,
) -> None:
    """Appends one audit entry. Never raises - a logging failure must not
    take down the tool call it is trying to describe; it is reported
    instead, same as every other best-effort write in this app."""
    entry = {
        "timestamp": _iso_now(),
        "tool": tool,
        "tier": tier,
        "allowed": allowed,
        "reason": reason,
        "dry_run": dry_run,
        "row": row,
        "candidate_index": candidate_index,
        "arguments": _redact(arguments or {}),
        "outcome": _redact(outcome),
    }
    _append(entry, path, max_bytes)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append(entry: Dict[str, Any], path: Path, max_bytes: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path, max_bytes)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        log.warning("Could not write MCP audit log entry to %s: %s", path, exc)


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    """Keeps exactly one rotated generation, the same shape as the
    config.json/.bak pattern elsewhere in this app: simple, and enough to
    stop an unattended overnight run from growing the log without bound."""
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            rotated = path.with_name(path.name + ".1")
            os.replace(path, rotated)
    except OSError as exc:
        log.warning("Could not rotate MCP audit log %s: %s", path, exc)
