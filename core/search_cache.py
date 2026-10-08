"""Caches search results (candidates, status, tags, etc.) keyed by the
local file's SHA256 content hash, so re-adding an image you've already
searched - a re-import from Hydrus, a reorganized folder, a duplicate
copy under a different filename - doesn't burn another rate-limited
IQDB/SauceNAO request for a result you already have.

One small JSON file per hash under ~/.config/hatate-linux/search_cache/,
rather than one big file, so a single corrupted entry can't take down the
whole cache and old entries can be inspected/removed individually.

Explicitly NOT cached: ERROR results (network hiccups, rate limits -
those should just be retried normally, not "remembered" as a fixed
outcome). NOT_FOUND results ARE cached, since IQDB/SauceNAO won't have a
different answer for the same exact file tomorrow - "Re-search" is the
deliberate bypass for when the user wants a fresh look anyway (e.g. they
know a source has since been posted).
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .applog import get_logger
from .models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
from .paths import SEARCH_CACHE_DIR

log = get_logger("search_cache")

# Bumped whenever a cached candidate could be MISSING information that a
# current search would have produced. Entries below this aren't thrown
# away - the expensive part of a search is the rate-limited IQDB/SauceNAO
# call, and the match URL it produced is still perfectly good. What gets
# redone is the cheap part: re-reading the booru page to fill in details
# that either didn't exist when the entry was written, or were wrong.
#
# v2: adds direct_file_url / preview_url / real dimensions, and Danbooru
#     moved to its JSON API. Entries written before this restore with no
#     preview or full-resolution URL, yet were flagged as fully fetched -
#     so every one of them silently fell back to the search engine's own
#     cached thumbnail, on every site, permanently.
CACHE_FORMAT_VERSION = 2


def _tag_version_for(url: str) -> int:
    """What a parser says its CURRENT tag output is worth comparing to.

    CACHE_FORMAT_VERSION above is all-or-nothing: bumping it re-reads the
    booru page for every entry on disk, whichever site it came from. That
    is the right hammer for a format change, and much too big a one for
    "MangaDex stopped emitting series/volume/chapter/page" - which is a
    real change to what a cached entry means, but only for MangaDex.
    Without a per-parser signal the choice was invalidating thousands of
    unrelated entries or leaving the stale ones in place, and leaving them
    is what actually happened: 106 entries kept serving 348 tags the
    parser no longer produces.

    So a parser declares TAG_VERSION and bumps it whenever the tags it
    returns change. Absent means 0, so every parser that has never changed
    needs nothing and behaves exactly as before.
    """
    from .boorus import find_parser  # local: avoids importing the parsers at module load
    try:
        parser = find_parser(url or "")
    except Exception:  # a parser registry problem must not cost the cache
        return 0
    return int(getattr(parser, "TAG_VERSION", 0) or 0) if parser else 0


def _cache_path(file_hash: str):
    return SEARCH_CACHE_DIR / f"{file_hash}.json"


def _tag_to_dict(tag: Tag) -> dict:
    return {"name": tag.name, "namespace": tag.namespace}


def _tag_from_dict(d: dict, source: TagSource) -> Tag:
    return Tag(name=d["name"], source=source, namespace=d.get("namespace"))


def _candidate_to_dict(c: MatchCandidate) -> dict:
    return {
        "url": c.url,
        "source_name": c.source_name,
        "thumb_url": c.thumb_url,
        "similarity": c.similarity,
        # Whether the number above is a real comparison of the two pictures
        # or the engine's own ordinal position. Two things depend on it
        # surviving: the display surfaces cannot say which numbers are real
        # without it, and core/similarity_check.py re-measures anything it
        # reads as unmeasured - so dropping it made every restart
        # re-download thumbnails to redo work already done.
        "similarity_measured": c.similarity_measured,
        "width": c.width,
        "height": c.height,
        "engine": c.engine,
        # Copied before iterating: a session autosave serializes these off
        # the GUI thread while a search worker may still be appending to
        # them, and iterating the live list can raise mid-write.
        "engine_tags": [_tag_to_dict(t) for t in list(c.engine_tags or ())],
        "booru_tags": [_tag_to_dict(t) for t in list(c.booru_tags or ())],
        "direct_file_url": c.direct_file_url,
        "preview_url": c.preview_url,
        "remote_format": c.remote_format,
        "remote_size_bytes": c.remote_size_bytes,
        # Kept so a restored match still explains its empty tag list,
        # rather than going back to a silent blank after a restart.
        "incomplete_reason": c.incomplete_reason,
        # Whose tags these actually are, when they came from another match.
        "tags_borrowed_from": c.tags_borrowed_from,
        # Is the source page still there? A HEAD per candidate, and every
        # launch bought the same answer again because it was dropped here.
        # Only a DEFINITE verdict means anything on the way back in: a
        # stored null is a record of having not looked, and
        # _restore_availability reads it the same as the key being absent.
        "remote_available": c.remote_available,
        "availability_checked_at": c.availability_checked_at,
        # The other half of the same verdict. A gated post (Danbooru Gold)
        # is not a deleted one, and this is the only thing that says
        # which - so a restored match that went for being restricted can
        # still say so instead of reading as a bare dead link.
        "restricted": c.restricted,
        # Which version of its parser's tag output the booru_tags above
        # came from, so a later change to that parser can be detected
        # without invalidating every other site's entries.
        "tag_version": _tag_version_for(c.url),
        # thumb_bytes is deliberately NOT cached - binary image data would
        # bloat every cache file; it's cheap to re-download on demand and
        # doesn't count against any rate limit.
    }


# How long a saved availability verdict stands before the source URL is
# checked again. It has to expire, in BOTH directions:
#
#   * A 404 is a claim about one post at one moment, like the NOT_FOUND
#     caching below. Sites do restore deleted posts, and a permanent
#     stored 404 would keep dropping a match that came back.
#   * "Alive" rots the same way. Before this was saved at all, every
#     launch re-swept and so noticed a link that had since died; keeping
#     a True forever would mean nothing ever noticed again. That would be
#     a regression bought with the saving, which is not a trade worth
#     making.
#
# 30 days because the sweep is one HEAD per candidate: long enough that
# the restarts this was written for are free, short enough that a verdict
# is never wildly out of date.
AVAILABILITY_VERDICT_TTL_DAYS = 30.0


def _restore_availability(d: dict) -> tuple[Optional[bool], Optional[float]]:
    """Reads back a saved availability verdict, or unknown.

    Tri-state, and the middle state is the one that needs defending:
    `None` means nobody has established anything, and everything
    downstream (core/ranking.py, ImageEntry.drop_unavailable_candidates,
    core/tag_borrowing.py) is written to treat only an explicit False as
    gone. Coercing a missing key with bool() would turn "never checked"
    into "confirmed alive" - so the verdict is accepted only when it is
    genuinely a bool, which rejects a stored null and absence alike.

    A verdict with no usable date is discarded rather than kept
    undated: an unbounded verdict is exactly what the TTL exists to
    prevent, and unknown costs one HEAD to settle.
    """
    verdict = d.get("remote_available")
    if not isinstance(verdict, bool):
        return None, None
    checked_at = d.get("availability_checked_at")
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return None, None
    if (time.time() - checked_at) > (AVAILABILITY_VERDICT_TTL_DAYS * 86400):
        return None, None
    return verdict, float(checked_at)


def _candidate_from_dict(d: dict, format_version: int = 0) -> MatchCandidate:
    # A parser whose tag output has changed since this was written must
    # not be trusted to have produced today's tags, so the booru page gets
    # re-read - the cheap half of a search. The rate-limited half, the
    # match URL itself, is still perfectly good and is kept.
    tags_current = int(d.get("tag_version", 0) or 0) == _tag_version_for(d.get("url") or "")
    fetched = format_version >= CACHE_FORMAT_VERSION
    remote_available, availability_checked_at = _restore_availability(d)
    return MatchCandidate(
        url=d["url"],
        source_name=d.get("source_name"),
        thumb_url=d.get("thumb_url"),
        similarity=d.get("similarity", 0.0),
        # Absent in anything written before this was stored, which reads as
        # unmeasured - correct, and the honest reading: such an entry may
        # hold either kind of number and there is no way to tell now.
        similarity_measured=bool(d.get("similarity_measured", False)),
        width=d.get("width"),
        height=d.get("height"),
        engine=d.get("engine", "IQDB"),
        engine_tags=[_tag_from_dict(t, TagSource.SEARCH_ENGINE) for t in d.get("engine_tags", [])],
        booru_tags=[_tag_from_dict(t, TagSource.BOORU) for t in d.get("booru_tags", [])],
        direct_file_url=d.get("direct_file_url"),
        preview_url=d.get("preview_url"),
        remote_format=d.get("remote_format"),
        remote_size_bytes=d.get("remote_size_bytes"),
        incomplete_reason=d.get("incomplete_reason"),
        tags_borrowed_from=d.get("tags_borrowed_from"),
        # Tri-state, and deliberately NOT bool(...) - see
        # _restore_availability, which is where absence, a stored null and
        # an expired verdict all become "unknown" rather than "gone".
        remote_available=remote_available,
        availability_checked_at=availability_checked_at,
        restricted=d.get("restricted"),
        # Only claim these are "already fetched" for entries written by
        # the CURRENT format. An older entry may be missing fields that a
        # search today would fill in, and marking it fetched would make
        # that permanent - fetch_candidate_details() would skip it
        # forever, and nothing would ever notice.
        booru_tags_fetched=(fetched and tags_current),
        # Deliberately NOT gated on tags_current: a parser changing which
        # TAGS it emits says nothing about the dimensions, format or size
        # it reported, and re-doing those would spend a HEAD per candidate
        # to arrive at the same answer.
        remote_info_fetched=fetched,
    )


# Only "not found" results are aged out, and the asymmetry is deliberate:
#
#   * A NOT_FOUND is a claim about the whole internet at one moment. Sites
#     index new work constantly, so "nothing matched in March" says very
#     little about today - and once cached it is never re-checked, so the
#     stale negative is permanent.
#   * A found match doesn't rot the same way. The URL might die, but that
#     is what the dead-link detection handles, and re-running a search
#     that already succeeded just spends rate-limited quota to arrive at
#     the same answer.
EXPIRING_STATUSES = ("not_found",)


def _is_expired(cached: dict, ttl_days: float) -> bool:
    if ttl_days <= 0:
        return False  # 0 (or negative) disables expiry entirely
    if cached.get("status") not in EXPIRING_STATUSES:
        return False
    cached_at = cached.get("cached_at")
    if not isinstance(cached_at, (int, float)):
        # Written before timestamps were recorded, or corrupt - treat it
        # as expired rather than keeping it forever with no way to tell.
        return True
    return (time.time() - cached_at) > (ttl_days * 86400)


def load_cached_result(file_hash: str, ttl_days: float = 0.0) -> Optional[dict]:
    """Returns the raw cached dict for this hash, or None if there's no
    usable entry - missing, unreadable, or expired (all treated the same
    as a miss; never raises).

    An expired entry is deleted on the way out, so the cache prunes
    itself as it's used rather than needing a separate sweep."""
    path = _cache_path(file_hash)
    if not path.exists():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Could not read search cache entry %s: %s (treating as a miss)", path, exc)
        return None

    if isinstance(cached, dict) and _is_expired(cached, ttl_days):
        age_days = (time.time() - cached.get("cached_at", 0)) / 86400
        log.info("Search cache entry %s is stale (%.0f days old, status=%s) - re-searching",
                 file_hash[:12], age_days, cached.get("status"))
        try:
            path.unlink()
        except OSError:
            pass
        return None

    return cached


