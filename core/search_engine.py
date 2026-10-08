"""Orchestrates a single image's search: queries IQDB (which itself checks
several booru sites at once) and, if enabled, SauceNAO too, merges every
result into a list of MatchCandidate objects on the ImageEntry, and fetches
the picture + tags for whichever one is most similar. The rest stay listed
so the user can flip through them in the GUI's candidate dropdown.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set

import threading

from PIL import Image

from .applog import get_logger
from .boorus import fetch_page_info, BooruContentGoneError, BooruError
from .config import Settings
from .hydrus_tag_lookup import hash_file
from .models import ImageEntry, MatchCandidate, MatchStatus
from .progress_ticker import OnTick
from .search_cache import apply_cached_result, load_cached_result, save_cached_result
from .ranking import rank_candidates
from .sites import classify_site
from .tag_band_metrics import EntryMeasurement, Recorder, bucket_for, measure_entry
from .tag_borrowing import borrow_tags
from .tag_rules import (
    apply_namespace_remap, apply_outcome_tags, apply_tag_blacklist, countable_tags,
    with_rating_tag,
)
# The pieces this module was split into. Re-exported, so everything that
# imported these from core.search_engine still can - and the modules
# themselves, for callers that go through them (remote.download_bytes).
from . import availability, engine_runner, multipage_resolve, remote, similarity_check  # noqa: F401
from .site_access import (  # noqa: F401
    ANIMEPICTURES_SESSION_COOKIE, USER_AGENT, animepictures_cookies, cookie_paste_warnings,
    cookies_for_url, e621_auth_header, headers_for_url, parse_cookie_string,
)
from .remote import (  # noqa: F401
    _format_from_content_type, download_bytes, fetch_remote_info, fetch_text,
    referer_for_candidate,
)
from .availability import (  # noqa: F401
    SANKAKU_POST_ID_RE, SOFT_404_MARKERS, SOFT_404_SCAN_BYTES, _page_says_available,
    _page_title, _recheck_cached_candidates, _redirect_means_gone, _redirect_to_index,
    _should_availability_check, _soft_404_markers_for, _sweep_candidate_availability, check_url_available,
    drop_structurally_dead, has_soft_404_detection, sankaku_post_exists, url_is_structurally_dead,
)
from .multipage_resolve import (  # noqa: F401
    _resolve_multipage_candidate,
)
from .engine_runner import (  # noqa: F401
    _append_candidate, _collect_ascii2d, _collect_google_images, _collect_google_lens,
    _collect_pawchive, _collect_pawchive_index, _collect_yandex, _engine_waves, _fallback_is_unnecessary,
    _no_match, _run_engines_parallel, planned_engines, retry_could_help,
)
from .similarity_check import (  # noqa: F401
    _recheck_borrowed_measurement, _worth_hashing, measure_ordinal_similarities,
)

# How many candidates to try before giving up on finding a live one. Each
# attempt costs a booru page fetch, so this is bounded rather than
# walking a long candidate list on an image whose sources are all dead.
MAX_DEAD_CANDIDATE_RETRIES = 4

log = get_logger("search")

# Tag-band instrumentation (DAN-55). Off unless the environment names an
# output file, so a normal run never touches it. Resolved once per process
# rather than per search: reading the variable on every entry would make a
# mid-run change take effect halfway through a measurement, and half a
# measurement is worse than none.
_tag_band_lock = threading.Lock()
_tag_band_recorder: Optional[Recorder] = None
_tag_band_resolved = False


def tag_band_recorder() -> Optional[Recorder]:
    """The process's measurement recorder, or None when not measuring."""
    global _tag_band_recorder, _tag_band_resolved
    with _tag_band_lock:
        if not _tag_band_resolved:
            _tag_band_recorder = Recorder.from_env()
            _tag_band_resolved = True
        return _tag_band_recorder


