"""Deciding which files actually enter the working list.

Both of these lived on MainWindow, and neither has anything to do with a
window: one walks a folder, the other applies the de-duplication rule.
The second in particular described itself as "pure in-memory logic, no
I/O" while sitting in a 3,500-line GUI class with no test of its own -
and getting it wrong means either the same image in the list twice, or a
file silently refused.
"""
from __future__ import annotations

import os
from typing import Dict, Iterable, List, Tuple

from .applog import get_logger
from .formats import IMAGE_EXTENSIONS

log = get_logger("file_intake")


def scan_folder(folder: str) -> List[str]:
    """Every image under `folder`, recursively.

    Extension-based, and deliberately so: the alternative is opening tens
    of thousands of files to sniff their contents before the user has
    asked for anything to be done with them.
    """
    found = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                found.append(os.path.join(root, name))
    return found


def filter_duplicate_paths(
    paths: Iterable[str],
    path_to_hash: Dict[str, str],
    existing_entries: Iterable,
) -> Tuple[List[str], int]:
    """Drops paths already represented in the list.

    Two rounds, cheapest first. An exact path match is free. A content
    hash match is what catches the same image arriving under a different
    name - a copy, a re-download, the same file in two folders - which is
    the case that actually happens when someone adds a directory tree
    twice.

    Duplicates WITHIN the batch being added count too: the accumulating
    sets are updated as it goes, so a folder containing a file and its own
    copy adds one of them, not both.

    No I/O. Every hash needed has already been computed by the background
    FileHashWorker, and a path with no hash simply falls through to the
    path check alone rather than being read here.

    Returns (unique_paths, duplicate_count).
    """
    existing_abs_paths = {os.path.abspath(e.path) for e in existing_entries}
    existing_hashes = {e.hydrus_hash for e in existing_entries if e.hydrus_hash}

    unique_paths: List[str] = []
    duplicate_count = 0

    for p in paths:
        abs_p = os.path.abspath(p)
        if abs_p in existing_abs_paths:
            duplicate_count += 1
            log.debug("Skipping %s - already in the list (same path)", p)
            continue

        file_hash = path_to_hash.get(p)
        if file_hash and file_hash in existing_hashes:
            duplicate_count += 1
            log.debug("Skipping %s - duplicate content of an already-added image", p)
            continue

        if file_hash:
            existing_hashes.add(file_hash)
        existing_abs_paths.add(abs_p)
        unique_paths.append(p)

    return unique_paths, duplicate_count
