"""A background ticker for showing live countdown feedback around a
single blocking call - an HTTP request via `requests` has no native
"percent done" callback, so there's no way to know it's actively working
versus stalled without something like this running alongside it.

Kept Qt-free (plain threading, not QThread) so core/ modules that use it
(search_engine.py, iqdb.py, saucenao.py) don't need to depend on PyQt6 -
the GUI layer just supplies a callback that forwards ticks to a Qt signal.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

OnTick = Callable[[str, float, float], None]  # label, seconds remaining, total seconds


class ProgressTicker:
    """Runs a background thread that calls on_tick(label, remaining,
    total) once per interval while active. Use as a context manager: the
    ticker starts on entry and stops on exit - regardless of whether the
    wrapped call succeeded, failed, or timed out - firing one final
    on_tick("", 0, 0) to clear whatever display is showing it.

    No-ops entirely if on_tick is None, so call sites don't need to
    branch on whether a caller wants countdown feedback."""

    def __init__(self, label: str, timeout: float, on_tick: Optional[OnTick], interval: float = 1.0):
        self.label = label
        self.timeout = timeout
        self.interval = interval
        self.on_tick = on_tick
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self):
        start = time.monotonic()
        while not self._stop_event.wait(self.interval):
            elapsed = time.monotonic() - start
            remaining = max(self.timeout - elapsed, 0.0)
            self.on_tick(self.label, remaining, self.timeout)
            if remaining <= 0:
                break

    def __enter__(self):
        if self.on_tick is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=1.0)
            self.on_tick("", 0.0, 0.0)  # clear whatever display was showing this
        return False
