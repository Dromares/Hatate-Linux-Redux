"""Saves and restores the working image list across restarts.

Rebuilding the list from scratch is expensive in a way that scales badly:
hashing a large batch takes minutes over a network share, and every
restart previously paid that cost again for files the app had already
seen. Persisting the list means a restart costs a fraction of a second.

The stored hash matters most - it's what lets the app skip re-hashing
entirely. Thumbnails are deliberately NOT stored: they're binary, they'd
dominate the file size, and they already regenerate in the background
without blocking the UI.

Serialization for candidates and tags is reused from search_cache rather
than reimplemented, so the two can't drift apart in how they represent
the same objects.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Union

from .applog import get_logger
from .models import ImageEntry, MatchStatus, TagSource
from .paths import SESSION_DB, SESSION_FILE
# Same package, and deliberately shared: duplicating these would let the
# session format and the search-cache format diverge silently.
from .search_cache import (
    _candidate_from_dict, _candidate_to_dict, _tag_from_dict, _tag_to_dict,
)

log = get_logger("session")

SESSION_FORMAT_VERSION = 1

# How long a migrated session.json is kept as a backup before it is
# removed. It stops changing the moment it is migrated, so its own mtime
# says how long it has been a backup.
LEGACY_BACKUP_DAYS = 30


def _snapshot(items) -> list:
    """A stable copy of a list that another thread may be mutating.

    Serialization runs off the GUI thread while search and tag workers are
    still filling these lists in. list() of a list is a single atomic
    operation under the GIL, so this cannot tear or raise, whereas
    iterating the live list can raise "list changed size during
    iteration" - which used to escape save_session's except clause,
    propagate out of a Qt slot, and abort the process.

    The copy is shallow and costs microseconds; the Tag and MatchCandidate
    objects inside it are only read.
    """
    try:
        return list(items or ())
    except (TypeError, RuntimeError):
        return []


def _entry_to_dict(entry: ImageEntry) -> dict:
    return {
        "path": entry.path,
        "hydrus_hash": entry.hydrus_hash,        # the expensive bit - avoids re-hashing
        "status": entry.status.value,
        "error_message": entry.error_message,
        "result_source": entry.result_source,
        # Kept so a restart still knows which results are provisional -
        # otherwise they would look finished and never be re-run.
        "searched_without_saucenao": entry.searched_without_saucenao,
        "sent_to_hydrus": entry.sent_to_hydrus,
        "hydrus_import_confirmed": entry.hydrus_import_confirmed,
        # The whole point of the reviewed flag is surviving a restart: a
        # review pass over a large library spans days, and a decision
        # forgotten on exit leaves the row indistinguishable from one
        # never opened - which is the state it was invented to fix.
        "reviewed": entry.reviewed,
        # The upscale check is opt-in and can take a while over a large
        # batch, so its verdict is saved like any other result rather
        # than making the user re-run it every session.
        "upscale_verdict": entry.upscale_verdict,
        "upscale_check_detail": entry.upscale_check_detail,
        "local_width": entry.local_width,
        "local_height": entry.local_height,
        "selected_candidate_index": entry.selected_candidate_index,
        "candidates": [_candidate_to_dict(c) for c in _snapshot(entry.candidates)],
        # Tags carry their source, which drives the tag-source filter and
        # decides what gets sent to Hydrus - so it has to survive a restart.
        "tags": [dict(_tag_to_dict(t), source=t.source.value)
                 for t in _snapshot(entry.tags)],
    }


def _entry_from_dict(d: dict) -> Optional[ImageEntry]:
    path = d.get("path")
    if not path:
        return None
    entry = ImageEntry(path=path)
    entry.hydrus_hash = d.get("hydrus_hash")
    entry.error_message = d.get("error_message")
    entry.result_source = d.get("result_source")
    entry.searched_without_saucenao = bool(d.get("searched_without_saucenao"))
    entry.sent_to_hydrus = bool(d.get("sent_to_hydrus"))
    entry.hydrus_import_confirmed = bool(d.get("hydrus_import_confirmed"))
    # Absent in sessions written before this existed, which read as
    # unreviewed - correct, since nothing had marked them.
    entry.reviewed = bool(d.get("reviewed"))
    # Absent in sessions written before this existed, which reads as
    # never-checked - correct, since no check had run yet.
    entry.upscale_verdict = d.get("upscale_verdict")
    entry.upscale_check_detail = d.get("upscale_check_detail")
    entry.local_width = d.get("local_width")
    entry.local_height = d.get("local_height")

    try:
        entry.status = MatchStatus(d.get("status") or "not_searched")
    except ValueError:
        entry.status = MatchStatus.NOT_SEARCHED

    # Order matters: select_candidate() intentionally REPLACES the
    # SEARCH_ENGINE and BOORU tags with the chosen candidate's own, so it
    # has to run BEFORE the saved tags are restored. Doing it the other
    # way round silently discarded every booru-sourced tag on load.
    entry.candidates = [_candidate_from_dict(c) for c in d.get("candidates") or []]
    if entry.candidates:
        index = d.get("selected_candidate_index", 0)
        if not (0 <= index < len(entry.candidates)):
            index = 0
        # Repopulates matched_url / booru_name / similarity from the candidate.
        entry.select_candidate(index)

    # The saved tags are the authoritative record of what the user was
    # actually looking at - including any they added or edited by hand -
    # so they overwrite whatever select_candidate just derived.
    restored_tags = []
    for raw in d.get("tags") or []:
        try:
            source = TagSource(raw.get("source", TagSource.USER.value))
        except ValueError:
            source = TagSource.USER
        restored_tags.append(_tag_from_dict(raw, source))
    if restored_tags:
        entry.tags = restored_tags

    return entry


def _migrate_legacy_session() -> List[ImageEntry]:
    """Reads the old single-document session once and rewrites it as rows.

    The JSON file is deliberately left in place rather than deleted: it is
    a complete, readable backup of the moment before the change. Nothing
    reads it again once session.db exists, and _retire_legacy_session
    removes it once that backup has had LEGACY_BACKUP_DAYS to be wanted -
    "nothing but disk" turned out to be 52MB on a real library.
    """
    entries = _load_json_session(SESSION_FILE)
    if not entries:
        return []
    from . import session_db
    ok, _ = session_db.save_entries(entries, None)
    if ok:
        log.info(
            "Migrated %d entr%s from %s into %s (the old file is kept as a backup)",
            len(entries), "y" if len(entries) == 1 else "ies", SESSION_FILE, SESSION_DB,
        )
    else:
        log.warning("Could not migrate %s into %s - continuing from the JSON this run",
                    SESSION_FILE, SESSION_DB)
    return entries


def save_session(entries: List[ImageEntry], path: Union[str, Path, None] = None) -> bool:
    """Writes the working list. Returns True on success - failure is
    logged but never raised, since losing the session is an
    inconvenience, not a reason to block shutdown.

    `path` writes a named session file the user chose; omitting it uses
    the app's own automatic session, which is the one restored at
    startup. Both use the identical format, so a named session can be
    opened later exactly like the automatic one.
    """
    if path is None:
        # The automatic session. A full write - callers that want this are
        # quitting or have just replaced the list, and both want every row
        # brought up to date rather than a delta.
        from . import session_db
        ok, _ = session_db.save_entries(entries, None)
        return ok
    payload = build_session_payload(entries)
    return write_session_payload(payload, path)


def build_session_payload(entries: List[ImageEntry]) -> dict:
    """Turns the working list into the plain dict that gets written.

    Split out from the writing so it can run off the GUI thread: this is
    two thirds of the cost of a save, and at 29k entries it froze the
    window for a third of a second every autosave.
    """
    return {
        "version": SESSION_FORMAT_VERSION,
        "saved_at": time.time(),
        "entries": [_entry_to_dict(e) for e in entries],
    }


def write_session_payload(payload: dict, path: Union[str, Path, None] = None) -> bool:
    """Writes an already-built payload, atomically and durably."""
    target = Path(path) if path else SESSION_FILE
    tmp = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename, so an interrupted write can't
        # leave a half-written session that fails to load next launch.
        #
        # The temp name is unique per write, not a fixed ".tmp": a
        # background autosave and a synchronous save (quitting, "Save
        # Session As") can overlap, and a shared scratch path would have
        # them writing over each other and renaming the wrong bytes into
        # place.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=target.name + ".", suffix=".tmp", delete=False,
        )
        tmp = Path(handle.name)
        with handle:
            handle.write(json.dumps(payload))
            # The rename below is atomic, but only orders itself against
            # data the filesystem has actually committed. Without this a
            # power cut can leave the rename durable and the contents
            # not - which is the one way this scheme still loses a
            # session. It costs a few milliseconds.
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
        _fsync_directory(target.parent)
        count = len(payload.get("entries") or ())
        log.info(
            "Saved session with %d entr%s to %s",
            count, "y" if count == 1 else "ies", target,
        )
        return True
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        # RuntimeError included deliberately: serializing a list another
        # thread is appending to raises it, and letting that escape a Qt
        # slot aborts the process rather than losing one autosave.
        log.warning("Could not save session to %s: %s", target, exc)
        if tmp is not None:
            # Don't leave scratch files behind on a failed write.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _fsync_directory(directory: Path) -> None:
    """Makes the rename itself durable. Best-effort - not every platform
    or filesystem allows opening a directory, and a failure here only
    costs the extra guarantee, not the write."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def has_saved_session() -> bool:
    """Whether the app's own automatic session exists on disk at all, in
    either store.

    Used to tell a genuinely empty queue (nothing was ever saved) apart
    from a session that exists but load_session() still came back empty -
    the latter only happens when the file is unreadable, corrupt, or a
    crash landed before the first write finished, which is worth surfacing
    differently after an unclean shutdown (DAN-487)."""
    from . import session_db
    return session_db.SESSION_DB.exists() or SESSION_FILE.exists()


