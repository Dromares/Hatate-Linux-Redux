"""Which image of a multi-image post the local file is.

Split out of core/search_engine.py; everything here is re-exported there.
"""
from __future__ import annotations




from .applog import get_logger
from .image_compare import (
    image_dimensions,
)
from .multipage import (
    MAX_PAGES_TO_COMPARE, check_page, choose_matching_page, dimensions_contradict_page,
    dimensions_identify_page, page_index_from_thumbnail, page_url_for_index,
)

from . import remote

log = get_logger("search")


def _use_page(candidate, pages: list, index: int, page_count: int, why: str):
    """Repoints a candidate at one page of its post.

    Page 1 is repointed too, rather than returned from early. The first
    page is the default for a post whose page nobody named - but not for
    one whose URL named a page itself (a tweet's /photo/3), and there the
    early return left the candidate on that page while the images had just
    said page 1. Repointing is a no-op wherever the default really was
    page 1, since it writes back the very same URLs and dimensions.
    """
    chosen = pages[index].get("urls") or {}
    candidate.direct_file_url = (
        chosen.get("original")
        or page_url_for_index(candidate.direct_file_url or "", index)
        or candidate.direct_file_url
    )
    candidate.preview_url = chosen.get("regular") or chosen.get("small") or candidate.preview_url
    candidate.page_index = index
    # The dimensions from the artwork record describe page 1, so they no
    # longer apply. This page's own are right there in the page list, so
    # use them; failing that, absent beats confidently wrong, since the
    # size comparison and the Size Difference column both build on them.
    candidate.width = pages[index].get("width")
    candidate.height = pages[index].get("height")
    if index == 0:
        log.debug("%s: the local image is page 1 of %d after all (%s)",
                  candidate.url, page_count, why)
        return
    log.info(
        "%s: %s - using page %d of %d instead of the first",
        candidate.url, why, index + 1, page_count,
    )


