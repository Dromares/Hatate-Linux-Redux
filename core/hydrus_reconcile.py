"""Settling what "Queued" actually means, by asking Hydrus.

Sending a URL to Hydrus's own downloader is asynchronous: POST
/add_urls/add_url says only that Hydrus ACCEPTED the URL. The import
happens afterwards, and core/hydrus_import_poll.py watches for it - but
that poll gives up after a minute, because it runs inline during a batch
and cannot hold the run open indefinitely.

When it gives up, the entry stays Queued. Nothing ever asks again. On a
real library that is not a rare edge: a session here held 162 entries sent
to Hydrus and NONE confirmed, because every one of them outlived its poll.
The Sent column is what lets a part-finished batch be picked up later, so
a column that says Queued forever is the feature quietly not working.

The state is knowable, though - it just needs asking a second time. The
file is identified by its SHA256, which every entry already carries from
duplicate detection, so this asks by hash rather than by URL. That matters
because the URL is often gone by now (an entry whose result was reset
keeps sent_to_hydrus, since whether Hydrus holds the file is a fact about
the library rather than about this search) while the hash is not.

No Qt here, and no client either: reconcile() takes the lookup as a
callable so the decision can be tested without a Hydrus to talk to.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, NamedTuple, Optional

from .applog import get_logger
from .models import ImageEntry

log = get_logger("hydrus_reconcile")

# Hydrus states that mean it resolved this file - it holds it, or it held
# it and the user has since deleted it. Both settle the question this is
# asking, which is whether the SEND worked, not whether the file is still
# there. core/hydrus_import_poll.py takes the same view of its own status
# 3 ("previously deleted"): either way it is no longer in progress.
RESOLVED_STATES = frozenset({"present", "trashed", "deleted"})

# Hashes per request. Hydrus takes them in one query string, so this is
# about keeping that URL a sane length rather than about Hydrus's limits.
# 256 keeps a 25,000-entry library to a hundred requests.
BATCH_SIZE = 256


class ReconcileResult(NamedTuple):
    confirmed: List[ImageEntry]    # Hydrus has resolved these; now marked confirmed
    unresolved: List[ImageEntry]   # Hydrus has never seen these
    checked: int                   # how many were asked about

    @property
    def summary(self) -> str:
        if not self.checked:
            return "Nothing was waiting on Hydrus"
        parts = [f"confirmed {len(self.confirmed)} of {self.checked}"]
        if self.unresolved:
            parts.append(f"{len(self.unresolved)} still unknown to Hydrus")
        return "; ".join(parts)


def entries_awaiting_confirmation(entries: Iterable[ImageEntry]) -> List[ImageEntry]:
    """Entries sent to Hydrus that it has never acknowledged holding.

    A hash is required, not optional: it is the only handle on the file
    here, and an entry without one cannot be asked about. Those are left
    alone rather than guessed at.
    """
    return [
        e for e in entries
        if e.sent_to_hydrus and not e.hydrus_import_confirmed and e.hydrus_hash
    ]


def reconcile(
    entries: Iterable[ImageEntry],
    states_for: Callable[[List[str]], Dict[str, str]],
    batch_size: int = BATCH_SIZE,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> ReconcileResult:
    """Asks Hydrus about every entry still waiting, and confirms those it
    has resolved.

    `states_for` maps hashes to Hydrus's reading of them - normally
    HydrusClient.deletion_states. It is passed in rather than a client so
    this is testable without a Hydrus.

    Only ever turns hydrus_import_confirmed ON. A hash Hydrus does not
    know may simply not have finished downloading yet, and un-confirming
    something already confirmed would lose information that was correct
    when it was recorded.
    """
    waiting = entries_awaiting_confirmation(entries)
    if not waiting:
        return ReconcileResult([], [], 0)

    by_hash: Dict[str, List[ImageEntry]] = {}
    for entry in waiting:
        # Two rows can share a hash (the same file added twice), and both
        # deserve the answer. The hash is never None here -
        # entries_awaiting_confirmation requires one - but it is Optional
        # on the entry, so this states that rather than assuming it.
        file_hash = entry.hydrus_hash
        if not file_hash:
            continue
        by_hash.setdefault(file_hash, []).append(entry)

    hashes = list(by_hash)
    confirmed: List[ImageEntry] = []
    unresolved: List[ImageEntry] = []
    checked = 0

    for start in range(0, len(hashes), batch_size):
        if should_stop is not None and should_stop():
            log.info("Reconcile stopped after %d of %d hash(es)", checked, len(hashes))
            break
        chunk = hashes[start:start + batch_size]
        states = states_for(chunk) or {}
        for file_hash in chunk:
            state = states.get(file_hash, "unknown")
            for entry in by_hash[file_hash]:
                checked += 1
                if state in RESOLVED_STATES:
                    entry.hydrus_import_confirmed = True
                    confirmed.append(entry)
                else:
                    unresolved.append(entry)
        if on_progress is not None:
            on_progress(min(start + batch_size, len(hashes)), len(hashes))

    log.info(
        "Reconciled %d entr%s against Hydrus: %d confirmed, %d still unknown",
        checked, "y" if checked == 1 else "ies", len(confirmed), len(unresolved),
    )
    return ReconcileResult(confirmed, unresolved, checked)