def _measure_tag_band(entry: ImageEntry, candidates: List[MatchCandidate],
                      chosen_index: int, settings: Settings) -> Optional[EntryMeasurement]:
    """Snapshots the tag/lender picture BEFORE the tagless rescue runs.

    Timing is the whole point. `borrow_tags` writes into
    `chosen.booru_tags`, so a rescued entry measured afterwards no longer
    looks tagless and the tagless band would read as empty. The other half
    of the record - `len(entry.tags)` - does not exist yet at this point,
    because `entry.select_candidate()` has not run, so it is filled in by
    `_finish_tag_band_measurement` once the status is decided.
    """
    recorder = tag_band_recorder()
    if recorder is None:
        return None
    try:
        return measure_entry(
            len(entry.tags), candidates, chosen_index,
            settings.match_conditions.min_tags_for_good,
            getattr(settings, "borrow_tags_similarity_slack", 0.0),
        )
    except Exception:  # pragma: no cover - instrumentation must not break a search
        log.exception("%s: tag-band measurement failed", entry.filename)
        return None


def _finish_tag_band_measurement(measurement: Optional[EntryMeasurement], entry: ImageEntry,
                                 settings: Settings, *, all_matches_dead: bool = False) -> None:
    """Completes the snapshot with the final tag count and writes the row."""
    recorder = tag_band_recorder()
    if recorder is None or measurement is None:
        return
    try:
        measurement.all_matches_dead = all_matches_dead
        # countable_tags, not entry.tags: this measures how many tags the
        # engines and boorus actually yielded, and the DAN-77 by-rule tags
        # are the app's own bookkeeping. Counting those would inflate the
        # band on exactly the entries the instrumentation exists to study -
        # the thin ones - and does so by however many tags the user happens
        # to have configured. Same count _decide_status uses.
        measurement.entry_tag_count = len(countable_tags(entry.tags))
        measurement.entry_bucket = bucket_for(
            measurement.entry_tag_count, settings.match_conditions.min_tags_for_good,
        )
        recorder.record(measurement)
    except Exception:  # pragma: no cover - instrumentation must not break a search
        log.exception("%s: tag-band measurement could not be recorded", entry.filename)


