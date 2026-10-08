"""Core data models shared across the app."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TagSource(Enum):
    """Where a tag came from, mirrors Hatate's original source list."""
    USER = "User"
    SEARCH_ENGINE = "Search engine"   # IQDB / SauceNAO unnamespaced tags
    BOORU = "Booru"                   # parsed from the matched booru page
    HATATE = "Hatate-linux"           # auto-added by this program
    HYDRUS = "Hydrus"                 # already on the file in Hydrus, auto-imported on add

    @classmethod
    def default_enabled(cls) -> List["TagSource"]:
        """All sources are enabled by default except the search engine one,
        matching the original Hatate behaviour."""
        return [cls.USER, cls.HYDRUS, cls.BOORU, cls.HATATE]


class MatchStatus(Enum):
    NOT_SEARCHED = "not_searched"   # hasn't been searched yet
    GOOD = "good"                   # found with good tags
    POOR = "poor"                   # found but few tags / local looks better
    NOT_FOUND = "not_found"         # no match on IQDB/SauceNAO
    ERROR = "error"                 # search failed (network, parse error, ...)
    SEARCHING = "searching"         # currently being processed

    @property
    def label(self) -> str:
        return {
            MatchStatus.NOT_SEARCHED: "Not searched",
            MatchStatus.GOOD: "Found (good)",
            MatchStatus.POOR: "Found (review)",
            MatchStatus.NOT_FOUND: "Not found",
            MatchStatus.ERROR: "Error",
            MatchStatus.SEARCHING: "Searching...",
        }[self]

    @property
    def sort_rank(self) -> int:
        """Ordering used when sorting the table by Status: found matches
        first (good ahead of needs-review), then not-found, then errors,
        with in-progress/unprocessed rows last."""
        return {
            MatchStatus.GOOD: 0,
            MatchStatus.POOR: 1,
            MatchStatus.NOT_FOUND: 2,
            MatchStatus.ERROR: 3,
            MatchStatus.SEARCHING: 4,
            MatchStatus.NOT_SEARCHED: 5,
        }[self]


@dataclass
class Tag:
    name: str
    source: TagSource
    namespace: Optional[str] = None  # e.g. "character", "artist", "copyright"

    @property
    def display(self) -> str:
        return f"{self.namespace}:{self.name}" if self.namespace else self.name

    def key(self):
        return (self.namespace, self.name)


@dataclass
class MatchCandidate:
    """One candidate match from IQDB or SauceNAO - a picture on some site
    that might be the source of the local image. An ImageEntry can hold
    several of these so the user can compare and pick for themselves."""
    url: str
    source_name: Optional[str] = None
    thumb_url: Optional[str] = None
    thumb_bytes: Optional[bytes] = None
    similarity: float = 0.0
    similarity_measured: bool = False           # whether `similarity` is a real comparison of the
                                                # two pictures rather than the engine's own number.
                                                # ascii2d and the Google engines report no similarity
                                                # at all and number their results by position, so
                                                # theirs is measured here instead - see
                                                # core/image_compare.py
    width: Optional[int] = None
    height: Optional[int] = None
    engine: str = "IQDB"                      # "IQDB" or "SauceNAO"
    engine_tags: List["Tag"] = field(default_factory=list)   # tags from the search engine itself
    booru_tags: List["Tag"] = field(default_factory=list)    # tags scraped from its page, once fetched
    booru_tags_fetched: bool = False           # whether we've tried pulling tags from its page yet
    direct_file_url: Optional[str] = None       # the actual image file, as opposed to the booru's HTML post page
    preview_url: Optional[str] = None           # mid-resolution "sample" image, if the site offers one
    page_index: int = 0                         # which image of a multi-image post this refers to.
                                                 # Pixiv artworks can hold dozens under one URL and
                                                 # their API describes only the first, so this
                                                 # records when a later page was identified as the
                                                 # actual match - much
                                                 # sharper than the search engine's own tiny thumbnail, without
                                                 # the cost of downloading the full original just for display
    remote_format: Optional[str] = None        # e.g. "JPEG", "PNG" - from the source URL's Content-Type
    remote_size_bytes: Optional[int] = None    # from the source URL's Content-Length
    remote_info_fetched: bool = False          # whether we've tried a HEAD request yet (may still have failed)
    tags_borrowed_from: Optional[str] = None   # URL of the match that lent these tags, when this
                                                # one had none of its own. Recorded rather than left
                                                # implicit so the tag list can say whose tags these
                                                # actually are
    incomplete_reason: Optional[str] = None    # why this match came back missing its tags, its file,
                                                # or both - when the parser knows. NOT a reason to drop
                                                # it: the match itself is real, and whatever DID arrive
                                                # is still worth having
    restricted: Optional[str] = None           # the site has this post but won't show it without a paid
                                                # account tier (Danbooru Gold), with a short reason.
                                                # Distinct from remote_available: the post isn't gone,
                                                # it's gated, and saying "dead link" would be wrong
    remote_available: Optional[bool] = None    # True: the source URL responded OK. False: it responded with a
                                                # definite "gone" (404/410) - the post was deleted or removed.
                                                # None: unknown - not checked yet, or the check was inconclusive
                                                # (network error, timeout, or a server that doesn't answer HEAD),
                                                # which must NOT be treated as "gone".
    availability_checked_at: Optional[float] = None  # when remote_available was last decided, so a saved
                                                      # verdict can age out rather than stand forever.
                                                      # Set iff remote_available is definite - see
                                                      # record_availability, which writes both together

    def record_availability(self, available: Optional[bool]) -> None:
        """Stores an availability verdict together with when it was reached.

        Both go through one place so the date cannot drift from the
        verdict it dates. A saved verdict is only trusted for as long as
        core/search_cache.py's expiry window says it is, and an undated
        one would either never expire or expire immediately depending on
        which way that check guessed.

        An inconclusive check (None) clears the date too: "unknown as of
        last Tuesday" is not a fact worth keeping, and unknown is exactly
        the state of a candidate nobody has ever checked.
        """
        self.remote_available = available
        self.availability_checked_at = time.time() if available is not None else None


