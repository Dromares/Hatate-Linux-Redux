"""Engine failures that the user has to be TOLD about, not just logged.

Most engine trouble belongs in the log and in the row's error text: a
site was slow, a page didn't parse, a match wasn't found. A missing
optional dependency is different. Nothing is wrong with the request, the
engine simply cannot run at all, and every image it is asked about will
fail the same way for the rest of the session. OBSERVED with Google
Lens: with Playwright absent the engine stood down on the first image
and said so only in the log, so a batch ran to completion looking like
Lens had searched and found nothing.

This is the channel for that: core raises an alert, the GUI shows it.
Kept as a tiny observer registry rather than a Qt signal because the
raising side is plain core code running on a search worker's thread
pool, and core does not import PyQt - see gui/main_window.py for the
subscriber that turns one of these into a message box on the GUI thread.

Each alert carries a `key`, and a key is delivered ONCE per session. A
fallback engine fires once per image, so a batch of four hundred would
otherwise be four hundred identical dialogs stacked in front of a user
who stepped away.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Set

from .applog import get_logger

log = get_logger("engine_alerts")


@dataclass(frozen=True)
class EngineAlert:
    """One thing worth interrupting the user for.

    `key` is what the once-per-session rule is applied to, so it names
    the CONDITION rather than the occurrence - "google-lens:playwright",
    not the image that happened to hit it. `title` and `body` are shown
    as they are; `remedy` is the command to run, kept apart so it can be
    displayed as something copyable rather than buried in a paragraph.
    """

    key: str
    engine: str
    title: str
    body: str
    remedy: str = ""

    def text(self) -> str:
        """The whole thing as one block, for a plain message box."""
        return f"{self.body}\n\n{self.remedy}" if self.remedy else self.body


Listener = Callable[[EngineAlert], None]

_lock = threading.Lock()
_listeners: List[Listener] = []
_raised: Set[str] = set()


def subscribe(listener: Listener) -> None:
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def unsubscribe(listener: Listener) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def raise_alert(alert: EngineAlert) -> bool:
    """Deliver `alert` unless its key has already been delivered.

    Returns whether it was delivered, which is what the tests assert on
    and what a caller can use to decide whether it still needs to log.
    A listener that throws must not take the search down with it, so
    each is called in its own try.
    """
    with _lock:
        if alert.key in _raised:
            return False
        _raised.add(alert.key)
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(alert)
        except Exception as exc:                  # noqa: BLE001 - a dialog is never worth a crash
            log.debug("An engine-alert listener failed (%s)", exc)
    return True


def already_raised(key: str) -> bool:
    with _lock:
        return key in _raised


def reset() -> None:
    """Forget which alerts have been shown.

    Deliberately NOT called when a search starts, unlike the engines'
    stand-down flags. An unanswered robot check is worth offering again
    on the next run; a missing 150MB dependency will not have appeared
    between two clicks of Start Search, and re-raising it there would
    turn one honest warning into a dialog per run. This exists for the
    tests and for a genuine session reset.
    """
    with _lock:
        _raised.clear()
