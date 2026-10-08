"""Running the search engines for one image: which ones, in what waves,
and collecting what each returns into candidates and errors.

Split out of core/search_engine.py; everything here is re-exported there.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse



from . import (
    ascii2d, google_images, google_lens, iqdb, pawchive_index, pawchive_lookup, saucenao,
    tracemoe, yandex,
)
from .applog import get_logger
from .config import Settings
from . import engines as engine_ids
from .models import ImageEntry, MatchCandidate, MatchStatus, Tag
from .progress_ticker import OnTick
from .sites import canonicalize_url, classify_site
from .tag_rules import apply_namespace_remap, apply_tag_blacklist

from .availability import url_is_structurally_dead

log = get_logger("search")


# If an engine call takes this long and still comes back with zero
# results, don't trust it enough to cache as a confident NOT_FOUND - a
# healthy IQDB/SauceNAO response normally takes a few seconds. A response
# this slow is more likely to be a degraded/partial one than a genuine
# "confirmed nothing" - even with search_timeout capping how long a
# request can run, this is a second line of defense in case the timeout
# doesn't fire for some reason (e.g. a slow initial connection phase, an
# intermediate proxy, or anything else outside our control).
SLOW_SEARCH_THRESHOLD_SECONDS = 45.0


def _fallback_is_unnecessary(settings: Settings, waves, found_by_engine, candidates):
    """Whether the fallback wave can be skipped, and why - for the log.

    Two rules, because the second one is opt-in and must not change what
    anyone's existing config already does:

    A result also has to be one the user would KEEP. The site filter runs
    after every engine has finished, so a match on a site they unticked
    used to satisfy the threshold and suppress the fallback, and was then
    thrown away - leaving the image with nothing and the fallback engines
    never asked. Observed on three searches in one run.

    An ordinal score cannot satisfy the threshold, but a MEASURED one
    can, whichever engine found it - see measure_ordinal_similarities.

      * With fallback_below_similarity set, a result only counts if it
        is at least that similar. A 42% "maybe" is exactly the case the
        setting exists for, and it brings the remaining engines in.
      * With it at 0 (the default), the rule is the original one: the
        PRIMARY engine having found anything at all is enough. Note
        that is deliberately the primary specifically, not any engine -
        an extra engine finding something has never suppressed the
        fallback, and changing that quietly would alter results for
        people who never asked for this feature.
    """
    threshold = settings.fallback_below_similarity
    if threshold > 0:
        # Only engines that MEASURE similarity can satisfy a similarity
        # threshold. ascii2d and the two Google engines score ordinally -
        # a position in a list dressed up as a percentage - and Google
        # Lens's first result is always 80%, which cleared a 75%
        # threshold on every image it ran and suppressed the fallback
        # even when IQDB and SauceNAO had found nothing.
        measured = [c for c in candidates
                    if (c.similarity_measured or engine_ids.reports_real_similarity(c.engine))
                    and classify_site(c.source_name, c.url) in settings.enabled_sites]
        best = max((c.similarity or 0.0 for c in measured), default=0.0)
        if best >= threshold:
            return True, f"best match is {best:.0f}%, at or above the {threshold:.0f}% threshold"
        ignored = len(candidates) - len(measured)
        note = f" ({ignored} result(s) from engines that don't measure similarity)" if ignored else ""
        return False, f"best measured match is {best:.0f}%, under the {threshold:.0f}% threshold{note}"

    primary = waves[0][0]
    if found_by_engine.get(primary):
        return True, f"the primary engine ({primary}) found results"
    return False, f"the primary engine ({primary}) found nothing"


def planned_engines(settings: Settings, override_engine: Optional[str] = None,
                    skip_saucenao: bool = False) -> List[str]:
    """Every engine the next search could query, in no particular order.

    Used to pace requests per host: the caller needs to know which hosts
    are about to be hit before it hits them. Deliberately includes the
    conditional fallback wave, which only runs when the primary finds
    nothing - counting it always means occasionally waiting for a host
    that turns out not to be queried, which costs a little time. Leaving
    it out would mean occasionally querying a host early, which costs a
    ban.
    """
    seen: List[str] = []
    for wave in _engine_waves(settings, override_engine, skip_saucenao):
        for engine in wave:
            if engine not in seen:
                seen.append(engine)
    return seen


def _engine_waves(settings: Settings, override_engine: Optional[str],
                  skip_saucenao: bool = False) -> List[List[str]]:
    """Which engines to run, grouped into parallel waves.

    A forced engine (right-click > IQDB only, etc.) is a single wave of
    one, ignoring the configured order and the extra-engine toggles -
    that's the point of "only".

    Otherwise wave 1 is the primary engine plus every enabled extra
    engine (ascii2d / trace.moe / IQDB 3D / Google Images / Google
    Lens / Yandex). When the
    second engine is set to "always", it joins wave 1 so IQDB and
    SauceNAO hit their different hosts at the same time instead of
    waiting on each other.
    When it's "fallback", it becomes wave 2 and only runs if the
    primary found nothing.
    """
    if override_engine:
        if override_engine in engine_ids.ALL_ENGINES:
            return [[override_engine]]
        log.warning("Ignoring unknown override engine %r", override_engine)

    primary = settings.primary_engine if settings.primary_engine in engine_ids.PRIMARY_ENGINES else engine_ids.IQDB
    secondary = engine_ids.SAUCENAO if primary == engine_ids.IQDB else engine_ids.IQDB
    mode = settings.secondary_engine_mode if settings.secondary_engine_mode in (
        "always", "fallback", "disabled",
    ) else "always"

    extras: List[str] = []
    if settings.enable_ascii2d:
        extras.append(engine_ids.ASCII2D)
    if settings.enable_tracemoe:
        extras.append(engine_ids.TRACEMOE)
    if settings.enable_iqdb3d:
        extras.append(engine_ids.IQDB3D)
    if settings.enable_google_images:
        extras.append(engine_ids.GOOGLE_IMAGES)
    if settings.enable_google_lens:
        extras.append(engine_ids.GOOGLE_LENS)
    if getattr(settings, "enable_yandex", False):
        extras.append(engine_ids.YANDEX)

    log.debug(
        "engine order primary=%s secondary=%s mode=%s extras=%s",
        primary, secondary, mode, extras,
    )

    # Held back, the extras become the fallback wave instead of running
    # on every image. That is the point of the setting: Google Lens
    # costs a page load per image and Cloud Vision costs money, so they
    # are worth spending only on the images the booru engines could not
    # place well.
    deferred = extras if settings.extras_only_as_fallback else []
    upfront = [] if settings.extras_only_as_fallback else extras
    # Pawchive always runs up front, whatever the setting above says: it
    # is one small request per image with no upload and no quota, and it
    # is the only engine that finds these files at all - holding it back
    # for "images the others could not place" would hold back nothing.
    # After the primary, never before it: waves[0][0] IS the primary as
    # far as _fallback_is_unnecessary is concerned.
    # The local index likewise: no request at all, and it answers from
    # memory in milliseconds.
    if getattr(settings, "enable_pawchive_index", False):
        upfront = [engine_ids.PAWCHIVE_INDEX, *upfront]
    if getattr(settings, "enable_pawchive", False):
        upfront = [engine_ids.PAWCHIVE, *upfront]

    if mode == "always":
        waves = [[primary, secondary, *upfront], [*deferred]]
    elif mode == "fallback":
        waves = [[primary, *upfront], [secondary, *deferred]]
    else:
        waves = [[primary, *upfront], [*deferred]]
    waves = [wave for wave in waves if wave]

    if skip_saucenao:
        # Its daily allowance is gone, so asking would spend a request to
        # be told so. Dropping it can empty a wave - when SauceNAO IS the
        # primary - and an empty wave must not survive into the plan,
        # since the pacing counts waves as hosts about to be queried.
        waves = [[e for e in wave if e != engine_ids.SAUCENAO] for wave in waves]
        waves = [wave for wave in waves if wave]
    return waves


# Engines whose silence says nothing about whether the picture exists
# anywhere: an exact-hash lookup and a local index of chosen artists. One
# of them finishing cleanly must not turn another engine's failure into
# a confident "not found".
_NOT_A_REAL_SEARCH = frozenset({engine_ids.PAWCHIVE, engine_ids.PAWCHIVE_INDEX})


def _no_match(errors: List[str], answered: Set[str]) -> Tuple[MatchStatus, Optional[str], bool]:
    """(status, message, final) for a search that ended with no candidates.

    An engine failing used to make the whole image an ERROR, even when
    another engine had searched and answered - on 2026-09-24 SauceNAO
    answered every time (best 40%, under the minimum) while IQDB timed
    out, and the row said "error" and was retried to the same end. Now:

      * every engine failed          -> ERROR, retried as before
      * one answered, others failed  -> NOT_FOUND with the failures as a
        note, and NOT cached (`final` False), so a later search asks the
        engines that couldn't answer this time
      * no failures                  -> NOT_FOUND, cached
    """
    if not errors:
        return MatchStatus.NOT_FOUND, None, True
    message = "; ".join(errors)
    if answered - _NOT_A_REAL_SEARCH:
        return MatchStatus.NOT_FOUND, message, False
    return MatchStatus.ERROR, message, True


def _run_engines_parallel(
    engine_names: List[str],
    entry: ImageEntry,
    settings: Settings,
    candidates: List[MatchCandidate],
    errors: List[str],
    on_tick: Optional[OnTick],
    on_engine_finished: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    answered: Optional[Set[str]] = None,
) -> dict:
    """Runs one wave of engines, concurrently when there's more than one.

    Each engine appends to its own lists; results are merged after join
    so two threads never mutate `candidates` at once. Returns
    {engine_id: found_any}, and adds each engine that finished without
    an error to `answered`.
    """
    if not engine_names:
        return {}
    if len(engine_names) == 1:
        errors_before = len(errors)
        try:
            found_any = _run_engine(engine_names[0], entry, settings, candidates, errors,
                                    on_tick, should_stop)
            if answered is not None and len(errors) == errors_before:
                answered.add(engine_names[0])
        finally:
            # In the `finally` so a failed request still counts as having
            # been made: it reached the host, so the next one owes it the
            # full gap regardless of what came back.
            if on_engine_finished is not None:
                on_engine_finished(engine_names[0])
        return {engine_names[0]: found_any}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Named apart from the single-engine bool above: one name holding a
    # bool on one path and this map on the other is what made five of
    # this file's type errors, all of them pointing at the same choice.
    found_by_name: Dict[str, bool] = {}
    workers = min(len(engine_names), len(engine_ids.ALL_ENGINES))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="engine") as pool:
        futures = {
            pool.submit(_run_engine_isolated, name, entry, settings, on_tick, should_stop): name
            for name in engine_names
        }
        for future in as_completed(futures):
            name = futures[future]
            if on_engine_finished is not None:
                on_engine_finished(name)
            try:
                engine_found, extra_candidates, extra_errors = future.result()
            except Exception as exc:
                log.exception("Engine %s raised: %s", name, exc)
                errors.append(f"{engine_ids.label_for(name)}: {exc}")
                found_by_name[name] = False
                continue
            candidates.extend(extra_candidates)
            errors.extend(extra_errors)
            found_by_name[name] = engine_found
            if answered is not None and not extra_errors:
                answered.add(name)
    return found_by_name


def _run_engine_isolated(engine: str, entry: ImageEntry, settings: Settings,
                         on_tick: Optional[OnTick],
                         should_stop: Optional[Callable[[], bool]] = None):
    local_candidates: List[MatchCandidate] = []
    local_errors: List[str] = []
    found = _run_engine(engine, entry, settings, local_candidates, local_errors,
                        on_tick, should_stop)
    return found, local_candidates, local_errors


def _run_engine(
    engine: str, entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str], on_tick: Optional[OnTick] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    """Runs one engine, appending its results to candidates/errors in
    place. Returns True if it found at least one usable result - used to
    decide whether a 'fallback' mode needs to try the other engine too."""
    if engine == engine_ids.IQDB:
        return _collect_iqdb(entry, settings, candidates, errors, on_tick, three_d=False)
    if engine == engine_ids.IQDB3D:
        return _collect_iqdb(entry, settings, candidates, errors, on_tick, three_d=True)
    if engine == engine_ids.SAUCENAO:
        return _collect_saucenao(entry, settings, candidates, errors, on_tick, should_stop)
    if engine == engine_ids.ASCII2D:
        return _collect_ascii2d(entry, settings, candidates, errors, on_tick)
    if engine == engine_ids.TRACEMOE:
        return _collect_tracemoe(entry, settings, candidates, errors, on_tick)
    if engine == engine_ids.GOOGLE_IMAGES:
        return _collect_google_images(entry, settings, candidates, errors, on_tick)
    if engine == engine_ids.GOOGLE_LENS:
        return _collect_google_lens(entry, settings, candidates, errors, on_tick)
    if engine == engine_ids.YANDEX:
        return _collect_yandex(entry, settings, candidates, errors, on_tick, should_stop)
    if engine == engine_ids.PAWCHIVE:
        return _collect_pawchive(entry, settings, candidates, errors)
    if engine == engine_ids.PAWCHIVE_INDEX:
        return _collect_pawchive_index(entry, candidates, errors)
    log.warning("Unknown search engine %r", engine)
    return False


def _append_candidate(
    candidates: List[MatchCandidate],
    url: str,
    source_name: Optional[str],
    thumb_url: Optional[str],
    similarity: float,
    engine_label: str,
    engine_tags: List[Tag],
    width: Optional[int] = None,
    height: Optional[int] = None,
):
    canonical = canonicalize_url(url)
    if url_is_structurally_dead(canonical):
        # Never becomes a candidate at all: no request can reach it, so
        # offering it would only waste the user's click.
        log.debug("Skipping %s - its URL form no longer resolves", canonical)
        return
    candidates.append(MatchCandidate(
        url=canonical,
        source_name=classify_site(source_name, canonical) or _guess_host(canonical),
        thumb_url=thumb_url,
        similarity=similarity,
        width=width,
        height=height,
        engine=engine_label,
        engine_tags=engine_tags,
    ))


def _collect_iqdb(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick], three_d: bool,
) -> bool:
    label = engine_ids.ENGINE_LABELS[engine_ids.IQDB3D if three_d else engine_ids.IQDB]
    start = time.monotonic()
    try:
        if three_d:
            results = iqdb.search_3d(entry.path, timeout=settings.search_timeout, on_tick=on_tick)
        else:
            results = iqdb.search(entry.path, timeout=settings.search_timeout, on_tick=on_tick)
        elapsed = time.monotonic() - start
        log.debug("%s returned %d result(s) for %s (%.1fs)", label, len(results), entry.filename, elapsed)
        if not results and elapsed >= SLOW_SEARCH_THRESHOLD_SECONDS:
            log.warning(
                "%s: %s took %.0fs and returned zero results - too slow to trust as a "
                "confident NOT_FOUND, won't be cached",
                entry.filename, label, elapsed,
            )
            errors.append(f"{label} took {elapsed:.0f}s and found nothing (too slow to trust)")
        for m in results:
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity, label,
                apply_tag_blacklist(list(m.unnamespaced_tags), settings),
                width=m.width, height=m.height,
            )
        return len(results) > 0
    except iqdb.IqdbError as exc:
        log.error("%s search failed for %s: %s", label, entry.filename, exc)
        errors.append(f"{label}: {exc}")
        return False


def _collect_saucenao(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    start = time.monotonic()
    try:
        results = saucenao.search(entry.path, settings.saucenao, timeout=settings.search_timeout,
                                  on_tick=on_tick, should_stop=should_stop)
        elapsed = time.monotonic() - start
        log.debug("SauceNAO returned %d result(s) for %s (%.1fs)", len(results), entry.filename, elapsed)
        found_any = False
        for m in results:
            if m.similarity < settings.saucenao.min_similarity:
                continue
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.SAUCENAO],
                apply_tag_blacklist(apply_namespace_remap(list(m.tags), settings), settings),
            )
            found_any = True
        if not found_any and elapsed >= SLOW_SEARCH_THRESHOLD_SECONDS:
            log.warning(
                "%s: SauceNAO took %.0fs and returned zero usable results - too slow to trust as a "
                "confident NOT_FOUND, won't be cached",
                entry.filename, elapsed,
            )
            errors.append(f"SauceNAO took {elapsed:.0f}s and found nothing (too slow to trust)")
        return found_any
    except saucenao.SauceNaoError as exc:
        log.error("SauceNAO search failed for %s: %s", entry.filename, exc)
        errors.append(f"SauceNAO: {exc}")
        return False


def _collect_ascii2d(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
) -> bool:
    if ascii2d.is_blocked():
        # Already established that ascii2d is challenging every upload.
        # Asking again costs a request per image to be refused again, and
        # would repeat the same failure on every row of the batch.
        log.debug("Skipping ascii2d for %s - it is refusing uploads this session", entry.filename)
        return False

    start = time.monotonic()
    try:
        results = ascii2d.search(entry.path, timeout=settings.search_timeout, on_tick=on_tick)
        elapsed = time.monotonic() - start
        log.debug("ascii2d returned %d result(s) for %s (%.1fs)", len(results), entry.filename, elapsed)
        for m in results:
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.ASCII2D],
                [],
                width=m.width, height=m.height,
            )
        return len(results) > 0
    except ascii2d.Ascii2dBlockedError as exc:
        # Reported once, against the image that discovered it. Every later
        # image skips ascii2d entirely rather than repeating this.
        log.error("ascii2d is blocking this app: %s", exc)
        errors.append(f"ascii2d: {exc}")
        return False
    except ascii2d.Ascii2dError as exc:
        log.error("ascii2d search failed for %s: %s", entry.filename, exc)
        errors.append(f"ascii2d: {exc}")
        return False


def _collect_tracemoe(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
) -> bool:
    start = time.monotonic()
    try:
        results = tracemoe.search(
            entry.path, timeout=settings.search_timeout, on_tick=on_tick,
            min_similarity=settings.tracemoe_min_similarity,
        )
        elapsed = time.monotonic() - start
        log.debug("trace.moe returned %d result(s) for %s (%.1fs)", len(results), entry.filename, elapsed)
        for m in results:
            _append_candidate(
                candidates, m.url, m.title, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.TRACEMOE],
                apply_tag_blacklist(apply_namespace_remap(list(m.tags), settings), settings),
            )
        return len(results) > 0
    except tracemoe.TraceMoeError as exc:
        log.error("trace.moe search failed for %s: %s", entry.filename, exc)
        errors.append(f"trace.moe: {exc}")
        return False


def _collect_google_images(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
) -> bool:
    if google_images.is_blocked():
        # Already established that Google won't answer this session -
        # a consent wall, a bot check, a JavaScript-only results page or
        # a rejected key. Asking again would spend a request per image to
        # be refused the same way, so it isn't asked.
        #
        # A short note still goes on the row, unlike the full explanation
        # the first image got. An engine that was enabled and never
        # queried must not leave the row looking like a confident "found
        # nothing": with no error at all, a row whose other engines also
        # came up empty would be recorded as NOT_FOUND and cached, and
        # would never be searched again. An ERROR is not cached.
        log.debug("Skipping Google Images for %s - it is refusing this app this session",
                  entry.filename)
        errors.append("Google Images: not searched (it stood down earlier this run)")
        return False

    start = time.monotonic()
    try:
        results = google_images.search(
            entry.path, timeout=settings.search_timeout, on_tick=on_tick,
            api_key=settings.google_images_api_key,
        )
        elapsed = time.monotonic() - start
        log.debug("Google Images returned %d result(s) for %s (%.1fs)",
                  len(results), entry.filename, elapsed)
        for m in results:
            # No tags: Google indexes pages, not a tag vocabulary. When a
            # hit lands on a site that boorus/ can parse, the ordinary
            # page fetch supplies the tags; when it doesn't, the value of
            # the row is the source URL itself.
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.GOOGLE_IMAGES],
                [],
            )
        return len(results) > 0
    except google_images.GoogleImagesBlockedError as exc:
        # Reported once, against the image that discovered it. Every later
        # image skips Google entirely rather than repeating this.
        log.error("Google is blocking this app: %s", exc)
        errors.append(f"Google Images: {exc}")
        return False
    except google_images.GoogleImagesError as exc:
        log.error("Google Images search failed for %s: %s", entry.filename, exc)
        errors.append(f"Google Images: {exc}")
        return False


# Engines that shut themselves off for the rest of a run - Google
# challenged, Cloudflare blocked, Playwright is not installed. Keyed by
# the prefix their messages carry in entry.error_message.
_STANDS_DOWN = (
    ("Google Lens:", google_lens.is_blocked),
    ("Google Images:", google_images.is_blocked),
    ("ascii2d:", ascii2d.is_blocked),
    ("Yandex:", yandex.is_blocked),
)


def retry_could_help(entry: ImageEntry) -> bool:
    """Whether a second attempt at a failed image could turn out different.

    Not when every failure came from an engine that has stood down for
    this run: the retry would skip that engine and fail again, having
    spent the pacing delay to report "not searched". Anything that
    cannot be attributed to such an engine counts as worth retrying,
    since assuming otherwise is the mistake that can't be seen.
    """
    parts = [p.strip() for p in (entry.error_message or "").split(";") if p.strip()]
    if not parts:
        return True
    for part in parts:
        if not any(part.startswith(prefix) and stood_down()
                   for prefix, stood_down in _STANDS_DOWN):
            return True
    return False


def _collect_pawchive(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
) -> bool:
    """Exact-copy lookup on pawchive.pw - see core/pawchive_lookup.py."""
    sha256 = entry.hydrus_hash
    if not sha256:
        # Normally already known: the hash worker fills it in when the file
        # is added. Reading the file here is the fallback, not the rule.
        try:
            sha256 = pawchive_lookup.file_sha256(entry.path)
        except OSError as exc:
            errors.append(f"Pawchive: could not read the file to hash it ({exc})")
            return False
    try:
        matches = pawchive_lookup.lookup(sha256, timeout=settings.search_timeout)
    except pawchive_lookup.PawchiveLookupError as exc:
        log.warning("Pawchive lookup failed for %s: %s", entry.filename, exc)
        errors.append(f"Pawchive: {exc}")
        return False
    if matches:
        log.info("%s: an exact copy is on pawchive.pw (%s)", entry.filename,
                 ", ".join(m.url for m in matches))
    else:
        log.debug("%s: not on pawchive.pw", entry.filename)
    label = engine_ids.ENGINE_LABELS[engine_ids.PAWCHIVE]
    for match in matches:
        # 100 and meant: the lookup is by the file's own SHA-256, so every
        # result holds these exact bytes. Which image of the post it is
        # gets settled again when the post is fetched - see
        # _resolve_multipage_candidate's exact-hash check.
        _append_candidate(candidates, match.url, "Pawchive", match.thumb_url, 100.0, label, [])
    return bool(matches)


_pawchive_index: Optional["pawchive_index.PawchiveIndex"] = None


def _shared_pawchive_index() -> "pawchive_index.PawchiveIndex":
    """One instance for every search, so the fingerprints are loaded into
    memory once and reloaded only after the index changes."""
    global _pawchive_index
    if _pawchive_index is None:
        _pawchive_index = pawchive_index.PawchiveIndex()
    return _pawchive_index


def _collect_pawchive_index(
    entry: ImageEntry, candidates: List[MatchCandidate], errors: List[str],
) -> bool:
    """Resized or re-saved copies of indexed pawchive artists' images -
    see core/pawchive_index.py. Local: no request is made."""
    import sqlite3
    index = _shared_pawchive_index()
    try:
        if index.is_empty():
            return False                  # nothing indexed yet - and no file read for it
        _, local_hash16 = pawchive_index.fingerprints(entry.path)
        if local_hash16 is None:
            errors.append("Pawchive index: could not read the local file")
            return False
        matches = index.search(local_hash16, entry.hydrus_hash)
    except sqlite3.Error as exc:
        log.warning("Pawchive index could not be searched: %s", exc)
        errors.append(f"Pawchive index: {exc}")
        return False
    for match in matches:
        # Same SHA-256 means the very same file - labelled as the exact
        # engine, which is what lets it be sent as the original.
        label = engine_ids.ENGINE_LABELS[
            engine_ids.PAWCHIVE if match.exact else engine_ids.PAWCHIVE_INDEX]
        _append_candidate(candidates, match.url, "Pawchive", match.thumb_url,
                          match.similarity, label, [])
    if matches:
        log.info("%s: the pawchive index has %s", entry.filename, ", ".join(
            f"{m.url} ({m.similarity:.0f}%{', exact' if m.exact else ''})" for m in matches))
    return bool(matches)


def _collect_google_lens(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
) -> bool:
    if google_lens.is_blocked():
        # Google challenged and the check went unanswered, or there is no
        # browser engine to render with. Either way the next image meets
        # the same wall, so it isn't asked - but the row still says so,
        # or an image whose other engines also found nothing would be
        # recorded and cached as a confident NOT_FOUND.
        why = (f"resting after an unanswered robot check, until {google_lens.resting_until()}"
               if google_lens.is_resting() else "it stood down earlier this run")
        log.debug("Skipping Google Lens for %s - %s", entry.filename, why)
        errors.append(f"Google Lens: not searched ({why})")
        return False

    google_lens.wait_for_turn(on_tick)
    start = time.monotonic()
    try:
        results = google_lens.search(
            entry.path, timeout=settings.search_timeout, on_tick=on_tick,
            whole_image=settings.lens_search_whole_image,
        )
        elapsed = time.monotonic() - start
        log.debug("Google Lens returned %d result(s) for %s (%.1fs)",
                  len(results), entry.filename, elapsed)
        for m in results:
            # No tags, for the same reason as Google Images: Lens indexes
            # pages, not a tag vocabulary. A hit that lands on a site
            # boorus/ can parse gets its tags from the page fetch.
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.GOOGLE_LENS],
                [],
                width=m.width, height=m.height,
            )
        return len(results) > 0
    except google_lens.GoogleLensUnavailableError as exc:
        log.error("Google Lens cannot run: %s", exc)
        errors.append(f"Google Lens: {exc}")
        return False
    except google_lens.GoogleLensBlockedError as exc:
        log.error("Google Lens was challenged and stood down: %s", exc)
        errors.append(f"Google Lens: {exc}")
        return False
    except google_lens.GoogleLensError as exc:
        log.error("Google Lens search failed for %s: %s", entry.filename, exc)
        errors.append(f"Google Lens: {exc}")
        return False


def _collect_yandex(
    entry: ImageEntry, settings: Settings,
    candidates: List[MatchCandidate], errors: List[str],
    on_tick: Optional[OnTick],
    should_stop: Optional[Callable[[], bool]] = None,
) -> bool:
    if yandex.is_blocked():
        # Same reasoning as Google Lens: not asked again this run, but the
        # row says so, or an image the other engines also found nothing
        # for would be cached as a confident NOT_FOUND.
        why = (f"resting after a captcha, until {yandex.resting_until()}"
               if yandex.is_resting() else "it stood down earlier this run")
        log.debug("Skipping Yandex for %s - %s", entry.filename, why)
        errors.append(f"Yandex: not searched ({why})")
        return False

    yandex.wait_for_turn(on_tick, should_stop)
    start = time.monotonic()
    try:
        results = yandex.search(entry.path, timeout=settings.search_timeout, on_tick=on_tick)
        elapsed = time.monotonic() - start
        log.debug("Yandex returned %d result(s) for %s (%.1fs)",
                  len(results), entry.filename, elapsed)
        for m in results:
            # No tags: only post pages boorus/ can parse are kept, and the
            # page fetch supplies them.
            _append_candidate(
                candidates, m.url, m.source_name, m.thumb_url, m.similarity,
                engine_ids.ENGINE_LABELS[engine_ids.YANDEX],
                [],
                width=m.width, height=m.height,
            )
        return len(results) > 0
    except yandex.YandexBlockedError as exc:
        log.error("Yandex stood down: %s", exc)
        errors.append(f"Yandex: {exc}")
        return False
    except yandex.YandexError as exc:
        log.error("Yandex search failed for %s: %s", entry.filename, exc)
        errors.append(f"Yandex: {exc}")
        return False


def _guess_host(url: str) -> Optional[str]:
    try:
        return urlparse(url).netloc or None
    except ValueError:
        return None