def load_session(path: Union[str, Path, None] = None) -> List[ImageEntry]:
    """Restores the working list, or an empty list if there's nothing
    saved or it can't be read.

    `path` opens a named session file - always JSON, always exactly the
    file the user picked. Omitting it uses the app's own automatic
    session, which lives in SQLite so its saves can be incremental; a
    session.json left over from before that change is read once and
    migrated.
    """
    if path is None:
        from . import session_db
        if SESSION_DB.exists():
            entries, skipped = session_db.load_entries()
            if skipped:
                log.error("Automatic session loaded with %d corrupt row(s) dropped - "
                          "some images may be missing from the list", skipped)
            if entries:
                _retire_legacy_session()
            return entries
        if SESSION_FILE.exists():
            return _migrate_legacy_session()
        return []

    target = Path(path)
    if not target.exists():
        log.warning("Session file does not exist: %s", target)
        return []
    return _load_json_session(target)


def _retire_legacy_session() -> None:
    """Removes a migrated session.json once it has been a backup long enough.

    Only called after session.db has just loaded with entries in it, so
    there is always a working session to fall back on. Anything short of
    a clear case - newer than the database, too recent, unreadable - is
    left alone."""
    try:
        legacy = SESSION_FILE.stat()
        current = SESSION_DB.stat()
    except OSError:
        return
    age_days = (time.time() - legacy.st_mtime) / 86400
    if legacy.st_mtime >= current.st_mtime or age_days < LEGACY_BACKUP_DAYS:
        return
    try:
        SESSION_FILE.unlink()
        log.info("Removed %s, migrated into %s %.0f days ago (%.0f MB)",
                 SESSION_FILE, SESSION_DB, age_days, legacy.st_size / 1e6)
    except OSError as exc:
        log.warning("Could not remove the old session backup %s: %s", SESSION_FILE, exc)