def apply_cached_result(entry: ImageEntry, cached: dict) -> bool:
    """Populates an ImageEntry from a cached result dict. Returns True on
    success. Mutates the entry in place, mirroring what a real search
    would have set."""
    try:
        status_value = cached.get("status")
        status = MatchStatus(status_value) if status_value else MatchStatus.NOT_FOUND
        format_version = int(cached.get("format_version", 0) or 0)
        candidates = [_candidate_from_dict(c, format_version)
                      for c in cached.get("candidates", [])]
    except (KeyError, ValueError) as exc:
        log.warning("Malformed search cache entry for this image, ignoring: %s", exc)
        return False

    if candidates and format_version < CACHE_FORMAT_VERSION:
        log.info(
            "Search cache entry predates format v%d - keeping the match (that's the "
            "rate-limited part) but re-reading the booru page to fill in the "
            "full-resolution and preview URLs it lacks",
            CACHE_FORMAT_VERSION,
        )

    entry.candidates = candidates
    entry.status = status
    entry.error_message = cached.get("error_message")

    if candidates:
        selected_index = cached.get("selected_candidate_index", 0)
        if not (0 <= selected_index < len(candidates)):
            selected_index = 0
        entry.select_candidate(selected_index)

    return True


def save_cached_result(file_hash: str, entry: ImageEntry):
    """Saves an entry's current search result (after a real search) to the
    cache. No-ops for ERROR results - those shouldn't be "remembered" as a
    fixed outcome, just retried normally next time - and for results found
    without SauceNAO, which are weaker than a full search would have given
    and must not be handed back tomorrow as if they were the real
    answer."""
    if entry.status == MatchStatus.ERROR:
        return
    if getattr(entry, "searched_without_saucenao", False):
        log.debug("Not caching %s - searched without SauceNAO, so the result is provisional",
                  entry.filename)
        return

    data = {
        "format_version": CACHE_FORMAT_VERSION,
        "cached_at": time.time(),
        "status": entry.status.value,
        "error_message": entry.error_message,
        "selected_candidate_index": entry.selected_candidate_index,
        "candidates": [_candidate_to_dict(c) for c in entry.candidates],
    }

    try:
        SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(file_hash).write_text(json.dumps(data), encoding="utf-8")
        log.debug("Saved search cache entry for %s (%d candidate(s))", file_hash[:12], len(entry.candidates))
    except OSError as exc:
        log.warning("Could not write search cache entry: %s", exc)


