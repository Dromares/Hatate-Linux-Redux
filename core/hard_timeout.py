"""Enforces a genuine wall-clock timeout around a blocking call.

`requests`'s own `timeout` parameter is NOT a true "give up after N
seconds no matter what" limit - by default it's a per-read (time between
bytes received) timeout that RESETS every time the server sends even a
little data. A slow or misbehaving server that trickles data can keep a
request alive far longer than the configured timeout would suggest, even
though a wall-clock countdown tracking the same elapsed time correctly
reaches zero right on schedule - the countdown isn't wrong, `requests`'s
timeout just isn't the kind of timeout it looks like.

This runs the call in a background thread and enforces a real deadline
regardless of what the call is doing internally on the network level.

The underlying thread can't be forcibly killed once started (a Python
limitation, not fixable from here) - if the deadline is hit, this
function raises immediately anyway and abandons that thread to finish
(or hang) on its own; its eventual result is simply discarded. The
thread is created with daemon=True specifically so an abandoned,
permanently-hung one (exactly what this guards against) can never block
the application from exiting - a plain ThreadPoolExecutor's worker
threads are NOT daemons, and Python registers an atexit hook that waits
for all submitted work to finish before the interpreter can shut down,
which would make a single truly-hung request freeze the whole app on
close, not just this one operation.
"""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class HardTimeoutError(Exception):
    """A call didn't finish within its hard wall-clock deadline."""


def run_with_hard_timeout(func: Callable[[], T], timeout: float) -> T:
    """Runs func() with a genuine wall-clock deadline. Raises
    HardTimeoutError if it doesn't complete in time, regardless of
    whether the call itself "thinks" it's still within its own timeout
    (e.g. a requests call being kept alive by trickling data)."""
    outcome: dict = {}
    done = threading.Event()

    def _target():
        try:
            outcome["value"] = func()
        except BaseException as exc:  # deliberately broad - re-raised on the caller's side below
            outcome["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()

    if not done.wait(timeout=timeout):
        raise HardTimeoutError(
            f"Timed out after {timeout:.0f}s (the server may be trickling data, "
            "keeping the connection alive without actually finishing)"
        )

    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]
