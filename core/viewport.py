"""Pure viewport arithmetic for lazy thumbnail loading.

Kept out of gui/ deliberately: this is the part that actually decides
which rows get work done for them, and it can be exercised properly here
without needing a Qt event loop or a real window.
"""
from __future__ import annotations

from typing import Callable, Container, List, NamedTuple, Optional, Sequence, Tuple


def visible_range_with_buffer(
    first: int, last: int, row_count: int, buffer_rows: int,
) -> Optional[Tuple[int, int]]:
    """Expands a visible row range by a buffer, clamped to the real list.

    `first`/`last` come from Qt's rowAt(), which returns -1 when the
    coordinate falls past the last row. That is a normal, frequent case -
    a list shorter than the window, or one scrolled to the bottom - not
    an error, so it's handled here rather than left to the caller:

    - first < 0  -> treat as the top of the list
    - last  < 0  -> treat as the final row

    The buffer means a short scroll lands on rows that already have
    thumbnails instead of blank ones. Returns None for an empty list.
    """
    if row_count <= 0:
        return None

    if first < 0:
        first = 0
    if last < 0:
        last = row_count - 1

    # Guard against a swapped pair rather than silently returning an
    # empty slice, which would look like "nothing needs loading" and
    # leave rows permanently blank.
    if last < first:
        first, last = last, first

    start = max(0, first - buffer_rows)
    end = min(row_count - 1, last + buffer_rows)
    return start, end


class ThumbnailWork(NamedTuple):
    """What a viewport pass found in the rows it looked at."""
    needed: List          # entries with no thumbnail and none being made
    already_cached: int   # had one already
    in_flight: int        # a worker is making one right now

    @property
    def examined(self) -> int:
        return len(self.needed) + self.already_cached + self.in_flight


def entries_needing_thumbnails(
    candidates: Sequence,
    is_cached: Callable[[int], bool],
    in_flight_ids: Container[int],
) -> ThumbnailWork:
    """Splits the rows in view into work to do and work already done.

    Here for the same reason visible_range_with_buffer is: it decides
    which rows get work done for them, and the two states that must not be
    confused - "already has one" and "one is being made right now" - are
    worth being able to test without a window and a running worker.

    Counting a row that is mid-generation as needing generation is the
    mistake this guards against: the pass would queue a second worker for
    the same entry, and on a scroll that repeats for every debounce tick.

    Entries are keyed by id() rather than by path, matching the caches
    this reads: two rows can share a path (the same file added twice) and
    each still needs its own thumbnail.
    """
    needed, already_cached, in_flight = [], 0, 0
    for entry in candidates:
        eid = id(entry)
        if is_cached(eid):
            already_cached += 1
        elif eid in in_flight_ids:
            in_flight += 1
        else:
            needed.append(entry)
    return ThumbnailWork(needed, already_cached, in_flight)
