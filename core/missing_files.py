"""Finding entries whose file no longer exists on disk.

A saved session stores paths, and paths go stale - most commonly because
the file was deleted from Hydrus, which removes it from Hydrus's own
file store. The session still restores perfectly: status, tags, matched
URL and sent state are all intact, and the row looks completely normal.
Nothing reveals the file is gone until something tries to read it, which
on a large list means finding out one failure at a time.

Checking is done by listing each DIRECTORY once rather than stat'ing each
file, because the cost that matters is round trips, not syscalls. A
Hydrus store shards its files across 256 directories, so a list of tens
of thousands of files resolves in a couple of hundred requests instead of
tens of thousands - locally that's a rounding error, but over SMB, where
every request pays network latency, it's the difference between seconds
and minutes.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Set

from .applog import get_logger

log = get_logger("missing_files")


def group_by_directory(paths: Iterable[str]) -> Dict[str, List[str]]:
    """Groups paths by their containing directory, preserving order."""
    grouped: Dict[str, List[str]] = {}
    for path in paths:
        grouped.setdefault(os.path.dirname(path) or ".", []).append(path)
    return grouped


def find_missing_paths(paths: Iterable[str], stop_check=None) -> Set[str]:
    """Returns the subset of `paths` that no longer exist.

    `stop_check` is an optional callable polled between directories so a
    long scan can be abandoned when the app is closing. When it asks to
    stop, whatever has been confirmed missing so far is returned rather
    than a partial result being mistaken for a complete one - callers
    that care should check the stop condition themselves.
    """
    missing: Set[str] = set()
    grouped = group_by_directory(paths)

    for directory, group in grouped.items():
        if stop_check is not None and stop_check():
            log.info("Missing-file scan stopped early")
            break
        try:
            present = {entry.name for entry in os.scandir(directory)}
        except OSError as exc:
            # The whole directory is unreadable - gone, unmounted, or
            # permissions. Every file under it is unreachable, which is
            # what matters to the caller, so they all count as missing.
            log.debug("Could not list %s (%s) - treating its %d file(s) as missing",
                      directory, exc, len(group))
            missing.update(group)
            continue

        for path in group:
            if os.path.basename(path) not in present:
                missing.add(path)

    if missing:
        log.info(
            "%d of %d file(s) are missing from disk, across %d director%s",
            len(missing), sum(len(g) for g in grouped.values()),
            len(grouped), "y" if len(grouped) == 1 else "ies",
        )
    return missing