def _resolve_multipage_candidate(candidate, page_count: int, settings, local_path: str,
                                 page_info=None):
    """Points a multi-page post's candidate at the page that actually
    matches the local file, instead of its first page.

    A Pixiv artwork can hold many images under one URL and its API
    describes only the first, so a match on page 5 was shown and
    downloaded as page 1 - the wrong picture, with nothing to indicate
    it. Two independent signals say which page it really was:

    - SauceNAO names the page in its own thumbnail URL, free with the
      result we already have. It can be stale, so it is checked against
      the live page list before it's believed, and never overrules the
      comparison below.
    - Each page's thumbnail, hashed and compared against the local file.
      Authoritative but costs a download per page.

    So the dimensions in the page list are tried first (they often settle
    it outright), then hashing, and SauceNAO's claim breaks a tie that
    hashing reports it cannot.

    Past MAX_PAGES_TO_COMPARE there's no hashing the whole post, but a
    page something has already named can still be checked on its own for
    one download - which is what makes a 111-image post resolvable
    instead of abandoned on page 1.

    Leaves the candidate untouched whenever the answer isn't clear:
    being wrong about this is exactly the problem being fixed, and the
    first page is at least a predictable default.
    """
    from .boorus import find_parser, pixiv

    # Any parser that can list a post's pages gets this, not just Pixiv.
    # The work below is generic - it asks "which of these images is the
    # one I have?" and the answer comes from hashing, which does not care
    # what site the pages came from. A parser opts in by exposing
    # pages_api_url() and parse_pages(); everything else falls straight
    # through and keeps its single image.
    parser = find_parser(candidate.url)
    pages_api_url = getattr(parser, "pages_api_url", None)
    parse_pages = getattr(parser, "parse_pages", None)
    if pages_api_url is None or parse_pages is None:
        return

    pages_url = pages_api_url(candidate.url)
    if not pages_url:
        return

    # SauceNAO names the page in its own thumbnail URL, but only for
    # Pixiv - the pattern is that index's, and reading another site's
    # thumbnail through it would invent a page number out of nothing.
    # Sites without the hint simply rely on hashing, which is the
    # authoritative signal anyway.
    reported = None
    if parser is pixiv:
        reported = page_index_from_thumbnail(
            candidate.thumb_url, pixiv.illust_id(candidate.url or ""),
        )
    # The page list is fetched even for posts far past the hashing cap.
    # It's one small API call, and it's what makes those posts resolvable
    # at all - both to bounds-check a reported page and to spot a page
    # the local file's size singles out. They're not a rare case: a tenth
    # of the multi-page results measured sat beyond the cap, and every
    # one of them was silently left on page 1 before.
    log.debug("%s has %d pages - working out which one matches", candidate.url, page_count)
    if page_info is not None and page_info.body and page_info.fetched_url == pages_url:
        # The page list IS the record just fetched (Pawchive's post API) -
        # no second request for the same URL.
        body = page_info.body
    else:
        body = remote.fetch_text(pages_url, settings, referer=candidate.url)
    if not body:
        log.debug("Could not read the page list for %s; keeping the first page", candidate.url)
        return

    pages = parse_pages(body, candidate.url)
    if len(pages) < 2:
        return

    # Some sites name each file by its SHA-256 (pawchive's paths ARE the
    # hash). Then the page holding the local file is simply the one with
    # its hash - certain, and not a single thumbnail downloaded.
    page_hashes = [p.get("sha256") for p in pages]
    if any(page_hashes):
        try:
            import hashlib
            digest = hashlib.sha256()
            with open(local_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            local_sha256 = digest.hexdigest()
        except OSError:
            local_sha256 = None
        if local_sha256 in page_hashes:
            _use_page(candidate, pages, page_hashes.index(local_sha256), page_count,
                      "it is the very same file (its SHA-256 matches)")
            return
        # Not the same file, but the engine that found it matched ONE of
        # the post's images - and its thumbnail names that image by hash.
        # (Pawchive's exact lookup and its local index both do this.)
        named = next((h for h in page_hashes if h and h in (candidate.thumb_url or "")), None)
        if named:
            _use_page(candidate, pages, page_hashes.index(named), page_count,
                      "the image the search matched is this one (named by its thumbnail)")
            return

    if reported is not None and reported >= len(pages):
        # The post has been edited since SauceNAO indexed it. Seen in the
        # wild, so it's a real case rather than defensive padding.
        log.info(
            "%s: SauceNAO reports page %d but the post now has %d - its index is stale, "
            "ignoring it", candidate.url, reported + 1, len(pages),
        )
        reported = None

    local_size = image_dimensions(local_path)
    page_sizes = [(p.get("width"), p.get("height")) for p in pages]

    if reported is not None and dimensions_contradict_page(local_size, page_sizes, reported):
        log.info(
            "%s: SauceNAO reports page %d, but that page isn't the local image's shape - "
            "ignoring it", candidate.url, reported + 1,
        )
        reported = None

    # Cheapest possible answer: if exactly one page is the local file's
    # size and that's the page SauceNAO named, two independent sources
    # agree and there's nothing left to establish. Skips a download per
    # page - measured agreeing with hashing in every case it fired.
    # `local_size` is checked here rather than relied upon: the call
    # below returns None for a falsy one, so the branch is already
    # unreachable without it - but only by way of a correlation between
    # two functions, which is the sort of thing that stops being true
    # when one of them is edited.
    if (
        reported is not None and local_size
        and dimensions_identify_page(local_size, page_sizes) == reported
    ):
        _use_page(
            candidate, pages, reported, page_count,
            f"SauceNAO's thumbnail names page {reported + 1} and it's the only page at "
            f"{local_size[0]}x{local_size[1]}",
        )
        return

    if page_count > MAX_PAGES_TO_COMPARE:
        # Too many pages to ask which one matches, but not too many to
        # ask whether a particular one does - that costs a single
        # download however big the post is. Worth trying for any page
        # something has already named: SauceNAO's, or one the page list
        # singles out by size.
        suspect = dimensions_identify_page(local_size, page_sizes)
        for index in dict.fromkeys(i for i in (reported, suspect) if i is not None):
            urls = pages[index].get("urls") or {}
            data = remote.download_bytes(
                urls.get("small") or urls.get("regular"), settings.hydrus.timeout,
                referer=candidate.url,
            )
            if not data:
                continue
            check = check_page(local_path, data)
            if check.confirmed:
                _use_page(
                    candidate, pages, index, page_count,
                    f"page {index + 1} was checked directly and {check.reason}",
                )
                return
            log.debug(
                "%s: page %d isn't it - %s", candidate.url, index + 1, check.reason,
            )
        log.info(
            "%s has %d pages - too many to compare them all, and no single page could be "
            "confirmed, so keeping the first", candidate.url, page_count,
        )
        return

    thumbnails = []
    for index, page in enumerate(pages[:MAX_PAGES_TO_COMPARE]):
        urls = page.get("urls") or {}
        thumb_url = urls.get("small") or urls.get("regular")
        if not thumb_url:
            continue
        data = remote.download_bytes(thumb_url, settings.hydrus.timeout, referer=candidate.url)
        if data:
            thumbnails.append((index, data))

    match = choose_matching_page(local_path, thumbnails)

    if match is not None and match.confident:
        # Hashing wins outright where it has an answer, including against
        # SauceNAO: the one measured disagreement was a stale index, and
        # hashing had it right.
        if reported is not None and reported != match.index:
            log.info(
                "%s: SauceNAO's thumbnail names page %d but the images say page %d - "
                "going with the images", candidate.url, reported + 1, match.index + 1,
            )
        _use_page(candidate, pages, match.index, page_count, match.reason)
        return

    if reported is None:
        if match is not None:
            log.info("Keeping page 1 of %s - %s", candidate.url, match.reason)
        return

    # Hashing couldn't choose. SauceNAO's claim is worth acting on only
    # where the refusal was a tie between pages that look alike - the
    # case it's uniquely placed to settle, and where it was measured
    # agreeing with the images wherever they could speak at all. When
    # nothing in the post resembles the local file, the post is the wrong
    # one or the image has been re-cropped, and a page number from
    # anywhere is a guess; those refusals came almost entirely from
    # matches SauceNAO itself scored around 60% similar.
    if match is not None and not match.indistinguishable:
        log.info(
            "Keeping page 1 of %s - %s, so SauceNAO's page %d isn't worth acting on either",
            candidate.url, match.reason, reported + 1,
        )
        return

    _use_page(
        candidate, pages, reported, page_count,
        "the pages are too alike to tell apart, so going with the page SauceNAO's thumbnail "
        f"names ({reported + 1})" if match is not None
        else f"SauceNAO's thumbnail names page {reported + 1} and the pages couldn't be compared",
    )
