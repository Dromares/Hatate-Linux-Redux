from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.hydrus_tag_lookup import hash_file, hash_from_filename, verify_filename_hashes

log = get_logger("file_hash_worker")

# Don't offer an estimate until this much of the work is done - the first
# couple of files are a terrible sample (connection warm-up, cache state,
# and one atypically-sized file all skew it), and a wildly wrong number is
# worse than no number.
MIN_FRACTION_BEFORE_ESTIMATING = 0.02
MIN_SECONDS_BEFORE_ESTIMATING = 1.5

# Progress is emitted at most this often. Each emission is a queued
# cross-thread signal that repaints the status bar and progress bar, and
# for a batch of tens of thousands of files, one per file floods the GUI
# thread with far more updates than anyone can read - the repaints end up
# competing with the work being reported on.
PROGRESS_EMIT_INTERVAL_SECONDS = 0.1


class FileHashWorker(QThread):
    """Hashes a batch of files in the background. Hashing is the dominant
    cost when adding a large batch - especially over a network share,
    where sequential read throughput can be far below local disk - and
    running it synchronously on the GUI thread means the whole
    application can't repaint or respond to anything until every file has
    been fully read, which for tens of GB can take minutes with zero
    feedback. This moves that work off the GUI thread and reports real
    progress as it goes.

    Parallelism (Settings > General) is worth measuring rather than
    assuming: hashing is I/O-bound, and on a LOCAL disk, reading several
    files at once usually just contends for the same bandwidth and gains
    nothing. Over a NETWORK share it's a different story - each read has
    real round-trip latency that a single sequential stream spends idle,
    and overlapping several requests can genuinely fill that gap. Which
    of those applies depends entirely on the storage, so this logs its
    own throughput (MB/s) every run to make the two directly comparable.

    Note the hashing itself is done by hashlib, which releases the GIL
    during its actual digest work, so threads here aren't fighting over
    the interpreter lock the way pure-Python CPU work would.
    """

    progress = pyqtSignal(int, int, float)   # done, total, estimated seconds remaining
                                              # (-1.0 while there's not enough data to estimate)
    finished_hashing = pyqtSignal(dict)       # {path: hash} for every file successfully hashed
    # Note: the `stopped` signal from BA-04 was removed. WorkerLifecycle.retire()
    # calls worker.disconnect() before worker.stop(), which disconnects all signals.
    # Since the worker is always stopped via the lifecycle, a separate `stopped`
    # signal would never be delivered to any listener. The lifecycle already
    # prevents stale results from reaching the GUI by disconnecting first.
    # If a direct stop() call were ever made outside the lifecycle, the worker
    # simply emits no signal (finished_hashing is only emitted on normal completion).

    def __init__(self, paths: List[str], workers: int = 1,
                 hash_source: str = "hydrus", parent=None):
        super().__init__(parent)
        self.paths = paths
        self.workers = max(1, workers)
        # "hydrus" = take hashes from Hydrus-style filenames where possible;
        # "local" = always read every file. Anything unrecognised falls back
        # to reading, which is the slower but never-wrong option.
        self.hash_source = hash_source
        self._stop_requested = False
        self._total_bytes_cached = 0
        self._sizes: Dict[str, int] = {}
        self._last_progress_emit = 0.0

    def _emit_progress(self, done: int, total: int, bytes_done: int, elapsed: float):
        """Throttled progress. Always emits the final update so the bar
        can't be left stranded short of the end."""
        now = time.monotonic()
        if done < total and (now - self._last_progress_emit) < PROGRESS_EMIT_INTERVAL_SECONDS:
            return
        self._last_progress_emit = now
        self.progress.emit(done, total, self._estimate_remaining(bytes_done, elapsed))

    def stop(self):
        self._stop_requested = True

    def _total_bytes(self) -> int:
        """Totals the batch, keeping each file's size on the way past.

        The sizes are needed again per-file for the byte-based ETA, and
        re-stat'ing there meant two round trips per file. That's
        invisible locally but not on a network share, where every stat
        is a request over the wire - on a batch of tens of thousands,
        halving them is minutes of pure latency saved before a single
        byte is hashed."""
        total = 0
        for p in self.paths:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            self._sizes[p] = size
            total += size
        return total

    def _file_size(self, path: str) -> int:
        return self._sizes.get(path, 0)

    def _split_by_filename_hash(self):
        """Separates files whose hash can be read straight off their name
        from those that have to be read.

        Hydrus names files in its store after their SHA256, so a batch
        added from there already carries every hash in its paths. Taking
        them from the names turns reading tens of gigabytes into reading
        nothing at all - but only if the names really are content
        hashes, so a sample is actually verified first."""
        if self.hash_source != "hydrus":
            log.debug("hash_source=%r - reading every file rather than using filenames", self.hash_source)
            return {}, self.paths

        candidates = [p for p in self.paths if hash_from_filename(p)]
        if not candidates:
            return {}, self.paths

        log.info(
            "%d of %d file(s) are named like a SHA256 - verifying a sample before trusting them",
            len(candidates), len(self.paths),
        )
        if not verify_filename_hashes(candidates):
            return {}, self.paths

        from_name = {p: hash_from_filename(p) for p in candidates}
        remaining = [p for p in self.paths if p not in from_name]
        skipped_bytes = sum(self._file_size(p) for p in from_name)
        log.info(
            "Taking %d hash(es) from filenames - skipping %.1f MB of reads; %d file(s) still to hash",
            len(from_name), skipped_bytes / 1024 / 1024, len(remaining),
        )
        return from_name, remaining

    def run(self):
        total = len(self.paths)
        total_bytes = self._total_bytes()
        self._total_bytes_cached = total_bytes
        log.info(
            "Hashing %d file(s) (%.1f MB) in the background using %d worker(s)",
            total, total_bytes / 1024 / 1024, self.workers,
        )
        start = time.monotonic()

        from_name, to_read = self._split_by_filename_hash()
        bytes_to_read = sum(self._file_size(p) for p in to_read)
        # The ETA should count only what's actually going to be read -
        # including bytes taken from filenames would make it project a
        # rate against work that never happens.
        self._total_bytes_cached = bytes_to_read

        result: Dict[str, str] = dict(from_name)
        stopped_early = False
        if to_read:
            result.update(
                self._run_sequential(to_read, len(from_name), total)
                if self.workers == 1
                else self._run_parallel(to_read, len(from_name), total)
            )
            # _run_sequential/_run_parallel return partial results on stop.
            # Check if we stopped early.
            stopped_early = self._stop_requested

        elapsed = time.monotonic() - start
        mb = bytes_to_read / 1024 / 1024
        log.info(
            "Finished hashing %d/%d file(s) in %.1fs using %d worker(s) - read %.1f MB at %.1f MB/s"
            "%s (compare against a different worker count to see what your storage actually prefers)",
            len(result), total, elapsed, self.workers, mb, (mb / elapsed) if elapsed > 0 else 0.0,
            f", {len(from_name)} taken from filenames without reading" if from_name else "",
        )
        if not stopped_early:
            self.finished_hashing.emit(result)
        # If stopped_early, we emit no signal - the lifecycle disconnects
        # before calling stop(), so a `stopped` signal would never be
        # delivered anyway. See class docstring.

    def _estimate_remaining(self, bytes_done: int, elapsed: float) -> float:
        """Seconds remaining, estimated from BYTES processed rather than
        file count - file sizes here range from well under a megabyte to
        several hundred, so a count-based estimate would be badly wrong
        for most of a mixed batch. Returns -1.0 when there isn't yet
        enough data to say anything meaningful."""
        total_bytes = self._total_bytes_cached
        if (
            total_bytes <= 0
            or bytes_done <= 0
            or elapsed < MIN_SECONDS_BEFORE_ESTIMATING
            or (bytes_done / total_bytes) < MIN_FRACTION_BEFORE_ESTIMATING
        ):
            return -1.0
        rate = bytes_done / elapsed          # bytes/sec, averaged over the whole run so far
        remaining = max(total_bytes - bytes_done, 0)
        return remaining / rate if rate > 0 else -1.0

    def _run_sequential(self, paths: List[str], offset: int, total: int) -> Dict[str, str]:
        result: Dict[str, str] = {}
        start = time.monotonic()
        bytes_done = 0
        for i, path in enumerate(paths):
            if self._stop_requested:
                log.info("File hashing stopped early at %d/%d", offset + i, total)
                break
            bytes_done += self._file_size(path)
            file_hash = hash_file(path)
            if file_hash:
                result[path] = file_hash
            self._emit_progress(offset + i + 1, total, bytes_done, time.monotonic() - start)
        return result

    def _run_parallel(self, paths: List[str], offset: int, total: int) -> Dict[str, str]:
        result: Dict[str, str] = {}
        done_count = offset
        start = time.monotonic()
        bytes_done = 0
        # Bounded pool rather than one thread per file - a batch of
        # thousands of files would otherwise try to open thousands of
        # concurrent reads, which helps nothing and can exhaust handles.
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="file-hash") as executor:
            futures = {executor.submit(hash_file, p): p for p in paths}
            for future in as_completed(futures):
                if self._stop_requested:
                    # Cancels only the not-yet-started ones; already-running
                    # reads finish on their own, which is fine and quick.
                    for f in futures:
                        f.cancel()
                    log.info("File hashing stopped early at %d/%d", done_count, total)
                    break
                path = futures[future]
                try:
                    file_hash = future.result()
                except Exception as exc:
                    log.warning("Could not hash %s: %s", path, exc)
                    file_hash = ""
                if file_hash:
                    result[path] = file_hash
                bytes_done += self._file_size(path)
                done_count += 1
                self._emit_progress(done_count, total, bytes_done, time.monotonic() - start)
        return result