def fetch_candidate_details(candidate: MatchCandidate, settings: Settings,
                            on_tick: Optional[OnTick] = None, local_path: Optional[str] = None):
    """Fills in a candidate's thumbnail bytes, booru-page tags, direct file
    URL, preview URL, and remote format/size info, mutating it in place.
    Safe to call more than once - skips work already done. Used both for
    the top pick right after searching, and lazily when the user picks a
    different candidate from the dropdown.

    If given, on_tick(label, remaining, total) fires roughly once a
    second while a request is in flight, purely for UI countdown
    feedback."""
    if not candidate.booru_tags_fetched:
        candidate.booru_tags_fetched = True
        if settings.retrieve_tags_from_booru and candidate.url:
            try:
                page_info = fetch_page_info(
                    candidate.url, timeout=settings.search_timeout, on_tick=on_tick,
                    cookies=cookies_for_url(candidate.url, settings),
                    headers=headers_for_url(candidate.url, settings),
                    gone_markers=_soft_404_markers_for(candidate.url),
                    gone_redirect=_redirect_means_gone,
                    local_path=local_path,
                    settings=settings,
                )
                if page_info.gone_reason:
                    # The page itself says the post is deleted. This is
                    # the only check a single-candidate match gets - the
                    # availability sweep only runs when there's more than
                    # one candidate to choose between.
                    candidate.record_availability(False)
                candidate.booru_tags = apply_tag_blacklist(
                    apply_namespace_remap(
                        with_rating_tag(page_info.tags, page_info.rating, settings),
                        settings,
                    ),
                    settings,
                )
                candidate.direct_file_url = page_info.file_url
                candidate.preview_url = page_info.preview_url
                if page_info.canonical_url and page_info.canonical_url != candidate.url:
                    # The URL we were given is a dead legacy route; now
                    # that the post has been identified, point at where it
                    # actually lives - this is the link the user opens and
                    # the one associated with the file in Hydrus.
                    log.info("Repointing %s to its current address %s",
                             candidate.url, page_info.canonical_url)
                    candidate.url = page_info.canonical_url
                # IQDB already gives us dimensions for its own matches; for
                # SauceNAO-origin candidates we don't have them until now.
                if candidate.width is None and page_info.width:
                    candidate.width = page_info.width
                if candidate.height is None and page_info.height:
                    candidate.height = page_info.height
                # A site that states these describes its ORIGINAL, which a
                # HEAD can only do when the original is actually reachable.
                if page_info.file_format and not candidate.remote_format:
                    candidate.remote_format = page_info.file_format
                if page_info.file_size_bytes and not candidate.remote_size_bytes:
                    candidate.remote_size_bytes = page_info.file_size_bytes
                if page_info.page_count > 1 and local_path:
                    _resolve_multipage_candidate(
                        candidate, page_info.page_count, settings, local_path, page_info,
                    )
                elif local_path:
                    _recheck_borrowed_measurement(candidate, page_info, settings, local_path)
                candidate.incomplete_reason = page_info.incomplete_reason
                candidate.restricted = page_info.restricted
                if page_info.restricted and settings.drop_restricted_matches:
                    # The post exists, but the site won't serve its image
                    # without a paid account tier. Marked unavailable so
                    # the existing dead-match handling drops it, because
                    # from this app's side the outcome is the same: no
                    # image to preview, download, compare or send to
                    # Hydrus. Kept as a SEPARATE flag from the drop so
                    # the reason survives for the log and the UI - a
                    # restricted post isn't deleted, and conflating the
                    # two would misreport why it went.
                    log.info("Dropping %s - %s", candidate.url, page_info.restricted)
                    candidate.record_availability(False)
                elif page_info.fetched and candidate.remote_available is None:
                    # Only meaningful when a parser actually fetched the
                    # page - for a site with no parser, fetch_page_info
                    # returns an empty result WITHOUT raising, which
                    # would otherwise be misread as "confirmed alive"
                    # despite nothing having been checked.
                    #
                    # And only when nothing has already decided. A page
                    # that SAYS the post is deleted was still fetched,
                    # so without this guard "fetched" overwrote the
                    # gone_reason verdict a few lines above with True -
                    # which quietly undid every soft-404 check in the
                    # app, for every site that has one.
                    candidate.record_availability(True)
            except BooruContentGoneError:
                # The post is definitively deleted/removed - record that
                # here so it doesn't have to be rediscovered later by a
                # separate availability check. Note this only applies to
                # a definite 404/410; a plain BooruError below could be a
                # timeout or rate-limit and says nothing about whether
                # the content still exists.
                candidate.record_availability(False)
                candidate.booru_tags = []
            except BooruError as exc:
                # A timeout, connection failure, or non-200 response -
                # says nothing about whether the post still exists, so
                # remote_available is left alone. Without incomplete_reason
                # this looked byte-for-byte identical to a post that
                # genuinely has zero tags, and without resetting
                # booru_tags_fetched it could never be retried even if
                # the user re-selected the same candidate later.
                log.warning("Could not fetch booru tags for %s: %s", candidate.url, exc)
                candidate.booru_tags = []
                candidate.incomplete_reason = str(exc)
                candidate.booru_tags_fetched = False

    if (
        candidate.remote_available is None
        and settings.drop_dead_matches
        and candidate.url
        and _should_availability_check(candidate.url)
    ):
        # Sites with no booru parser (DeviantArt, etc.) never went through
        # the fetch above, so nothing has established whether they're
        # still alive. A deleted deviation is exactly the dead link worth
        # dropping, so check it directly - this is a single cheap request
        # that reads only the status line and headers.
        candidate.record_availability(availability._reactor_full_fallback(
            candidate,
            availability.check_url_available(
                candidate.url, settings.search_timeout, on_tick=on_tick,
                cookies=cookies_for_url(candidate.url, settings), settings=settings,
            ),
        ))

    if candidate.thumb_bytes is None:
        # Prefer the booru's own mid-resolution "sample" image over the
        # search engine's tiny thumbnail when we have one - much sharper,
        # without the cost of downloading the full original just to show
        # a preview. Falls back to the search engine's thumbnail if the
        # site has no sample or we couldn't fetch its page.
        preview_source = candidate.preview_url or candidate.thumb_url
        if preview_source:
            # Same hotlink-protection concern as the format/size check
            # below - send the right Referer when hitting the booru's own
            # CDN for the sample image.
            referer = referer_for_candidate(candidate, preview_source)
            candidate.thumb_bytes = remote.download_bytes(
                preview_source, settings.search_timeout, referer=referer, on_tick=on_tick,
                cookies=cookies_for_url(preview_source, settings),
            )

    if not candidate.remote_info_fetched:
        candidate.remote_info_fetched = True
        if candidate.remote_format and candidate.remote_size_bytes:
            # The site already stated both, about the real original. A HEAD
            # could only agree at best - and on a site whose original isn't
            # reachable it would fall back to the search engine's thumbnail
            # and report THAT as the match's format and size.
            log.debug(
                "%s: format/size came from the site itself (%s, %s bytes) - skipping the HEAD",
                candidate.url, candidate.remote_format, candidate.remote_size_bytes,
            )
        else:
            # Use the actual image file URL when we have one (from the booru
            # page) - HEADing the post page itself just reports "text/html",
            # which is technically correct but not what the user wants to see.
            # Fall back to the thumbnail URL (still a genuine image resource,
            # just not full-size) rather than ever HEADing the HTML page.
            info_url = candidate.direct_file_url or candidate.thumb_url
            # Send the right Referer when HEADing the direct file URL -
            # several image CDNs (Gelbooru's and Pixiv's included) enforce
            # hotlink protection and serve an HTML block/challenge page
            # instead of the real image to requests with no (or the wrong)
            # Referer, which looks identical to a wrongly-extracted URL from
            # the outside.
            referer = referer_for_candidate(candidate, info_url)
            fmt, size_bytes = remote.fetch_remote_info(
                info_url, settings.search_timeout, referer=referer, on_tick=on_tick,
            )
            # Never let the HEAD blank out a value the site stated itself -
            # it may have supplied only one of the two.
            candidate.remote_format = candidate.remote_format or fmt
            candidate.remote_size_bytes = candidate.remote_size_bytes or size_bytes


