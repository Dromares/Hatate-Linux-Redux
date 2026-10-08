"""Turning the scores engines make up into measured ones, from the
thumbnails they return.

Split out of core/search_engine.py; everything here is re-exported there.
"""
from __future__ import annotations

from typing import List


from PIL import Image

from .applog import get_logger
from .config import Settings
from . import engines as engine_ids
from .image_compare import (
    aligned_similarity, image_dimensions, local_prints,
)
from .models import ImageEntry, MatchCandidate

from . import remote
from .remote import referer_for_candidate

log = get_logger("search")


# A candidate's own thumbnail is enough to compare against: dhash works
# on a downscaled grayscale grid, so a search engine's preview carries
# all the signal the hash uses. Bounded so a long candidate list cannot
# turn into a long queue of downloads.
SIMILARITY_THUMB_WORKERS = 5

# A picture has to carry enough detail to be worth hashing. MEASURED on
# the same Google Lens tile, both ways: with SafeSearch set to blur its
# thumbnail is 0.04 bytes per pixel and hashes an IDENTICAL image to
# 78%; with SafeSearch off the same tile is 0.27 and hashes to 100%. A
# blurred photo lands around 0.07, a real one at 0.19 and up, so the
# threshold sits between them. The browser turns the blur off itself -
# this is the guard for when that has not taken effect.
MIN_THUMB_BYTES_PER_PIXEL = 0.10
MIN_THUMB_PIXELS = 64 * 64


def _worth_hashing(data: bytes) -> bool:
    """Whether this image carries enough detail to compare against.

    A blurred placeholder is still a valid image and still hashes - it
    just hashes to something meaningless. Refusing it leaves the
    candidate with its ordinal score, which is poor information but
    honest, rather than a measurement that is quietly wrong.
    """
    size = image_dimensions(data)
    if not size:
        return False
    pixels = size[0] * size[1]
    if pixels < MIN_THUMB_PIXELS:
        return False
    return (len(data) / pixels) >= MIN_THUMB_BYTES_PER_PIXEL


# How far apart two width:height ratios may be and still be one picture.
# Generous, because a re-upload is often trimmed a little - the case this
# exists for is a wide picture scored as a tall one (1.41 against 0.71).
ASPECT_TOLERANCE = 0.10


def _recheck_borrowed_measurement(candidate: MatchCandidate, page_info, settings: Settings,
                                  local_path: str) -> None:
    """Give a candidate a real, locally-measured score from its own page image.

    Two unrelated cases share the same download-and-compare tail:

    - measure_ordinal_similarities scores a Lens or ascii2d result by the
      thumbnail the engine paired with it. That thumbnail is a picture seen
      ON the page, which is not always the page's own picture - a post page
      shows related posts. CONFIRMED on a real image: a 4093x2894 zerochan
      post was scored 100% from a 1105x1565 related-post thumbnail. The
      site's own dimensions are the tell: when they do not fit the local
      picture's shape, the post's own preview is measured instead.

    - An ordinal engine (Lens, ascii2d, Google Images, Yandex) can also
      leave a candidate with NO measurement at all, when its thumbnail was
      unusable (too small, blurred) - so it keeps the engine's made-up
      ordinal score forever, even for a byte-identical file. CONFIRMED:
      a Lens exact match showed a flat 80% for this reason (see DAN-50).
      Once the booru page is fetched the post's own image is right there,
      so it is measured too rather than left as a placeholder. Skipped for
      engines whose ordinal score is real (IQDB, SauceNAO, ...) - those
      already have a trustworthy measurement, so re-downloading would
      only cost a fetch for nothing.

    Either way, if the post's own image cannot be had, the candidate is
    left/marked unmeasured, since what would be reported was never its
    own picture.
    """
    if candidate.similarity_measured:
        if not (page_info.width and page_info.height):
            return
        try:
            with Image.open(local_path) as im:
                local_w, local_h = im.size
        except (OSError, ValueError):
            return
        if not (local_w and local_h):
            return
        local_ratio = local_w / local_h
        post_ratio = page_info.width / page_info.height
        if abs(post_ratio - local_ratio) / max(local_ratio, post_ratio) <= ASPECT_TOLERANCE:
            return
        reason = ("a thumbnail that isn't its own (it is %dx%d, the local file %dx%d)"
                   % (page_info.width, page_info.height, local_w, local_h))
    elif engine_ids.reports_real_similarity(candidate.engine):
        return
    else:
        reason = "a made-up ordinal position, never actually measured"

    before = candidate.similarity
    source = page_info.preview_url or page_info.file_url
    remeasured = None
    if source:
        data = remote.download_bytes(source, settings.search_timeout,
                              referer=referer_for_candidate(candidate, source))
        if data:
            remeasured, _ = aligned_similarity(local_prints(local_path), data)
    if remeasured is None:
        candidate.similarity_measured = False
        log.info("%s: scored %.0f%% from %s - could not measure its own image, marked "
                 "unmeasured", candidate.url, before, reason)
        return
    candidate.similarity = remeasured
    candidate.similarity_measured = True
    log.info("%s: scored %.0f%% from %s - %.0f%% against its own image",
             candidate.url, before, reason, remeasured)


