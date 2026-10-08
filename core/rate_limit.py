"""How long to actually wait between searches.

The delay between images exists to be polite to IQDB/SauceNAO, so it
should be measured from the moment their request finished - not from the
moment everything else did. A single image's search also fetches the
matched booru page, sweeps candidate availability, and downloads a
preview, and auto-import may then poll Hydrus for up to a minute. None
of that touches the search engines, yet all of it happens after the
engine request and before the next one.

Counting that time toward the gap keeps the engine-to-engine interval
exactly as long as it was configured to be, while not charging the user
a second time for seconds that have already passed. It is the same
reasoning the cache path already uses - a cached result skips the wait
entirely because no engine request was made - applied to the partial
case where a request WAS made but a lot of unrelated work followed it.

Kept out of workers/ deliberately: this is the part that decides how
long a batch takes, and it can be exercised properly here without a Qt
event loop.
"""
from __future__ import annotations

from typing import Optional


def remaining_delay(
    delay: float, engines_finished_at: Optional[float], now: float,
) -> float:
    """Seconds still to wait, having credited time already elapsed since
    the search engines were last called.

    `engines_finished_at` and `now` are monotonic timestamps.
    `engines_finished_at` is None when no engine request was made, or
    when the search failed before one completed - in that case the full
    delay applies, which is the conservative choice: it can only ever
    make the gap longer than strictly necessary, never shorter.
    """
    if delay <= 0:
        return 0.0
    if engines_finished_at is None:
        return delay

    elapsed = now - engines_finished_at
    # A negative reading means the two timestamps didn't come from the
    # same monotonic clock, which is a caller bug rather than a real
    # measurement. Fall back to the full delay instead of crediting
    # nonsense - and never extend the wait beyond it.
    if elapsed < 0:
        return delay

    return max(0.0, delay - elapsed)


# ----------------------------------------------------------------------
# Per-engine pacing
# ----------------------------------------------------------------------
# One delay for the whole batch treats every search engine as if it were
# the same server. They are not: IQDB, SauceNAO, ascii2d, trace.moe,
# IQDB 3D, Google Images and Google Lens are separate hosts with different
# tolerances, and the single gap has to be set for whichever is
# strictest - so a run that never touches that host waits at its pace
# anyway.
#
# The clearest case is the one the README recommends: once SauceNAO's
# daily allowance is spent, "IQDB only" keeps working. That run queries
# one host, and there is no reason for it to be paced by the limits of a
# service it is not calling.
#
# Every engine defaults to the global delay, so nothing changes until an
# override is set. Overrides can only be applied where they are known to
# be safe, which is why none are guessed here.

def engine_interval(
    engine: str, overrides: dict, global_min: float, global_max: float,
) -> tuple:
    """The (min, max) seconds to leave between two requests to `engine`.

    Falls back to the global delay whenever this engine has no override,
    or has one that is malformed - a hand-edited config should degrade to
    the safe setting rather than to no delay at all.
    """
    from .engines import LOCAL_ENGINES
    if engine in LOCAL_ENGINES:
        return 0.0, 0.0            # no request is made, so there is nothing to pace
    pair = (overrides or {}).get(engine)
    if pair is None:
        return global_min, global_max
    try:
        low, high = float(pair[0]), float(pair[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return global_min, global_max
    if low < 0 or high < 0:
        return global_min, global_max
    return (low, high) if low <= high else (high, low)


class EngineRateLimiter:
    """Remembers when each engine was last queried.

    Thread-safe because a wave runs its engines in parallel threads, and
    each records its own finish time.
    """

    def __init__(self):
        import threading
        self._last = {}
        self._lock = threading.Lock()

    def record(self, engine: str, when: float) -> None:
        with self._lock:
            self._last[engine] = when

    def last_seen(self, engine: str):
        with self._lock:
            return self._last.get(engine)

    def wait_needed(self, engines, intervals: dict, now: float) -> float:
        """Seconds to wait before the given engines may all be queried.

        The maximum of each engine's own remaining gap, because they are
        about to be queried together - the wave starts when the slowest
        of them is ready. An engine never queried before imposes no wait.

        Deliberately the max and not the min: taking the min would start
        the wave as soon as ANY engine was ready and hit the others
        early, which is the one mistake this must never make.
        """
        with self._lock:
            last = dict(self._last)
        worst = 0.0
        for engine in engines:
            seen = last.get(engine)
            if seen is None:
                continue  # never queried - nothing to wait for
            elapsed = now - seen
            if elapsed < 0:
                # Mismatched clocks: fall back to this engine's full
                # interval rather than crediting a nonsense reading.
                elapsed = 0.0
            remaining = intervals.get(engine, 0.0) - elapsed
            if remaining > worst:
                worst = remaining
        return max(0.0, worst)