def search_image(
    entry: ImageEntry, settings: Settings, override_engine: Optional[str] = None,
    use_cache: bool = True, on_tick: Optional[OnTick] = None,
    on_engines_done: Optional[Callable[[], None]] = None,
    on_engine_finished: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    skip_saucenao: bool = False,
) -> ImageEntry:
    """If given, on_tick(label, remaining, total) fires roughly once a
    second while a request to IQDB/SauceNAO/a booru page/a thumbnail is
    in flight - purely for UI countdown feedback, since a single HTTP
    request has no native progress of its own.

    on_engines_done() fires once, the moment the last search-engine
    request completes and before any booru page is fetched. That is the
    point the caller's rate limit should be measured from: everything
    after it hits booru hosts instead, so counting it toward the gap
    between engine requests would be charging for the wrong thing. It
    does not fire at all when the result came from the cache, or when
    the search failed before an engine request finished.

    on_engine_finished(engine_id) fires as each individual engine
    completes, which is what lets the caller pace each host separately -
    a wave queries several hosts at once, so one timestamp for the whole
    wave cannot say when any particular one of them was last called."""
    log.info("Searching %s", entry.filename)
    entry.status = MatchStatus.SEARCHING
    entry.error_message = None
    entry.candidates = []

    _load_local_dimensions(entry)

    if not entry.hydrus_hash:
        entry.hydrus_hash = hash_file(entry.path)

    if use_cache and entry.hydrus_hash:
        cached = load_cached_result(entry.hydrus_hash, ttl_days=settings.search_cache_ttl_days)
        if cached is not None:
            if apply_cached_result(entry, cached):
                # A result cached before a site's URLs stopped resolving
                # still carries them. Cheap to clean up here, and the
                # alternative - throwing the whole cache away - would
                # mean re-searching every image at 45-75s each.
                cleaned = drop_structurally_dead(entry.candidates, entry.filename)
                if settings.drop_dead_matches:
                    cleaned = _recheck_cached_candidates(
                        cleaned, settings, entry.filename, on_tick)
                if len(cleaned) != len(entry.candidates):
                    entry.candidates = cleaned
                    entry.select_candidate(0 if cleaned else -1)
                log.info(
                    "%s: using cached search result (%d candidate(s), status=%s) - "
                    "skipping IQDB/SauceNAO",
                    entry.filename, len(entry.candidates), entry.status.value,
                )
                if entry.candidates:
                    selected = entry.candidates[entry.selected_candidate_index]
                    fetch_candidate_details(selected, settings, on_tick=on_tick, local_path=entry.path)
                    entry.select_candidate(entry.selected_candidate_index)  # re-sync now thumb_bytes is filled in
                entry.result_source = "cached"
                entry.mark_searched()
                # The cache stores status and candidates but NOT
                # entry.tags (see core/search_cache.py:save_cached_result),
                # so the by-rule tags are not restored with the result and
                # have to be re-derived here. Without this the rule would
                # silently stop applying as soon as an image was in the
                # cache - which, after the first pass over a library, is
                # every image.
                apply_outcome_tags(entry, settings)
                return entry

    # A genuinely fresh search produces a NEW result, so this entry is
    # eligible to be sent again. Without this reset, an entry restored
    # from a saved session came back with sent_to_hydrus=True and was
    # skipped by auto-import forever - even after the user explicitly
    # re-searched it and got a different match. Re-sending is safe:
    # Hydrus's file import is idempotent, and re-adding a URL to its
    # downloader is a no-op if it already has it.
    entry.sent_to_hydrus = False
    entry.hydrus_import_confirmed = False

    candidates: List[MatchCandidate] = []
    errors: List[str] = []

    tick_lock = threading.Lock()

    def tick(*args):
        if on_tick is None:
            return
        with tick_lock:
            on_tick(*args)

    waves = _engine_waves(settings, override_engine, skip_saucenao)
    found_by_engine: Dict[str, bool] = {}
    # Engines that searched and came back without an error - a real
    # "here is what I found", even when that is nothing. See _no_match.
    answered: Set[str] = set()
    # The fallback engines, if they were skipped. Kept because the result
    # they were skipped FOR can still be thrown away afterwards - by the
    # site filter, or by the availability sweep finding it dead - and an
    # image should not end up with nothing while engines that were never
    # asked sit in the plan.
    deferred_wave = None
    for wave in waves:
        if not wave:
            continue
        # The last wave is a fallback: it runs only when what has been
        # found so far is not good enough. What "good enough" means is
        # the user's call - see _fallback_is_unnecessary.
        if len(waves) > 1 and wave is waves[-1]:
            skip, why = _fallback_is_unnecessary(settings, waves, found_by_engine, candidates)
            if skip:
                log.debug("%s: skipping fallback engines %s - %s", entry.filename, wave, why)
                deferred_wave = wave
                break
            log.debug("%s: running fallback engines %s - %s", entry.filename, wave, why)
        wave_found = _run_engines_parallel(
            wave, entry, settings, candidates, errors, tick, on_engine_finished,
            should_stop, answered,
        )
        found_by_engine.update(wave_found)

    if on_engines_done is not None:
        # Every engine request for this image is now done. Everything
        # below talks to booru hosts, not to IQDB/SauceNAO.
        on_engines_done()

    candidates = _dedupe(candidates)
    before_site_filter = len(candidates)
    candidates = [c for c in candidates if classify_site(c.source_name, c.url) in settings.enabled_sites]
    if len(candidates) != before_site_filter:
        log.debug(
            "%s: %d candidate(s) dropped by Sites filter (%d -> %d)",
            entry.filename, before_site_filter - len(candidates), before_site_filter, len(candidates),
        )
    # Before the sort, so the order reflects the real numbers rather than
    # the positions the engines made up. After the site filter, so
    # nothing is downloaded for a candidate that is about to be dropped.
    measure_ordinal_similarities(entry, candidates, settings)
    candidates.sort(key=lambda c: -c.similarity)
    entry.candidates = candidates
    log.info("%s: %d unique candidate(s) after merge and site filter", entry.filename, len(candidates))

    def run_deferred_engines(reason: str) -> bool:
        """Run the fallback engines after all, and report whether they
        turned anything up.

        They were skipped because something good enough had been found.
        When that something is thrown away afterwards - filtered out by
        site, or found to be dead - the reason for skipping them is gone
        with it, and the image would otherwise be recorded as NOT_FOUND
        while engines that were never asked sat in the plan. MEASURED
        over one real run: thirteen searches ended that way.
        """
        nonlocal candidates, deferred_wave
        if not deferred_wave:
            return False
        wave, deferred_wave = deferred_wave, None      # one retry, not a loop
        log.info("%s: %s - running the fallback engines %s after all",
                 entry.filename, reason, wave)
        _run_engines_parallel(wave, entry, settings, candidates, errors, tick,
                              on_engine_finished, should_stop, answered)
        candidates = _dedupe(candidates)
        candidates = [
            c for c in candidates
            # Anything already proved gone stays gone. One of the two
            # callers is the "every match was dead" branch, which
            # computes the live list but does not assign it - so the
            # dead ones are still in `candidates` here, and rebuilding
            # the list without this put the very match that triggered
            # the retry straight back into the dropdown.
            if c.remote_available is not False
            and classify_site(c.source_name, c.url) in settings.enabled_sites
        ]
        measure_ordinal_similarities(entry, candidates, settings)
        candidates.sort(key=lambda c: -c.similarity)
        entry.candidates = candidates
        return bool(candidates)

    if not candidates and run_deferred_engines("every match was filtered out"):
        log.info("%s: the fallback engines found %d candidate(s)",
                 entry.filename, len(candidates))

    if not candidates:
        entry.status, entry.error_message, final = _no_match(errors, answered)
        entry.result_source = "fresh"
        entry.searched_without_saucenao = skip_saucenao
        entry.mark_searched()
        # _no_match can return ERROR as well as NOT_FOUND; apply_outcome_tags
        # adds nothing for ERROR, so this stays a no-op for a search that
        # merely failed.
        apply_outcome_tags(entry, settings)
        if entry.hydrus_hash and final:
            save_cached_result(entry.hydrus_hash, entry)
        return entry

    if settings.drop_dead_matches and len(candidates) > 1:
        # Check EVERY candidate's availability up front, not just the ones
        # we'd fetch details for - otherwise a dead match further down the
        # list survives into the dropdown and is only discovered when the
        # user clicks it, which wastes their time for no reason. This is a
        # cheap status-only request per candidate, run concurrently since
        # they hit different hosts.
        _sweep_candidate_availability(candidates, settings, entry.filename, on_tick)

    if settings.rank_matches_by_quality:
        # Re-ordered only now, once availability is known - ranking before
        # the sweep would recommend a match that's about to be found dead.
        # Similarity remains dominant; see core/ranking.py.
        candidates = rank_candidates(
            candidates, entry.local_width, entry.local_height, entry.filename,
        )
        entry.candidates = candidates

    # Fetch details for the best candidate - but if that fetch reveals the
    # post is definitively gone (a 404/410, not a transient failure), it's
    # not a usable match, so try the next-best one instead of settling on
    # a dead link. Bounded so a run of dead matches can't turn one search
    # into a long chain of fetches.
    max_attempts = min(len(candidates), MAX_DEAD_CANDIDATE_RETRIES)
    chosen_index = 0
    rescored = False
    for attempt in range(max_attempts):
        if candidates[attempt].remote_available is False:
            continue  # already known dead from the sweep - don't waste a detail fetch on it
        score_before = candidates[attempt].similarity
        fetch_candidate_details(candidates[attempt], settings, on_tick=on_tick,
                                local_path=entry.path)
        if candidates[attempt].remote_available is False:
            log.info(
                "%s: candidate %d (%s) is gone, trying the next best match",
                entry.filename, attempt + 1, candidates[attempt].url,
            )
            continue
        if candidates[attempt].similarity < score_before - 1.0:
            # Its score came from a picture that wasn't its own - see
            # _recheck_borrowed_measurement. Not the best match after all.
            rescored = True
            log.info("%s: candidate %d (%s) re-scored %.0f%% -> %.0f%%, trying the next best",
                     entry.filename, attempt + 1, candidates[attempt].url,
                     score_before, candidates[attempt].similarity)
            continue
        chosen_index = attempt
        break
    else:
        # Every candidate we checked was gone - keep the first one
        # selected so the entry still shows something, but the status
        # logic below and the dead-link filter will reflect reality.
        chosen_index = 0

    if rescored:
        # Put the re-scored ones where their real numbers belong, keeping
        # the choice made above.
        chosen_candidate = candidates[chosen_index]
        candidates = rank_candidates(
            candidates, entry.local_width, entry.local_height, entry.filename,
        )
        entry.candidates = candidates
        chosen_index = candidates.index(chosen_candidate)

    # Read-only, and deliberately ahead of the borrow below - see
    # _measure_tag_band. No-op unless the run was started with the
    # measurement environment variable set.
    tag_band = _measure_tag_band(entry, candidates, chosen_index, settings)

    # A chosen match with no tags is not always a dead end: another match
    # of the SAME picture may carry them. See core/tag_borrowing.py.
    if candidates and not candidates[chosen_index].booru_tags:
        borrow_tags(
            entry, candidates, chosen_index, settings,
            lambda c: fetch_candidate_details(c, settings, on_tick=on_tick,
                                              local_path=entry.path),
        )

    if settings.drop_dead_matches:
        live = [c for c in candidates if c.remote_available is not False]
        dropped = len(candidates) - len(live)
        if dropped:
            log.info("%s: dropped %d match(es) confirmed gone", entry.filename, dropped)
        if live:
            chosen_candidate = candidates[chosen_index]
            candidates = live
            entry.candidates = candidates
            chosen_index = candidates.index(chosen_candidate) if chosen_candidate in candidates else 0
        elif dropped and run_deferred_engines("every match was dead"):
            # The fallback engines had been skipped for a match that has
            # since turned out to be a broken link. They get their turn.
            chosen_index = 0
        elif dropped:
            # Every match was dead - report honestly rather than
            # presenting a broken link as a good result.
            entry.candidates = []
            entry.status = MatchStatus.NOT_FOUND
            entry.matched_url = None
            entry.booru_name = None
            entry.similarity = None
            entry.result_source = "fresh"
            entry.searched_without_saucenao = skip_saucenao
            entry.mark_searched()
            apply_outcome_tags(entry, settings)
            _finish_tag_band_measurement(tag_band, entry, settings, all_matches_dead=True)
            if entry.hydrus_hash:
                save_cached_result(entry.hydrus_hash, entry)
            return entry

    entry.select_candidate(chosen_index)

    if errors:
        entry.error_message = "; ".join(errors)  # partial failure, but we still got results

    entry.status = _decide_status(entry, settings)
    entry.result_source = "fresh"
    entry.searched_without_saucenao = skip_saucenao
    entry.mark_searched()
    apply_outcome_tags(entry, settings)
    # Now that entry.tags is final, the snapshot taken above can be
    # completed and written. This is the number _decide_status just used.
    _finish_tag_band_measurement(tag_band, entry, settings)
    if entry.hydrus_hash:
        save_cached_result(entry.hydrus_hash, entry)
    return entry


