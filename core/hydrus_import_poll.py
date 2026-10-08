"""Confirms whether Hydrus's own downloader has actually finished
importing a URL, as opposed to just having accepted the request.

POST /add_urls/add_url only tells you Hydrus *accepted* the URL for its
downloader queue - the actual fetch+import happens asynchronously and can
take anywhere from under a second to much longer depending on the site.
GET /add_urls/get_url_files reflects real database state: its
url_file_statuses list is empty while nothing has been imported for that
URL yet, and gains an entry once something has (status 2 = already in
database, meaning genuinely imported and present; status 3 = previously
deleted, meaning it was imported and then removed - either way, Hydrus
has resolved the URL to an actual file, so it's no longer "in progress").
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .applog import get_logger
from .hydrus_client import HydrusClient, HydrusError

log = get_logger("hydrus_import_poll")

# Statuses that mean "Hydrus has resolved this URL to a real file" -
# status 0 ("not in database, ready for import") is explicitly documented
# as a rare transient state, not a normal "still downloading" signal, so
# it's treated the same as an empty list: keep waiting.
RESOLVED_STATUSES = (2, 3)

# Shared with workers/hydrus_import_poll_worker.py (the batch/concurrent
# QThread version used by the manual "Send URL to Hydrus's Importer"
# action) - defined here once so both stay in sync.
DEFAULT_TIMEOUT = 60.0
DEFAULT_INTERVAL = 2.0


def extract_confirmed_hash(get_url_files_response: dict) -> Optional[str]:
    """Returns the file hash if Hydrus has confirmed an import for this
    URL, or None if it's still in progress (or genuinely has nothing)."""
    for status_entry in get_url_files_response.get("url_file_statuses") or []:
        if status_entry.get("status") in RESOLVED_STATUSES and status_entry.get("hash"):
            return status_entry["hash"]
    return None


def poll_single_url_import(
    client: HydrusClient, url: str,
    timeout: float = DEFAULT_TIMEOUT, interval: float = DEFAULT_INTERVAL,
    stop_check: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float, float], None]] = None,
) -> Optional[str]:
    """Blocks, checking Hydrus every `interval` seconds, until it confirms
    the given URL has actually been imported (not just accepted) or
    `timeout` seconds pass. Returns the confirmed file hash, or None if it
    never confirmed in time (the import may still genuinely be in
    progress - this isn't necessarily a failure).

    If given, on_progress(remaining_seconds, timeout) is called once per
    check so the caller can show a live countdown while this blocks -
    without it, a caller has no way to know this is actively working
    versus stalled.

    This is a plain blocking function, not a QThread - safe to call from
    any background thread (e.g. SearchWorker's own thread, which is
    already off the GUI thread, so blocking it briefly doesn't freeze the
    UI). For confirming several URLs at once with concurrent checks, see
    workers/hydrus_import_poll_worker.py instead."""
    start = time.monotonic()
    while True:
        if stop_check and stop_check():
            return None
        try:
            data = client.get_url_files(url)
        except HydrusError as exc:
            log.warning("get_url_files failed while polling %s: %s (will retry)", url, exc)
            data = {}

        confirmed_hash = extract_confirmed_hash(data)
        if confirmed_hash:
            return confirmed_hash

        elapsed = time.monotonic() - start
        remaining = max(timeout - elapsed, 0.0)
        if on_progress:
            on_progress(remaining, timeout)
        if remaining <= 0:
            return None
        time.sleep(min(interval, remaining))
