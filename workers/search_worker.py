from __future__ import annotations

import atexit
import os
import random
import shutil
import signal
import sys
import tempfile
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core import engines as engine_ids
from core.config import Settings
from core.hydrus_client import HydrusClient
from core.hydrus_import import (
    download_and_send, download_instead, normalize_url_for_hydrus,
    finish_url_import, send_file_upload, send_url_to_importer, url_import_refusal,
)
from core.hydrus_import_poll import poll_single_url_import
from core.logger import log_matched_url
from core.models import ImageEntry, MatchStatus
from core.paths import AUTO_IMPORT_STAGING_DIR
from core.rate_limit import EngineRateLimiter, engine_interval, remaining_delay
from core.saucenao import is_daily_quota_exhausted, record_quota_pause
from core.search_engine import planned_engines, retry_could_help, search_image

log = get_logger("search_worker")

# Module-level tracking of the current staging directory for atexit/signal cleanup.
# Only one SearchWorker runs at a time in practice, so a single global is fine.
_current_staging_dir: Optional[str] = None
_cleanup_registered = False

# PID file name used to track ownership of staging directories
_PID_FILE_NAME = "owner.pid"


def _write_pid_file(staging_dir: str) -> None:
    """Write the current process PID to a file in the staging directory."""
    pid_file = os.path.join(staging_dir, _PID_FILE_NAME)
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        log.warning("Could not write PID file to %s: %s", staging_dir, exc)


def _read_pid_file(staging_dir: str) -> Optional[int]:
    """Read the PID from the staging directory's PID file, if it exists and is valid."""
    pid_file = os.path.join(staging_dir, _PID_FILE_NAME)
    try:
        with open(pid_file, "r") as f:
            content = f.read().strip()
            return int(content) if content.isdigit() else None
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.
    
    On Unix, os.kill(pid, 0) raises OSError with errno=ESRCH if the process
    doesn't exist, and errno=EPERM if it exists but we can't signal it.
    Both cases mean the process exists from our perspective.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        import errno
        if exc.errno == errno.ESRCH:
            return False  # No such process
        # EPERM means process exists but we can't signal it - treat as alive
        return True
    return True


def register_cleanup_handlers():
    """Register atexit and signal handlers for staging directory cleanup.
    Must be called from the main thread for signal.signal() to work.
    Called once on first use."""
    global _cleanup_registered
    if _cleanup_registered:
        return
    
    def cleanup():
        global _current_staging_dir
        if _current_staging_dir:
            shutil.rmtree(_current_staging_dir, ignore_errors=True)
            log.debug("Cleaned up auto-import staging directory %s (atexit/signal)", _current_staging_dir)
            _current_staging_dir = None
    
    atexit.register(cleanup)
    # Handle graceful shutdown signals.
    def _sigint_handler(sig, frame):
        cleanup()
        # Re-raising via signal.default_int_handler/raise_signal (the
        # "textbook" way to report a SIGINT exit) only propagates on a
        # normal Python call stack. Delivered here, it surfaces as a
        # KeyboardInterrupt inside a Qt slot dispatch (the wakeup timer in
        # main.py), which PyQt routes to sys.excepthook and then swallows -
        # logging it but leaving the event loop running, so the process
        # never actually exits and needs a SIGKILL. sys.exit() raises
        # SystemExit instead, which does propagate through that same path
        # (SIGTERM's handler below already relies on this), so use the
        # same mechanism here with SIGINT's conventional 128+n exit code.
        sys.exit(128 + signal.SIGINT)
    
    def _sigterm_handler(sig, frame):
        cleanup()
        # Default SIGTERM behaviour is to exit; we do that explicitly
        sys.exit(1)
    
    try:
        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError):
        # Signal may not be available (e.g. Windows) or handler already set
        pass
    
    _cleanup_registered = True


