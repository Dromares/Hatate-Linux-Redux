"""A minimal least-recently-used cache.

Kept deliberately Qt-free so it's testable without a GUI, even though its
only current users hold Qt pixmaps. Wraps OrderedDict rather than doing
anything clever: reads move the key to the most-recent end, and writes
past the cap drop whatever is least recently used.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any


class LRUCache:
    """Dict-like, capped by entry count. Supports the operations the
    callers actually use: get(), subscript read AND write, pop(), len(),
    and `in`. Note both read paths (get and []) count as a use for LRU
    purposes - an entry you looked at is not a candidate for eviction."""

    def __init__(self, max_entries: int):
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self._data: "OrderedDict[Any, Any]" = OrderedDict()
        self.evictions = 0  # purely for logging/diagnostics

    def get(self, key: Any, default: Any = None) -> Any:
        if key not in self._data:
            return default
        self._data.move_to_end(key)  # touched, so it's now most-recently-used
        return self._data[key]

    def __getitem__(self, key: Any) -> Any:
        """Subscript read, raising KeyError on a miss like a dict does.
        Counts as a use for LRU purposes, same as get()."""
        value = self._data[key]      # raises KeyError if absent, matching dict
        self._data.move_to_end(key)
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)  # drop least-recently-used
            self.evictions += 1

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def pop(self, key: Any, default: Any = None) -> Any:
        return self._data.pop(key, default)

    def clear(self) -> None:
        self._data.clear()