def get_cache_size_bytes() -> int:
    """Total on-disk size of all cached search results, in bytes. 0 if the
    cache directory doesn't exist yet (nothing cached so far)."""
    if not SEARCH_CACHE_DIR.exists():
        return 0
    total = 0
    for path in SEARCH_CACHE_DIR.glob("*.json"):
        try:
            total += path.stat().st_size
        except OSError:
            pass  # file vanished between the glob and the stat, or unreadable - skip it
    return total


def drop_cached_result(file_hash: str) -> bool:
    """Forgets one file's cached search result. Returns whether there was
    one to forget.

    Resetting an entry has to do this too. The cache is keyed on file
    contents, so a reset entry that kept its cached result would be
    handed the very match it just discarded the next time a search ran -
    the reset would look like it had simply not worked.
    """
    if not file_hash:
        return False
    path = _cache_path(file_hash)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not delete search cache entry %s: %s", path, exc)
        return False
    log.debug("Dropped cached search result for %s", file_hash)
    return True


def clear_cache() -> int:
    """Deletes every cached search result. Returns how many were removed."""
    if not SEARCH_CACHE_DIR.exists():
        return 0
    removed = 0
    for path in SEARCH_CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not delete search cache entry %s: %s", path, exc)
    log.info("Cleared %d search cache entr%s", removed, "y" if removed == 1 else "ies")
    return removed