def measure_ordinal_similarities(
    entry: ImageEntry, candidates: List[MatchCandidate], settings: Settings,
) -> None:
    """Replace made-up similarity scores with measured ones.

    ascii2d, Google Images and Google Lens report no similarity at all -
    the service simply does not provide one - so they number their
    results by position: 80%, 78%, 76%. That number looks exactly like
    IQDB's and SauceNAO's measured ones in the same column, and it means
    nothing.

    Comparing the pictures gives a real answer. Each candidate already
    carries a thumbnail URL, and a perceptual hash is resolution- and
    re-encoding-independent (see core/image_compare.py), so its own
    preview is enough to compare against without downloading originals.

    Candidates whose thumbnail cannot be fetched or hashed keep their
    ordinal score, still flagged as unmeasured - a position in a list is
    poor information, but inventing a measurement would be worse.
    """
    from concurrent.futures import ThreadPoolExecutor

    # Why a candidate was skipped is worth a line each. Silently dropping
    # it leaves no way to tell, after a run, whether a result kept its
    # ordinal score because measuring said so or because measuring never
    # happened - the two look identical in the result row. Each line names
    # the engine and the candidate URL so it can be matched to that row.
    ordinal = [c for c in candidates
               if not c.similarity_measured
               and not engine_ids.reports_real_similarity(c.engine)]
    pending = []
    for candidate in ordinal:
        if candidate.thumb_bytes or candidate.thumb_url:
            pending.append(candidate)
        else:
            log.info("%s: not measuring %s result %s - it arrived with no thumbnail",
                     entry.filename, candidate.engine, candidate.url)
    if not pending:
        return

    # Hashed once, in every shape a thumbnail is compared against - see
    # image_compare.compare_to_local for why one straight hash was not
    # enough (crops, letterboxing and mirrored copies scored far too low).
    prints = local_prints(entry.path)
    if prints is None:
        # INFO, not DEBUG. This one line accounts for EVERY candidate being
        # skipped at once, and at DEBUG it was the only way a run could
        # leave a whole row on ordinal scores with nothing said at the
        # level anybody reads - the gap DAN-51 left deliberately. The
        # per-candidate skips above are all INFO, and this is strictly the
        # larger event of the two.
        log.info("%s: could not hash the local file - leaving ordinal scores alone "
                 "for all %d candidate(s)", entry.filename, len(pending))
        return

    def measure(candidate: MatchCandidate) -> None:
        thumb = candidate.thumb_bytes
        if thumb is None and candidate.thumb_url:
            thumb = remote.download_bytes(
                candidate.thumb_url, settings.search_timeout,
                referer=referer_for_candidate(candidate, candidate.thumb_url),
            )
        if not thumb:
            log.info("%s: not measuring %s result %s - its thumbnail %s came back empty",
                     entry.filename, candidate.engine, candidate.url,
                     candidate.thumb_url or "(supplied with the result)")
            return
        if not _worth_hashing(thumb):
            log.info("%s: not measuring %s result %s - its thumbnail %s carries too little "
                     "detail to hash (%d bytes)", entry.filename, candidate.engine,
                     candidate.url, candidate.thumb_url or "(supplied with the result)",
                     len(thumb))
            return
        measured, mirrored = aligned_similarity(prints, thumb)
        if measured is None:
            return
        if mirrored:
            log.info("%s is the local image mirrored - scored %.0f%%, kept below "
                     "auto-import", candidate.url, measured)
        candidate.similarity = measured
        candidate.similarity_measured = True

    workers = min(len(pending), SIMILARITY_THUMB_WORKERS)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="similarity") as pool:
        list(pool.map(measure, pending))

    measured = [c for c in pending if c.similarity_measured]
    if measured:
        log.info("%s: measured %d/%d ordinal score(s) against the local image",
                 entry.filename, len(measured), len(pending))
