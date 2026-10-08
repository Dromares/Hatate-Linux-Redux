from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.hydrus_client import HydrusClient, HydrusError
from core.hydrus_import import finish_url_import
from core.hydrus_import_poll import DEFAULT_INTERVAL, DEFAULT_TIMEOUT, extract_confirmed_hash
from core.models import ImageEntry

log = get_logger("hydrus_import_poll_worker")

MAX_CONCURRENT_CHECKS = 8  # cap on simultaneous status checks per round, so a large
                           # batch doesn't hammer Hydrus with dozens of requests at once


class HydrusImportPollWorker(QThread):
    """After queuing URLs with Hydrus's own downloader
    (POST /add_urls/add_url), polls Hydrus to confirm each one actually
    finished importing - not just that the request was accepted, since
    Hydrus's downloader works asynchronously and can take a while.

    Checks multiple pending entries concurrently within each round (up to
    MAX_CONCURRENT_CHECKS at once), rather than one sequential HTTP
    request at a time - for a large batch, a fully sequential round can
    itself take much longer than the configured timeout, which combined
    with tracking elapsed time in fixed per-round increments used to
    silently stretch the effective timeout out to many minutes. Elapsed
    time is now tracked per-entry with a real wall-clock timestamp, so
    the timeout is honored accurately no matter how large the batch is."""

    entry_resolved = pyqtSignal(object, object, object)  # ImageEntry, confirmed_hash or None,
                                                          # warning string or None. The warning is
                                                          # what ImportResult.warning carries on the
                                                          # synchronous paths - this signal had no
                                                          # equivalent, so a duplicate relationship
                                                          # that could not be recorded had nowhere
                                                          # to be reported (DAN-71)
    wait_countdown = pyqtSignal(str, float, float)  # label, seconds remaining, total seconds -
                                                      # same shared signal shape as SearchWorker's,
                                                      # so the GUI can show live feedback here too
    finished_all = pyqtSignal()

    def __init__(
        self, client: HydrusClient, entries_with_urls: List[Tuple[ImageEntry, str]],
        timeout: float = DEFAULT_TIMEOUT, interval: float = DEFAULT_INTERVAL, parent=None,
        settings=None,
    ):
        super().__init__(parent)
        self.client = client
        self.entries_with_urls = entries_with_urls
        # Only read for set_hydrus_duplicate_relationships and
        # write_hydrus_provenance_note. Optional, and absent reads as off -
        # so a caller that does not pass it gets the behaviour this worker
        # had before DAN-71, unchanged.
        self.settings = settings
        self.timeout = timeout
        self.interval = interval
        self._stop_requested = False
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list = []

    def stop(self):
        self._stop_requested = True
        # Cancel any in-flight futures so they don't emit signals after stop.
        if self._executor is not None:
            # Python 3.9+: cancel_futures=True cancels not-yet-started futures.
            # Running futures can't be interrupted, but we'll ignore their results.
            self._executor.shutdown(wait=False, cancel_futures=True)
        for f in self._futures:
            f.cancel()

    def _check_one(self, item: Tuple[ImageEntry, str]):
        entry, url = item
        try:
            data = self.client.get_url_files(url)
        except HydrusError as exc:
            log.warning("get_url_files failed for %s: %s (will retry)", entry.filename, exc)
            data = {}
        return entry, url, extract_confirmed_hash(data)

    def _relate(self, entry: ImageEntry, confirmed_hash: str):
        """The bookkeeping that follows a confirmed import: how the file
        relates to the user's local copy, and the note recording where the
        match came from. Both off by default; neither makes a request when
        off.

        Never raises: this runs after Hydrus has confirmed the import, and
        losing the confirmation (and with it the removal from the list)
        over a piece of bookkeeping that follows it would be worse than
        the missing relationship or note. Same rule as
        record_duplicate_relationship's, which this delegates to."""
        try:
            return finish_url_import(
                entry, self.client, self.settings, entry.hydrus_hash, confirmed_hash)
        except Exception as exc:                     # noqa: BLE001 - see the docstring
            log.warning("Could not finish the Hydrus import bookkeeping for %s: %s",
                        entry.filename, exc)
            return None

    def run(self):
        total = len(self.entries_with_urls)
        log.info("Polling Hydrus to confirm %d queued import(s) (up to %.0fs each)", total, self.timeout)

        start_times = {id(entry): time.monotonic() for entry, _url in self.entries_with_urls}
        pending: List[Tuple[ImageEntry, str]] = list(self.entries_with_urls)

        while pending and not self._stop_requested:
            still_pending: List[Tuple[ImageEntry, str]] = []
            worker_count = min(MAX_CONCURRENT_CHECKS, len(pending))

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                self._executor = executor
                self._futures = [executor.submit(self._check_one, item) for item in pending]
                for future in as_completed(self._futures):
                    if self._stop_requested:
                        # Stop requested: ignore this result, don't emit signals.
                        continue
                    entry, url, confirmed_hash = future.result()

                    if confirmed_hash:
                        log.info("Confirmed Hydrus import for %s (hash=%s)", entry.filename, confirmed_hash)
                        # Done here rather than in _check_one, and rather
                        # than in the GUI's handler. Not in _check_one
                        # because that runs speculatively in the pool and
                        # its result is DISCARDED after a stop request -
                        # discarding a relationship already written into
                        # the user's library is not possible, so it must
                        # not be written until past the stop check above.
                        # Not in the GUI handler because it downloads a
                        # full-resolution file and perceptually hashes it,
                        # which would freeze the window.
                        #
                        # entry.hydrus_hash is still the LOCAL copy's hash
                        # at this point; the GUI handler is what replaces
                        # it with the confirmed one.
                        warning = self._relate(entry, confirmed_hash)
                        self.entry_resolved.emit(entry, confirmed_hash, warning)
                        continue

                    elapsed = time.monotonic() - start_times[id(entry)]
                    if elapsed >= self.timeout:
                        log.warning(
                            "Could not confirm Hydrus import for %s within %.0fs - "
                            "leaving it in the list (it may still be downloading)",
                            entry.filename, self.timeout,
                        )
                        self.entry_resolved.emit(entry, None, None)
                    else:
                        still_pending.append((entry, url))

                self._executor = None
                self._futures = []

            pending = still_pending
            if pending and not self._stop_requested:
                log.debug("%d/%d still pending confirmation, next check in %.0fs", len(pending), total, self.interval)
                self.wait_countdown.emit(
                    f"Confirming {len(pending)} Hydrus import(s)", self.interval, self.interval,
                )
                time.sleep(self.interval)

        self.wait_countdown.emit("", 0.0, 0.0)  # clear the countdown display
        log.info("Hydrus import poll finished")
        self.finished_all.emit()
