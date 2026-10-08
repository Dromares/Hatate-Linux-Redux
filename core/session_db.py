"""The automatic session, stored in SQLite so a save can be incremental.

The working list used to be one JSON document rewritten in full every two
minutes. At 29,000 entries that meant re-encoding ~50MB to record the one
match that had just come in, and the file grows with how much of the
library has been searched - a fully searched 29,000-image library projects
to well over 200MB per save.

Here each entry is a row, and a save rewrites only the rows that changed.
The row's payload is the SAME per-entry JSON the old format used, produced
by the same functions, so nothing about what a session MEANS changed -
only how much of it gets rewritten.

Which rows changed comes from ImageEntry.revision, which its __setattr__
bumps. That catches assignments to the entry but not mutations of the
objects it points at, so every save also refreshes a rotating slice of the
list (RECONCILE_FRACTION): a change that slips past the revision counter
is written within a bounded number of passes rather than never.

Named sessions ("Save Session As") stay JSON - they are occasional,
user-facing, and portable, and none of the above is a problem for them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from .applog import get_logger
from .models import ImageEntry
from .paths import SESSION_DB

log = get_logger("session_db")

SCHEMA_VERSION = 1

# Fraction of the list re-serialized on every save regardless of whether
# it looks changed, so a mutation the revision counter didn't see is
# corrected within this many passes rather than persisting forever.
RECONCILE_FRACTION = 10


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    # WAL keeps a reader (a load) from blocking on a writer (an autosave),
    # and survives a crash mid-write. FULL rather than NORMAL because this
    # file exists to be correct after exactly the crash NORMAL trades away
    # - the writes are small and off the GUI thread, so the cost is fine.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            path    TEXT PRIMARY KEY,
            ordinal INTEGER NOT NULL,
            data    TEXT NOT NULL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return conn


def save_entries(
    entries: List[ImageEntry],
    last_revisions: Optional[Dict[str, int]] = None,
    path: Union[str, Path, None] = None,
    pass_number: int = 0,
) -> Tuple[bool, Dict[str, int]]:
    """Writes the list, rewriting only what changed.

    `last_revisions` is what a previous call returned - {path: revision}
    as of that write. Passing None writes everything, which is what a
    first save or an explicit full save wants.

    Returns (succeeded, revisions_now) so the caller can feed the map back
    in next time. On failure the previous map is returned unchanged, so a
    failed save doesn't convince the next one that its rows are current.
    """
    target = Path(path) if path else SESSION_DB
    previous = last_revisions or {}
    # Serializing is the expensive half, so decide what to serialize first.
    current: Dict[str, int] = {}
    ordered: List[Tuple[str, int, ImageEntry]] = []
    for ordinal, entry in enumerate(entries):
        current[entry.path] = entry.revision
        ordered.append((entry.path, ordinal, entry))

    reconcile = _reconcile_slice(len(ordered), pass_number)
    changed = [
        (entry_path, ordinal, entry)
        for index, (entry_path, ordinal, entry) in enumerate(ordered)
        if last_revisions is None
        or previous.get(entry_path) != entry.revision
        or index % RECONCILE_FRACTION == reconcile
    ]
    conn = None
    try:
        conn = _connect(target)
        with conn:  # one transaction: either the whole save lands or none of it
            # What to delete is decided from what the FILE holds, not from
            # the caller's revision map.
            #
            # It used to be `[p for p in previous if p not in current]`,
            # and `previous` is {} whenever last_revisions is None - which
            # is exactly what a full save passes, and a full save is what
            # runs on quit. So the one write meant to be authoritative
            # could only ever add and update: every entry removed during
            # the session came back on the next start, at its old ordinal,
            # interleaved with the rows that had legitimately stayed. The
            # restored list looked like an older one.
            existing = {row[0] for row in conn.execute("SELECT path FROM entries")}
            removed = [p for p in existing if p not in current]
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('saved_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
            if removed:
                conn.executemany("DELETE FROM entries WHERE path = ?", [(p,) for p in removed])
            if changed:
                conn.executemany(
                    "INSERT INTO entries(path, ordinal, data) VALUES(?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET ordinal=excluded.ordinal, data=excluded.data",
                    [(p, o, json.dumps(_entry_payload(e))) for p, o, e in changed],
                )
            # Ordinals restore the list's order, and a sort moves nearly
            # all of them - but an ordinary autosave moves none. The
            # revision map is a dict built in list order, so its keys ARE
            # the order of the last write: comparing against that skips
            # ~29,000 pointless UPDATEs on every save that didn't reorder
            # anything, which is most of them.
            if last_revisions is None or list(previous) != [p for p, _, _ in ordered]:
                conn.executemany(
                    "UPDATE entries SET ordinal = ? WHERE path = ?",
                    [(o, p) for p, o, _ in ordered],
                )
        log.info(
            "Saved session: %d entr%s (%d rewritten, %d removed) to %s",
            len(ordered), "y" if len(ordered) == 1 else "ies",
            len(changed), len(removed), target,
        )
        return True, current
    except (sqlite3.Error, OSError, TypeError, ValueError, RuntimeError) as exc:
        # RuntimeError included deliberately: this runs off the GUI thread
        # while entries are still being filled in, and an exception
        # escaping into Qt would abort rather than lose one autosave.
        log.warning("Could not save session to %s: %s", target, exc)
        return False, previous
    finally:
        if conn is not None:
            conn.close()


def revisions_of(entries: List[ImageEntry]) -> Dict[str, int]:
    """{path: revision} for entries that are exactly what the file holds -
    the map save_entries returns, for a list that was just LOADED rather
    than just written. Handing it to the first autosave makes that save
    incremental; without it the first save after every launch rewrote all
    of session.db (17,304 rows, ~65MB, measured on a real library) to
    record nothing new. Ordered like the list, as save_entries relies on
    to skip the ordinal pass."""
    return {entry.path: entry.revision for entry in entries}


def load_entries(path: Union[str, Path, None] = None) -> Tuple[List[ImageEntry], int]:
    """Restores the list in its saved order, or [] if there is nothing.

    Returns (entries, skipped_count) where skipped_count is the number of
    malformed rows that were silently dropped. If > 0, the caller should
    alert the user (e.g. via a recovery dialog) because those entries'
    search state, tags, and sent_to_hydrus flags are lost.
    """
    target = Path(path) if path else SESSION_DB
    if not target.exists():
        return [], 0
    conn = None
    try:
        conn = _connect(target)
        rows = conn.execute("SELECT data FROM entries ORDER BY ordinal").fetchall()
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not read session %s: %s", target, exc)
        return [], 0
    finally:
        if conn is not None:
            conn.close()

    entries: List[ImageEntry] = []
    skipped = 0
    for (blob,) in rows:
        try:
            entry = _entry_from_payload(json.loads(blob))
        except Exception as exc:
            log.error("Skipping a malformed session entry (data corruption): %s", exc)
            skipped += 1
            continue
        if entry is not None:
            entries.append(entry)
    if skipped:
        log.error("Session load dropped %d corrupt row(s) from %s - those images' search state is lost",
                  skipped, target)
    else:
        log.info("Restored %d entr%s from %s",
                 len(entries), "y" if len(entries) == 1 else "ies", target)
    return entries, skipped


def entry_count(path: Union[str, Path, None] = None) -> int:
    """How many entries are stored, without building any of them."""
    target = Path(path) if path else SESSION_DB
    if not target.exists():
        return 0
    conn = None
    try:
        conn = _connect(target)
        return int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not count session entries in %s: %s", target, exc)
        return 0
    finally:
        if conn is not None:
            conn.close()


def clear(path: Union[str, Path, None] = None) -> bool:
    """Empties the stored session. Returns whether anything was there."""
    target = Path(path) if path else SESSION_DB
    if not target.exists():
        return False
    conn = None
    try:
        conn = _connect(target)
        with conn:
            had = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] > 0
            conn.execute("DELETE FROM entries")
        return bool(had)
    except (sqlite3.Error, OSError) as exc:
        log.warning("Could not clear session %s: %s", target, exc)
        return False
    finally:
        if conn is not None:
            conn.close()


def _reconcile_slice(total: int, pass_number: int) -> int:
    """Which slice of the list this pass refreshes unconditionally."""
    if total <= 0:
        return -1
    return pass_number % RECONCILE_FRACTION


# Imported lazily to keep this module's import graph free of session.py,
# which imports THIS module to decide where a session lives.
def _entry_payload(entry: ImageEntry) -> dict:
    from .session import _entry_to_dict
    return _entry_to_dict(entry)


def _entry_from_payload(payload: dict) -> Optional[ImageEntry]:
    from .session import _entry_from_dict
    return _entry_from_dict(payload)