def _load_json_session(target: Path) -> List[ImageEntry]:
    """Reads one JSON session document. Used for named sessions, and once
    more for a legacy automatic session on its way into SQLite."""
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Could not read session %s: %s", target, exc)
        return []

    if not isinstance(payload, dict):
        log.warning("%s is not a session file (expected a JSON object)", target)
        return []
    version = payload.get("version")
    if version != SESSION_FORMAT_VERSION:
        log.info("Ignoring session %s written in format v%s (this build expects v%s)",
                 target, version, SESSION_FORMAT_VERSION)
        return []

    entries: List[ImageEntry] = []
    for raw in payload.get("entries") or []:
        try:
            entry = _entry_from_dict(raw)
        except Exception as exc:
            log.warning("Skipping a malformed session entry: %s", exc)
            continue
        if entry is not None:
            entries.append(entry)

    log.info("Restored %d entr%s from %s",
             len(entries), "y" if len(entries) == 1 else "ies", target)
    return entries


def session_entry_count(path: Union[str, Path]) -> Optional[int]:
    """How many entries a session file holds, without building any of
    them - used to describe a file before replacing the current list with
    it, since that's a destructive action worth previewing."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version") == SESSION_FORMAT_VERSION:
            return len(payload.get("entries") or [])
    except (OSError, ValueError):
        pass
    return None


def clear_session() -> bool:
    """Discards the automatic session.

    Both stores are cleared: the SQLite one that is actually used, and a
    legacy session.json if one is still sitting there - leaving that
    behind would have it migrated back in on the next launch, which is
    the opposite of what clearing means.
    """
    from . import session_db
    ok = True
    try:
        session_db.clear()
    except Exception as exc:      # pragma: no cover - defensive
        log.warning("Could not clear the session database: %s", exc)
        ok = False
    try:
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()
    except OSError as exc:
        log.warning("Could not remove the legacy session file: %s", exc)
        ok = False
    if ok:
        log.info("Cleared the saved session")
    return ok