def _cleanup_stale_staging_dirs():
    """Remove any leftover staging directories from previous runs.
    Called on worker startup to handle SIGKILL/OOM/power-loss cases.
    
    Only removes directories whose owner process is provably gone (PID file
    missing or PID not alive). This prevents cross-process interference where
    instance B's startup sweep would delete instance A's in-flight staging files.
    """
    try:
        AUTO_IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
        for child in AUTO_IMPORT_STAGING_DIR.iterdir():
            if not (child.is_dir() and child.name.startswith("hatate-linux-auto-")):
                continue
            
            pid = _read_pid_file(str(child))
            if pid is not None and _pid_is_alive(pid):
                log.debug("Skipping live staging directory %s (owner PID %d)", child, pid)
                continue
            
            shutil.rmtree(child, ignore_errors=True)
            log.info("Cleaned up stale auto-import staging directory %s (owner PID %s)", child, pid if pid else "unknown")
    except OSError as exc:
        log.warning("Could not clean up stale staging directories: %s", exc)


class SearchWorker(QThread):
    """Searches a list of ImageEntry objects one at a time, waiting a random
    delay (Settings.delay_min_seconds..delay_max_seconds) between each
    request to avoid abusing IQDB/SauceNAO, exactly like the original
    Hatate's rate limiting."""

    image_updated = pyqtSignal(object)    # the ImageEntry instance that was just searched
    progress = pyqtSignal(int, int)       # done, total
    waiting = pyqtSignal(float)           # seconds until next search
    wait_countdown = pyqtSignal(str, float, float)  # label, seconds remaining, total seconds -
                                                      # a general "waiting on something" indicator,
                                                      # used for the rate-limit wait and for confirming
                                                      # a URL-importer download during auto-import
    auto_imported = pyqtSignal(object, object)  # ImageEntry, ImportResult - see Settings > General
    paused_out_of_quota = pyqtSignal(int, int)  # images searched, images left unsearched
    continuing_without_saucenao = pyqtSignal(int, int)  # images searched, images left to do
    finished_all = pyqtSignal()

    def __init__(
        self, entries: List[ImageEntry], settings: Settings,
        override_engines: Optional[Dict[int, str]] = None, bypass_cache: bool = False,
        force_continue_without_saucenao: bool = False, parent=None,
    ):
        super().__init__(parent)
        self.entries = entries
        self.settings = settings
        # One-shot override for "Continue with other engines" from the
        # quota-pause dialog: behaves exactly like
        # settings.continue_without_saucenao_on_quota for THIS run only,
        # without touching the user's persisted setting (DAN-486).
        self._force_continue_without_saucenao = force_continue_without_saucenao
        # Optional {id(entry): "iqdb"|"saucenao"|"ascii2d"|"tracemoe"|"iqdb3d"|
        #                       "googleimages"|"googlelens"|"yandex"|"pawchive"}
        # - forces that single engine for that entry, bypassing the
        # configured primary/fallback order and extra-engine toggles.
        # Used by "Search with a specific engine" / "Opposite Engine".
        self.override_engines = override_engines or {}
        # Skips the search-result cache for every entry in this batch -
        # used by "Re-search" and "Search with Opposite Engine", which
        # exist specifically to get a fresh result, not a saved one.
        self.bypass_cache = bypass_cache
        self._stop_requested = False
        self._auto_import_tmp_dir = None
        self._announced_quota_continue = False   # say it once, not once per image
        # Per-host pacing: one clock per engine, kept for the whole batch
        # so each host's gap is measured from ITS own last request rather
        # than from whenever the previous image happened to finish.
        self._limiter = EngineRateLimiter()

    def stop(self):
        log.info("Stop requested for search worker (%d image(s) queued)", len(self.entries))
        self._stop_requested = True

    def run(self):
        try:
            self._run()
        finally:
            self._cleanup_auto_import_tmp()

    def _queue_retries(self, done: List[ImageEntry]) -> List[ImageEntry]:
        """The images worth one more attempt, after the first pass.

        An ERROR is a network fault - a timeout, a 502, a rate limit -
        rather than a verdict about the image. search_cache already knows
        the difference and deliberately refuses to remember them, "since
        those should just be retried normally, not remembered as a fixed
        outcome". Nothing ever did the retrying, though: the run ended and
        the rows sat there until someone noticed and re-searched by hand.
        On this library that was 12 in the first 1,262 searched, about 1%,
        which over a full pass is a few hundred images needing attention
        for faults that mostly clear on a second try.

        Exactly one extra attempt, and only for rows THIS run touched:
        a persistent failure must not become an infinite loop, and an
        error left over from an earlier run is one the user has already
        had the chance to see.

        Nothing is queued once stopping, or once the daily quota has run
        out - a retry then would just fail the same way and spend the
        pacing delay doing it.
        """
        if not self.settings.retry_failed_searches:
            return []
        if self._stop_requested or self._saucenao_quota_should_pause():
            return []
        failed = [e for e in done if e.status is MatchStatus.ERROR]
        retryable = [e for e in failed if retry_could_help(e)]
        if len(retryable) < len(failed):
            # Their only failures were engines that have stood down for
            # the run (Lens without Playwright, a Google challenge, an
            # ascii2d block). A retry would skip that engine and fail again.
            log.info(
                "Not retrying %d image(s): the engines that failed on them have "
                "stopped for this run - see the errors above",
                len(failed) - len(retryable),
            )
        if not retryable:
            return []
        log.info(
            "First pass done - retrying %d image(s) that failed on a network fault",
            len(retryable),
        )
        return retryable

    def _cleanup_auto_import_tmp(self):
        """Removes the staging directory the download-and-send auto-import
        method downloads into.

        Uses a persistent cache directory (XDG_CACHE_HOME) instead of /tmp
        so that files survive reboot and can be cleaned up on next startup
        if the process was killed (SIGKILL, OOM, power loss).
        """
        tmp_dir = getattr(self, "_auto_import_tmp_dir", None)
        if not tmp_dir:
            return
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.debug("Cleaned up auto-import staging directory %s", tmp_dir)
        self._auto_import_tmp_dir = None
        # Also clear the module-level tracker
        global _current_staging_dir
        if _current_staging_dir == tmp_dir:
            _current_staging_dir = None

    def _run(self):
        # Clean up any stale staging directories from previous runs
        _cleanup_stale_staging_dirs()
        # Note: atexit/signal handlers are registered at app startup from the
        # main thread (see main.py). Registering them from a QThread would
        # fail with ValueError and be silently swallowed.

        # A queue rather than the list itself, because it can grow: an
        # image that failed on a network fault gets one more attempt
        # appended after the first pass. See _queue_retries.
        queue: List[ImageEntry] = list(self.entries)
        retries_queued = False
        total = len(queue)
        log.info("Search worker starting: %d image(s) to process", total)

        auto_import_client: Optional[HydrusClient] = None
        auto_import_tmp_dir: Optional[str] = None
        if self.settings.auto_import_enabled and self.settings.hydrus.access_key:
            auto_import_client = HydrusClient(self.settings.hydrus)
            if self.settings.auto_import_method in ("download_send", "url_importer"):
                # url_importer too: when Hydrus can't take a URL, Hatate
                # downloads the file itself (hydrus_import.download_instead).
                # Use persistent cache directory instead of /tmp
                AUTO_IMPORT_STAGING_DIR.mkdir(parents=True, exist_ok=True)
                auto_import_tmp_dir = tempfile.mkdtemp(prefix="hatate-linux-auto-", dir=AUTO_IMPORT_STAGING_DIR)
                # Write PID file to mark this directory as owned by this process
                _write_pid_file(auto_import_tmp_dir)
                # Recorded on the worker so the cleanup in run() can find
                # it however this method exits - including the stop path
                # and an unexpected exception.
                self._auto_import_tmp_dir = auto_import_tmp_dir
                # Also track at module level for atexit/signal cleanup
                global _current_staging_dir
                _current_staging_dir = auto_import_tmp_dir
            log.info(
                "Auto-import is on: matches at or above %.0f%% similarity will be sent to Hydrus "
                "(method=%s) as each one is found",
                self.settings.auto_import_min_similarity, self.settings.auto_import_method,
            )

        i = -1
        while i + 1 < len(queue):
            i += 1
            entry = queue[i]
            total = len(queue)   # grows once, when the retries are queued
            if self._stop_requested:
                log.info("Search worker stopping early at %d/%d (user requested)", i, total)
                break

            entry.status = MatchStatus.SEARCHING
            self.image_updated.emit(entry)

            entry.result_source = None  # reset so a failed call below can't inherit a stale
                                         # "cached" value from an earlier search on this entry
            # Stays None if no engine request completes (a cache hit, or a
            # failure before one finished) - which makes the wait below fall
            # back to the full delay.
            engines_finished_at: Optional[float] = None

            def _note_engines_done():
                nonlocal engines_finished_at
                engines_finished_at = time.monotonic()

            engines_reported = 0

            def _note_engine_finished(engine: str):
                nonlocal engines_reported
                engines_reported += 1
                self._limiter.record(engine, time.monotonic())

            # SauceNAO's daily allowance may have gone during this run.
            # Asking anyway spends a request to be told so, and the result
            # is provisional either way - see _skip_saucenao_now.
            skip_saucenao = self._skip_saucenao_now()
            try:
                override = self.override_engines.get(id(entry))
                use_cache = self.settings.use_search_cache and not self.bypass_cache
                search_image(
                    entry, self.settings, override_engine=override, use_cache=use_cache,
                    on_tick=self.wait_countdown.emit,
                    on_engines_done=_note_engines_done,
                    on_engine_finished=_note_engine_finished,
                    # So a wait on SauceNAO's burst window answers Stop as
                    # promptly as the pacing between images does.
                    should_stop=lambda: self._stop_requested,
                    skip_saucenao=skip_saucenao,
                )
            except Exception as exc:
                # A bug or an unexpected condition here would otherwise kill
                # the whole batch silently (Qt threads don't surface Python
                # tracebacks) - log it, mark this one image as errored, and
                # keep going with the rest instead of losing the whole run.
                log.exception("Unexpected error searching %s: %s", entry.filename, exc)
                entry.status = MatchStatus.ERROR
                entry.error_message = f"Unexpected error: {exc}"

            self.image_updated.emit(entry)
            self.progress.emit(i + 1, total)
            log.debug("(%d/%d) %s -> %s", i + 1, total, entry.filename, entry.status.value)

            if (
                self.settings.log_matched_urls
                and entry.matched_url
                and entry.status in (MatchStatus.GOOD, MatchStatus.POOR)
            ):
                log_matched_url(self.settings.log_file_path, entry.filename, entry.matched_url)

            if auto_import_client is not None:
                self._maybe_auto_import(entry, auto_import_client, auto_import_tmp_dir)

            remaining = total - (i + 1)
            if remaining > 0 and self._skip_saucenao_now():
                if not self._announced_quota_continue:
                    self._announced_quota_continue = True
                    log.warning(
                        "SauceNAO's daily quota is exhausted - carrying on without it for the "
                        "remaining %d image(s). Those results are marked provisional and are "
                        "not cached, so re-searching them once the allowance resets gets the "
                        "full answer (Settings > SauceNAO)", remaining,
                    )
                    self.continuing_without_saucenao.emit(i + 1, remaining)
            elif remaining > 0 and self._saucenao_quota_should_pause():
                log.warning(
                    "SauceNAO's daily quota is exhausted - pausing after %d/%d image(s), "
                    "leaving %d unsearched (Settings > SauceNAO)", i + 1, total, remaining,
                )
                # Persisted, not just signalled: the signal only reaches
                # whoever is looking at THIS moment, and the whole point of
                # an unattended run is that nobody necessarily is (DAN-486).
                record_quota_pause(i + 1, remaining)
                self.paused_out_of_quota.emit(i + 1, remaining)
                break

            if i == len(queue) - 1 and not retries_queued:
                # End of the first pass. Anything that ended in ERROR
                # failed on a network fault, not on a verdict - which is
                # why search_cache refuses to remember those - so each
                # gets exactly one more attempt before the run ends.
                retries_queued = True
                queue.extend(self._queue_retries(queue))
                total = len(queue)

            if self._stop_requested or i == len(queue) - 1:
                continue

            if entry.result_source == "cached":
                # No real IQDB/SauceNAO request was made for this one, so
                # there's nothing to rate-limit against - move straight on
                # to the next image instead of waiting for no reason.
                log.debug("%s was served from the search cache, skipping the rate-limit wait", entry.filename)
                continue

            delay = random.uniform(self.settings.delay_min_seconds, self.settings.delay_max_seconds)
            # The gap that matters is engine-request to engine-request. The
            # booru page fetch, the availability sweep, the preview download
            # and any auto-import polling all happened since, and none of
            # them touched IQDB/SauceNAO - so that time counts toward the
            # gap rather than being waited out a second time.
            # Paced per host. Each engine is a separate server with its own
            # clock, so the wait is the longest outstanding gap among the
            # engines the NEXT image will actually query - a run that skips
            # a host owes it nothing.
            #
            # An engine with no override of its own uses the global delay,
            # so with nothing configured this works out to the same wait
            # the single global gap gave, measured from the same moment.
            # That is deliberate: the global delay stays the default for
            # every engine rather than becoming a floor underneath them,
            # because a floor would make a SHORTER per-engine setting do
            # nothing - and that setting is the entire point.
            next_entry = queue[i + 1]
            upcoming = planned_engines(
                self.settings, self.override_engines.get(id(next_entry)),
            )
            intervals = {
                engine: random.uniform(*engine_interval(
                    engine, self.settings.engine_delays,
                    self.settings.delay_min_seconds, self.settings.delay_max_seconds,
                ))
                for engine in upcoming
            }

            if engines_reported:
                wait = self._limiter.wait_needed(upcoming, intervals, time.monotonic())
            else:
                # This image ran a fresh search - a cached one already
                # skipped ahead above - yet no engine reported finishing,
                # so the per-engine clocks cannot be trusted for it. Fall
                # back to the global delay: the failure mode of this whole
                # mechanism has to be waiting too long, never not at all.
                log.warning(
                    "No engine reported completing for %s; falling back to the global "
                    "delay rather than per-host pacing", entry.filename,
                )
                wait = remaining_delay(delay, engines_finished_at, time.monotonic())

            if wait < delay:
                log.debug(
                    "Waiting %.1fs before next search (rate limiting; %.1fs of the %.1fs gap "
                    "already elapsed on non-engine work)", wait, delay - wait, delay,
                )
            else:
                log.debug("Waiting %.1fs before next search (rate limiting)", wait)
            if wait > 0:
                self._wait_with_countdown(wait)

        log.info("Search worker finished (%d/%d processed)", i + 1 if total else 0, total)
        self.finished_all.emit()

    def _skip_saucenao_now(self) -> bool:
        """Whether to leave SauceNAO out of the searches from here on.

        Only when the user has asked for it. The default is still to
        stop, and the reasoning for that has not changed - a result found
        without SauceNAO is weaker than one found with it. What changed is
        that the weaker result no longer has to be permanent: it is marked
        provisional, kept out of the cache, and re-searchable in one
        action once the allowance resets.

        The trade this setting makes is idle time against provisional
        results. A 24,000-image library against a 5,000-a-day allowance is
        five days, most of them spent waiting for midnight, and IQDB has
        no daily cap at all.

        _force_continue_without_saucenao is the one-shot equivalent set by
        "Continue with other engines" on an already-paused batch (DAN-486)
        - same effect, scoped to this run instead of the saved setting.
        """
        if not (self.settings.continue_without_saucenao_on_quota
                or self._force_continue_without_saucenao):
            return False
        if not self._saucenao_in_use():
            return False
        return is_daily_quota_exhausted()

    def _saucenao_in_use(self) -> bool:
        return (
            self.settings.primary_engine == "saucenao"
            or self.settings.secondary_engine_mode != "disabled"
        )

    def _saucenao_quota_should_pause(self) -> bool:
        """Whether to stop the batch because SauceNAO's daily allowance is
        spent. Only applies when SauceNAO is actually in the engine order:
        with it disabled entirely, its quota is irrelevant and stopping
        would be nonsense.

        Note this pauses even when SauceNAO is only the secondary engine.
        The point isn't that searching becomes impossible - IQDB may still
        work - it's that every remaining image would be recorded with a
        result weaker than it would otherwise have got, and those results
        get cached and marked searched. Stopping keeps the rest of the
        list clean to retry after the quota resets."""
        if not self.settings.saucenao.pause_search_on_quota_exhausted:
            return False

        # Continuing takes precedence: the user has said explicitly what
        # they want to happen here, and it is not stopping - whether that
        # came from the saved setting or this run's one-shot override.
        if self.settings.continue_without_saucenao_on_quota or self._force_continue_without_saucenao:
            return False
        if not self._saucenao_in_use():
            return False

        return is_daily_quota_exhausted()

    def _maybe_auto_import(self, entry: ImageEntry, client: HydrusClient, tmp_dir: Optional[str]):
        """Sends a just-found match straight to Hydrus if it's confident
        enough - runs right after this image finishes searching, before
        moving on to the next one, per Settings > General."""
        if entry.sent_to_hydrus:
            return  # already handled (e.g. served from cache after an earlier manual send)
        if entry.status not in (MatchStatus.GOOD, MatchStatus.POOR):
            return
        if entry.similarity is None or entry.similarity < self.settings.auto_import_min_similarity:
            return

        selected = entry.selected_candidate
        if selected is not None and selected.remote_available is False:
            # This match's source is confirmed gone, so there's nothing
            # for Hydrus to fetch. Skipping is especially worth it for the
            # url_importer method, which would otherwise burn the whole
            # confirmation timeout waiting on a download that can't
            # possibly succeed.
            log.info(
                "Skipping auto-import for %s - its matched source is gone (%s)",
                entry.filename, selected.url,
            )
            return

        method = self.settings.auto_import_method
        log.info(
            "Auto-importing %s (%.0f%% similarity, method=%s)",
            entry.filename, entry.similarity, method,
        )
        if method == "url_importer":
            result = send_url_to_importer(entry, client, self.settings)
            if result.refused and self._switch_to_importable_match(entry, client):
                result = send_url_to_importer(entry, client, self.settings)
            if result.refused and tmp_dir:
                # No match Hydrus can take - Hatate fetches the file itself
                # where the site lets it (see download_instead).
                result = download_instead(entry, client, self.settings, tmp_dir, result)
                if result.note:
                    log.info("Auto-import: %s for %s", result.note, entry.filename)
            needs_confirmation = (self.settings.remove_after_import
                                  or self.settings.set_hydrus_duplicate_relationships
                                  or self.settings.write_hydrus_provenance_note)
            if result.success and result.confirmed is None and needs_confirmation:
                # Don't just trust that Hydrus *accepted* the request -
                # actually poll it to confirm the file was imported before
                # this can be removed from the list. Blocks this worker
                # thread for up to the poll timeout, which is fine (it's
                # already off the GUI thread) - if none of the three
                # things that need a confirmed hash is switched on, skip
                # this entirely and save the extra API calls/wait.
                #
                # Duplicate relationships are the second of those three,
                # and the reason this is no longer gated on
                # remove_after_import alone: the confirmed hash IS the new
                # file's identity, so without the poll there is nothing to
                # pair the user's local copy with. Gating on
                # remove_after_import by itself left
                # set_hydrus_duplicate_relationships silently doing nothing
                # for anyone who keeps their imports in the list. The
                # provenance note is the third, and needs the hash for the
                # same reason - it is the file the note goes onto.
                log.info(
                    "Auto-import (url_importer): confirming %s with Hydrus before it can "
                    "be removed from the list…", entry.filename,
                )
                assert entry.matched_url is not None  # url_importer only reaches success with one set
                confirmed_hash = poll_single_url_import(
                    client, normalize_url_for_hydrus(entry.matched_url),
                    timeout=self.settings.url_import_confirm_timeout,
                    interval=self.settings.url_import_confirm_interval,
                    stop_check=lambda: self._stop_requested,
                    on_progress=lambda remaining, total: self.wait_countdown.emit(
                        "Confirming Hydrus import", remaining, total,
                    ),
                )
                if confirmed_hash:
                    # Read BEFORE the assignment below overwrites it: this
                    # is the only hash that still identifies the copy the
                    # user already had, which is the whole point of the
                    # pairing. Same ordering as download_and_send's.
                    local_hash = entry.hydrus_hash
                    entry.hydrus_hash = confirmed_hash
                    result.confirmed = True
                    log.info("Confirmed Hydrus import for %s (hash=%s)", entry.filename, confirmed_hash)
                    # Both off by default, and neither makes a request at
                    # all when off (see finish_url_import).
                    result.warning = finish_url_import(
                        entry, client, self.settings, local_hash, confirmed_hash)
                    if result.warning:
                        entry.error_message = result.warning
                        log.warning("Auto-import: %s for %s", result.warning, entry.filename)
                else:
                    result.confirmed = False
                    log.warning(
                        "Could not confirm Hydrus import for %s within the poll window - "
                        "leaving it in the list (it may still be downloading)", entry.filename,
                    )
                self.wait_countdown.emit("", 0.0, 0.0)  # clear the countdown display
        elif method == "download_send":
            if not tmp_dir:
                log.error("Auto-import (download_send): no staging directory available for %s",
                          entry.filename)
                return
            result = download_and_send(entry, client, self.settings.search_timeout, tmp_dir, self.settings)
        else:
            result = send_file_upload(entry, client, self.settings)

        if result.success and result.confirmed is False:
            log.warning("Auto-import sent %s to Hydrus, but Hydrus has not confirmed importing "
                        "it - check Hydrus's downloader page", entry.filename)
        elif result.success:
            log.info("Auto-import succeeded for %s", entry.filename)
        elif result.skipped_reason:
            log.debug("Auto-import skipped for %s: %s", entry.filename, result.skipped_reason)
        else:
            log.warning("Auto-import failed for %s: %s", entry.filename, result.error)

        self.auto_imported.emit(entry, result)

    # How many other matches to ask Hydrus about once the chosen one is
    # refused. Each is one quick local API call.
    MAX_IMPORT_ALTERNATIVES = 5

    def _switch_to_importable_match(self, entry: ImageEntry, client: HydrusClient) -> bool:
        """Selects the best OTHER match Hydrus's URL importer can take.

        The chosen match is often a comic-aggregator page Hydrus cannot
        read, sitting at the same measured 100% as a booru post it can.
        Only matches that clear the auto-import threshold on a MEASURED
        score are considered, so falling back never lowers the bar.
        """
        threshold = self.settings.auto_import_min_similarity
        tried = 0
        for index, candidate in enumerate(entry.candidates):
            if index == entry.selected_candidate_index or tried >= self.MAX_IMPORT_ALTERNATIVES:
                continue
            if candidate.remote_available is False or (candidate.similarity or 0.0) < threshold:
                continue
            if not (candidate.similarity_measured
                    or engine_ids.reports_real_similarity(candidate.engine)):
                continue
            if not (candidate.url or "").startswith(("http://", "https://")):
                continue
            tried += 1
            if url_import_refusal(client, normalize_url_for_hydrus(candidate.url)):
                continue
            log.info("Auto-import: Hydrus can't take %s's top match, using %s (%.0f%%) instead",
                     entry.filename, candidate.url, candidate.similarity)
            entry.select_candidate(index)
            entry.error_message = None
            return True
        return False

    def _wait_with_countdown(self, delay: float):
        """Sleeps for `delay` seconds, emitting `waiting` (and the shared
        `wait_countdown`) once every time the displayed second actually
        changes - so the status bar shows a real countdown instead of a
        single static message that then sits there stale until the next
        search happens to start."""
        if delay <= 0:
            return
        step = 0.25
        # Counted against a fixed deadline rather than by accumulating
        # sleep() calls: each sleep overshoots slightly, and summing those
        # errors made a long wait drift past the delay it was asked for.
        deadline = time.monotonic() + delay
        last_shown = None
        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            shown = round(remaining)
            if shown != last_shown:
                self.waiting.emit(remaining)
                self.wait_countdown.emit("Rate limit", remaining, delay)
                last_shown = shown
            # Never sleep past the deadline. Crediting non-engine work
            # routinely leaves a wait shorter than one step, and a fixed
            # quarter-second sleep would overshoot it - giving back the
            # very time that was just credited.
            time.sleep(min(step, remaining))
        self.wait_countdown.emit("", 0.0, 0.0)  # clear the countdown display
