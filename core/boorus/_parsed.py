"""Parse a page once, however many hooks read it.

A parser is a set of hooks - parse, parse_file_url, parse_dimensions,
... - each handed the same raw body by fetch_page_info. Each used to
parse it again: rule34us built four BeautifulSoup trees per page, and
pawchive decoded the same JSON five times. These return the one parse,
cached by the body itself.

The cache is keyed by the body string. fetch_page_info decodes the
response once and passes that same object to every hook, so a lookup is
an identity check, not a comparison of two 40KB strings.

What comes back is SHARED between hooks - a parser must read it, never
change it. None of them do: they select, get_text and .get, and build
their own results.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from bs4 import BeautifulSoup

from ..lru_cache import LRUCache

# A page's hooks run back to back, so a handful covers it - including a
# couple of candidates' details being fetched at the same time.
CACHE_ENTRIES = 8

_soups = LRUCache(CACHE_ENTRIES)
_json = LRUCache(CACHE_ENTRIES)
_lock = threading.Lock()


class _NotJson:
    """Remembers that a body failed to decode, so the next hook doesn't retry."""

    def __init__(self, error: ValueError):
        self.error = error


def soup_of(html: str) -> BeautifulSoup:
    """The lxml tree of this page - built once, shared by every hook."""
    with _lock:
        cached = _soups.get(html)
    if cached is None:
        cached = BeautifulSoup(html, "lxml")
        with _lock:
            _soups[html] = cached
    return cached


def json_of(body: str) -> Any:
    """json.loads(body), once per body. Raises ValueError as json.loads
    would, every time it is asked about a body that isn't JSON."""
    with _lock:
        cached = _json.get(body)
    if cached is None:
        try:
            cached = json.loads(body)
        except ValueError as exc:
            cached = _NotJson(exc)
        with _lock:
            _json[body] = cached
    if isinstance(cached, _NotJson):
        # The same type json.loads raised - parsers catch JSONDecodeError
        # specifically, not only ValueError.
        err = cached.error
        if isinstance(err, json.JSONDecodeError):
            raise json.JSONDecodeError(err.msg, err.doc, err.pos)
        raise ValueError(str(err))
    return cached