def _dedupe(candidates: List[MatchCandidate]) -> List[MatchCandidate]:
    """Same picture can come back from both engines (or from IQDB checking
    more than one mirror) - keep the highest-similarity copy per URL."""
    best_by_url: Dict[str, MatchCandidate] = {}
    for c in candidates:
        existing = best_by_url.get(c.url)
        if existing is None or c.similarity > existing.similarity:
            best_by_url[c.url] = c
    return list(best_by_url.values())


def _decide_status(entry: ImageEntry, settings: Settings) -> MatchStatus:
    """GOOD (green) vs POOR (yellow), matching Hatate's original heuristic:
    few retrieved tags, or the matched image looking better/larger than the
    local one, both suggest the user should review it manually."""
    cond = settings.match_conditions
    # Not len(entry.tags): a re-search keeps the by-rule tags a previous
    # search left behind (only the search-engine and booru sources are
    # replaced), so counting them here would let `hatate:few tags` count
    # towards the threshold it is a report of and eventually promote the
    # entry to GOOD. Identical to len(entry.tags) when the DAN-77 lists
    # are unconfigured, which is the default - see core/tag_rules.py.
    tag_count = len(countable_tags(entry.tags))

    if tag_count < cond.min_tags_for_good:
        return MatchStatus.POOR

    if cond.require_larger_than_local and entry.local_width and entry.match_width:
        width_gain = (entry.match_width - entry.local_width) / max(entry.local_width, 1) * 100
        height_gain = 0.0
        if entry.local_height and entry.match_height:
            height_gain = (entry.match_height - entry.local_height) / max(entry.local_height, 1) * 100
        if width_gain >= cond.min_width_gain_percent or height_gain >= cond.min_height_gain_percent:
            return MatchStatus.POOR

    return MatchStatus.GOOD


def _load_local_dimensions(entry: ImageEntry):
    try:
        with Image.open(entry.path) as im:
            entry.local_width, entry.local_height = im.size
    except (OSError, ValueError):
        pass