@dataclass
class ImageEntry:
    path: str
    hydrus_file_id: Optional[int] = None
    hydrus_hash: Optional[str] = None
    status: MatchStatus = MatchStatus.NOT_SEARCHED
    candidates: List[MatchCandidate] = field(default_factory=list)
    selected_candidate_index: int = 0
    matched_url: Optional[str] = None
    matched_thumb_url: Optional[str] = None
    matched_thumb_bytes: Optional[bytes] = None
    booru_name: Optional[str] = None
    similarity: Optional[float] = None
    similarity_measured: bool = False  # whether `similarity` above is a real comparison of the two
                                        # pictures. Mirrors the selected candidate's own flag, because
                                        # every display surface reads the entry rather than the
                                        # candidate, and without it an ascii2d/Google ordinal position
                                        # is shown in exactly the format IQDB's measured score uses.
                                        # False for an entry with no match at all, which has no number
                                        # to qualify.
    match_width: Optional[int] = None
    match_height: Optional[int] = None
    local_width: Optional[int] = None
    local_height: Optional[int] = None
    tags: List[Tag] = field(default_factory=list)
    error_message: Optional[str] = None
    last_searched: Optional[float] = None
    file_missing: bool = False  # the file wasn't on disk when the list was last checked, most
                                 # often because it was deleted from Hydrus (which removes it from
                                 # Hydrus's file store). Deliberately NOT saved in the session: it
                                 # describes the disk right now, not the list, so it's re-checked
                                 # on load rather than restored from a possibly-stale record
    sent_to_hydrus: bool = False
    hydrus_import_confirmed: bool = False  # Hydrus actually acknowledged holding the file, as opposed to
                                            # merely accepting the request. True for the upload and
                                            # download+send paths (Hydrus returns a hash synchronously);
                                            # for the URL-importer path only once the confirmation poll
                                            # verifies it, since Hydrus's downloader runs asynchronously.
                                            # Deliberately NOT inferred from hydrus_hash: that's just the
                                            # file's SHA256, computed for every added file during duplicate
                                            # detection, and says nothing about whether Hydrus has it.
    reviewed: bool = False  # the user looked at this result and decided against sending it -
                             # the local copy is better, or the match is wrong. Distinct from
                             # sent_to_hydrus, which records the other decision. Without this
                             # the two outcomes of a review are indistinguishable from a row
                             # nobody has opened yet, so a pass over a large library cannot be
                             # resumed: "not sent" collects the decided and the untouched alike.
                             # Set only by the user, never inferred from a search result.
    searched_without_saucenao: bool = False  # this result came from a run that skipped SauceNAO
                                              # because its daily allowance was spent, so it is
                                              # weaker than a full search would have produced. Kept
                                              # out of the result cache and re-searchable in one
                                              # action, so the allowance resetting is enough to get
                                              # the real answer
    result_source: Optional[str] = None  # "cached" or "fresh" - which one produced the current result
    upscale_verdict: Optional[str] = None  # "flagged" or "clear" - the outcome of the last "Check
                                            # for Upscaling" run, None if it has never been run.
                                            # Persisted like Similarity/Size Difference so the check
                                            # doesn't have to be re-run every session.
    upscale_check_detail: Optional[str] = None  # the per-heuristic breakdown behind the verdict
                                                 # (metadata signature, source-size comparison,
                                                 # self-consistency), shown as the Upscale column's
                                                 # tooltip and carried into exports

    # Bumped on every field write (see __setattr__). The session store
    # writes only entries whose revision moved since the last save, which
    # is what makes an autosave cost milliseconds instead of re-encoding
    # the whole list. Not persisted - it describes this run only.
    revision: int = 0

    # Fields that say nothing about what gets SAVED, so writing them must
    # not make an entry look changed. thumb bytes in particular are set
    # for every row a background worker decodes, which would otherwise
    # mark the entire visible list dirty on every scroll.
    _UNTRACKED_FIELDS = frozenset({
        "revision", "matched_thumb_bytes", "file_missing",
    })

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name not in ImageEntry._UNTRACKED_FIELDS:
            object.__setattr__(self, "revision", self.revision + 1)

    def touch(self) -> None:
        """Marks this entry as changed when something INSIDE it moved.

        __setattr__ catches `entry.status = x` and `entry.candidates = []`,
        but not `entry.tags.append(...)` or a write to one of its
        candidates - those mutate objects the entry merely points at. Call
        this at the places that do that; the session store also re-checks
        a rotating slice of the list every save, so a missed call costs a
        delay rather than a lost record.
        """
        object.__setattr__(self, "revision", self.revision + 1)

    @property
    def needs_review(self) -> bool:
        """Whether this row is still waiting on a decision.

        Sending IS a decision, so a sent file is done whether or not it
        was ever explicitly marked - which keeps the flag meaning one
        thing ("looked at, deliberately not sent") instead of drifting
        into a second record of what Hydrus already knows.
        """
        return not self.sent_to_hydrus and not self.reviewed

    @property
    def selected_candidate(self) -> Optional[MatchCandidate]:
        if 0 <= self.selected_candidate_index < len(self.candidates):
            return self.candidates[self.selected_candidate_index]
        return None

    def select_candidate(self, index: int):
        """Switch which candidate match is 'active' - the one shown in the
        preview panel, used for 'open URL', and sent to Hydrus. Also swaps
        in that candidate's own tags (search engine + any booru tags
        already fetched for it), replacing the previously selected
        candidate's tags of those two sources."""
        if not self.candidates or not (0 <= index < len(self.candidates)):
            return
        self.selected_candidate_index = index
        c = self.candidates[index]
        self.matched_url = c.url
        self.matched_thumb_url = c.thumb_url
        self.matched_thumb_bytes = c.thumb_bytes
        self.booru_name = c.source_name
        self.similarity = c.similarity
        self.similarity_measured = c.similarity_measured
        self.match_width = c.width
        self.match_height = c.height
        self.add_tags(c.engine_tags, replace_source=TagSource.SEARCH_ENGINE)
        self.add_tags(c.booru_tags, replace_source=TagSource.BOORU)

    def add_tags(self, new_tags: List[Tag], replace_source: Optional[TagSource] = None):
        if replace_source is not None:
            self.tags = [t for t in self.tags if t.source != replace_source]
        existing = {t.key() for t in self.tags}
        for t in new_tags:
            if t.key() not in existing:
                self.tags.append(t)
                existing.add(t.key())
        # Appending to self.tags mutates a list rather than assigning a
        # field, so __setattr__ never sees it and the session store would
        # take this entry for unchanged. This is the only place in the app
        # that appends to it, so marking here covers every caller.
        self.touch()

    def visible_tags(self, enabled_sources: List[TagSource]) -> List[Tag]:
        return [t for t in self.tags if t.source in enabled_sources]

    def mark_searched(self):
        self.last_searched = time.time()

    def has_result(self) -> bool:
        """Whether a search has produced anything for this entry - a match,
        an error, or a recorded "nothing found". Used to tell an entry
        that would lose something by being reset from one where resetting
        is a no-op."""
        return (
            self.status != MatchStatus.NOT_SEARCHED
            or bool(self.candidates)
            or self.last_searched is not None
            or self.result_source is not None
        )

    def reset_result(self) -> None:
        """Puts this entry back to never-searched, discarding the match and
        everything derived from it.

        Kept deliberately:

        * User and Hydrus tags. The others (search engine, booru, and
          this program's own) came from the result being discarded, so
          they go with it - but a tag someone typed, or one Hydrus
          already holds on the file, was never this search's to remove.
          Hydrus tags in particular cannot be removed from Hydrus by
          dropping them here, so hiding them would just misreport the
          library.
        * sent_to_hydrus / hydrus_import_confirmed. Whether Hydrus holds
          the file is a fact about the library, not about this search.
          Clearing it would claim a file had never been sent while it
          sits in Hydrus, which is worse than the stale match this is
          getting rid of.
        * hydrus_hash and the local dimensions, which describe the local
          file and are unchanged by any of this. Keeping the hash also
          means a re-search does not have to read the file again.
        """
        self.candidates = []
        self.selected_candidate_index = 0
        self.matched_url = None
        self.matched_thumb_url = None
        self.matched_thumb_bytes = None
        self.booru_name = None
        self.similarity = None
        self.similarity_measured = False
        self.match_width = None
        self.match_height = None
        self.error_message = None
        self.last_searched = None
        self.result_source = None
        self.searched_without_saucenao = False
        # The verdict's source-comparison half is derived from the match
        # being discarded here, so keeping it would go on naming a match
        # that no longer exists as evidence.
        self.upscale_verdict = None
        self.upscale_check_detail = None
        # Unlike sent_to_hydrus above, this one DOES clear. "Reviewed"
        # means a judgement was made about a specific match, and that
        # match is what is being thrown away here - leaving the row
        # marked would hide it from the next review pass on the strength
        # of a decision about a result that no longer exists.
        self.reviewed = False
        self.status = MatchStatus.NOT_SEARCHED
        self.tags = [
            t for t in self.tags
            if t.source in (TagSource.USER, TagSource.HYDRUS)
        ]

    def drop_unavailable_candidates(self) -> int:
        """Removes candidates confirmed gone, and returns how many went.

        Only remote_available is False counts - that is the definite
        404/410 reading. None means "not checked, or could not be
        reached", and dropping those would delete a perfectly good match
        over a network hiccup.

        The user's current selection is kept pointing at the SAME match
        wherever it survives. Its index shifts when earlier candidates are
        removed, so the new one is re-derived from the candidate object
        itself rather than reused as a number - keeping the old number
        would silently move the selection to a different match.

        When nothing survives the entry goes back to a no-match state,
        rather than being left with a selection pointing at candidates
        that are no longer there.
        """
        previously_selected = self.selected_candidate
        surviving = [c for c in self.candidates if c.remote_available is not False]
        removed = len(self.candidates) - len(surviving)

        self.candidates = surviving
        if not surviving:
            self.selected_candidate_index = 0
            self.matched_url = None
            self.booru_name = None
            self.similarity = None
            self.similarity_measured = False
            self.status = MatchStatus.NOT_FOUND
            return removed

        # None is possible when nothing was selected, and is treated the
        # same as a selection that did not survive: fall back to the best
        # remaining rather than to whatever now sits at the old index.
        new_index = 0
        if previously_selected is not None and previously_selected in surviving:
            new_index = surviving.index(previously_selected)
        self.select_candidate(new_index)
        return removed

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def enabled_tag_sources(settings: object) -> List[TagSource]:
    """The TagSource values named by settings.enabled_tag_sources.

    Names that don't correspond to a source (a stale config, a rename)
    are dropped rather than raising - the setting is a list of strings
    on disk that nothing validates on load.
    """
    names = getattr(settings, "enabled_tag_sources", None) or []
    return [s for s in TagSource if s.value in names]


def tags_to_send(entry: ImageEntry, settings: Optional[object] = None) -> List[Tag]:
    """The tags that should actually leave this app for a given entry -
    every Hydrus send and every export goes through here.

    Until DAN-72 the tag-source checkboxes only ever filtered the tag
    list widget: a user who unticked "Search engine" still had search
    engine tags written into their Hydrus library on the next send, and
    the README claimed otherwise. Rather than quietly start honouring
    the existing setting - which would change what the next send
    contains for everyone who had ever unticked a box - the filter is
    now a separate opt-in switch, `filter_sent_tags_by_source`, default
    off. Off means what every build before this one did: send the lot.

    When it is on the setting is honoured literally, including the case
    where nothing is ticked, which then sends no tags at all. That is
    deliberately unlike the tag list widget, which falls back to showing
    everything when the list is empty: showing nothing would look like a
    broken panel, whereas a send is a deliberate act and "I unticked
    every source and asked for sends to be filtered" has only one honest
    reading.
    """
    if settings is None or not getattr(settings, "filter_sent_tags_by_source", False):
        return list(entry.tags)
    enabled = enabled_tag_sources(settings)
    return [t for t in entry.tags if t.source in enabled]
