"""How much longer a search run has to go.

A search is paced at 45-75 seconds an image by default, so a library of
any size is a run measured in days, and the only thing the status bar
said about it was a progress bar with no numbers on it. This turns the
progress signal into "how much longer", which is the question someone
walking away from an overnight run is actually asking.

Two details decide how the estimate is calculated, and both come from
how the worker emits progress.

The worker emits progress *after* an image's search and *before* that
image's rate-limit wait. So elapsed-since-start divided by images-done
is systematically short by one wait: after the first image only the
search itself has happened, and the estimate would converge on the true
pace from below, reading far too optimistic exactly when someone is
first looking at it. Measuring the *interval between* progress signals
avoids that entirely - one interval is one whole cycle, wait included -
so that is what this averages.

The average is taken over the whole run rather than a trailing window.
Per-image cost here is bimodal: an image served from the search cache
skips the rate-limit wait altogether and costs almost nothing, and
cache hits arrive in clusters (a re-imported folder), not spread evenly.
A trailing window sitting inside one of those clusters would report
minutes remaining for a run with days left, then leap back. A whole-run
mean moves by 1/n per sample, so a cluster pulls it down in proportion
to how much of the run it actually was - which is the honest answer.
"""
from __future__ import annotations

import time
from typing import Optional

# Two intervals - three progress signals - before saying anything. One
# interval is a single sample of a quantity that varies by design (the
# delay is randomised, and a cache hit skips it), and quoting a finish
# date off one sample would be a guess wearing a number's clothes.
MIN_INTERVALS_BEFORE_ESTIMATING = 2


class RunEstimate:
    """Tracks a run's pace and answers how much of it is left.

    Reused across runs: `reset()` at every start, `record()` on every
    progress signal, `seconds_remaining()` to display.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forgets the previous run. Called at each start, since a new
        run's pace has nothing to do with the last one's - the delay
        setting may have changed, and a re-search of cached rows runs at
        a completely different speed to a fresh batch."""
        self._first_at: Optional[float] = None
        self._first_done: int = 0
        self._last_at: float = 0.0
        self._done: int = 0
        self._total: int = 0

    def record(self, done: int, total: int, now: Optional[float] = None) -> None:
        """Notes one progress signal.

        `total` is re-read every time on purpose: it grows once mid-run,
        when images that failed on a network fault are queued for their
        one retry.
        """
        moment = time.monotonic() if now is None else now
        self._done = done
        self._total = total
        if self._first_at is None:
            # The baseline is this signal, not the run's start: the time
            # before it is one search with no wait attached, which is not
            # a sample of anything the rest of the run will repeat.
            self._first_at = moment
            self._first_done = done
        self._last_at = moment

    @property
    def intervals(self) -> int:
        """Completed image-to-image cycles observed so far."""
        return max(self._done - self._first_done, 0)

    def seconds_per_image(self) -> float:
        """Mean seconds per image over the run so far, or -1.0 when
        there isn't enough of the run yet to say."""
        if self._first_at is None or self.intervals < MIN_INTERVALS_BEFORE_ESTIMATING:
            return -1.0
        elapsed = self._last_at - self._first_at
        if elapsed <= 0:
            return -1.0
        return elapsed / self.intervals

    def seconds_remaining(self) -> float:
        """Estimated seconds to the end of the run, or -1.0 when unknown.

        Unknown is a real answer here and is reported as one rather than
        being papered over with the configured delay: the delay is only
        part of the per-image cost, and a made-up number that later
        halves is worse than no number at all.
        """
        pace = self.seconds_per_image()
        if pace < 0:
            return -1.0
        remaining = max(self._total - self._done, 0)
        return remaining * pace
