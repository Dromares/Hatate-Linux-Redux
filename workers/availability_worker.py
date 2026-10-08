from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from PyQt6.QtCore import QThread, pyqtSignal

from core.applog import get_logger
from core.config import Settings
from core.hydrus_import import normalize_url_for_hydrus
from core.models import ImageEntry, MatchCandidate
from core.search_engine import (
    check_url_available, cookies_for_url, has_soft_404_detection, referer_for_candidate,
)

log = get_logger("availability_worker")

MAX_CONCURRENT_CHECKS = 6  # these hit many different hosts, so some concurrency is safe and much
                           # faster than serially waiting on each one - but keep it bounded so a
                           # long candidate list doesn't fire dozens of simultaneous requests


class AvailabilityWorker(QThread):
    """Checks whether each candidate's source URL still exists, so dead
    matches (deleted posts, removed uploads) can be identified rather
    than silently selected and found broken later.

    Only a definite 404/410 counts as gone - see check_url_available for
    why anything ambiguous (timeout, 403, server error) deliberately
    stays "unknown" instead of being reported as unavailable."""

    progress = pyqtSignal(int, int)     # done, total
    finished_checking = pyqtSignal(object, int, int)  # entry, gone_count, checked_count

    def __init__(self, entry: ImageEntry, settings: Settings, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.settings = settings
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def _check_one(self, candidate: MatchCandidate) -> tuple:
        # Which URL to test depends on the site. Normally the actual image
        # file is the more meaningful check, since a post page can still
        # render fine after its image is removed. But for sites that
        # signal deletion via a "this post was deleted" page (Pixiv), the
        # POST PAGE is the only place that error text appears - its old
        # CDN file URL just fails ambiguously (403/timeout), which
        # correctly-but-uselessly reports "unknown".
        page_url = normalize_url_for_hydrus(candidate.url) if candidate.url else None
        if page_url and has_soft_404_detection(page_url):
            target = page_url
        else:
            target = candidate.direct_file_url or candidate.url
        referer = referer_for_candidate(candidate, target)
        # Send the site's login cookies if any are configured. Without
        # them, Sankaku shows account-only and adult posts as a blank
        # page, which is indistinguishable from a deleted one - so a
        # logged-out check would report perfectly good posts as dead and
        # this action would delete them from the list. The search-time
        # sweep already did this; doing it here too means the manual
        # check can't disagree with the automatic one.
        available = check_url_available(
            target, self.settings.search_timeout, referer=referer,
            cookies=cookies_for_url(target, self.settings) if target else None,
            settings=self.settings,
        )
        return candidate, available

    def run(self):
        candidates = list(self.entry.candidates)
        total = len(candidates)
        log.info("Checking availability of %d candidate(s) for %s", total, self.entry.filename)

        done = 0
        gone = 0
        worker_count = min(MAX_CONCURRENT_CHECKS, max(1, total))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="availability") as executor:
            futures = [executor.submit(self._check_one, c) for c in candidates]
            for future in as_completed(futures):
                if self._stop_requested:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    candidate, available = future.result()
                except Exception as exc:
                    log.warning("Availability check raised: %s", exc)
                    done += 1
                    self.progress.emit(done, total)
                    continue
                # Unconditional, unlike the search-time sweep in
                # core/availability.py, which skips a candidate whose
                # verdict is already known. This is the user explicitly
                # asking again, so a saved verdict - however recent - is
                # overwritten with what the site says now.
                candidate.record_availability(available)
                if available is False:
                    gone += 1
                done += 1
                self.progress.emit(done, total)

        log.info(
            "Availability check finished for %s: %d/%d candidate(s) confirmed gone",
            self.entry.filename, gone, done,
        )
        self.finished_checking.emit(self.entry, gone, done)
