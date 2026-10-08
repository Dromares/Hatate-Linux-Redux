"""Counting how many matches are thin, and whether anything could lend.

Read-and-log only. Nothing here changes what the app does with tags - see
DAN-55. `core/tag_borrowing.py` rescues matches that arrive with NO tags,
and its docstring earns that scope on measured numbers. The band just
above it - a match with one to four tags, still POOR under
`min_tags_for_good` - has never been counted, so there is no equivalent
number to argue from. This module produces it.

Two numbers are recorded per entry, deliberately kept apart:

  * `len(entry.tags)` - the union across every `TagSource`, which is what
    `_decide_status` actually compares to `min_tags_for_good`.
  * `len(chosen.booru_tags)` - only the tags the chosen post itself
    supplied.

They are not the same and conflating them inflates the problem: a 3-tag
booru post that also carried 2 search-engine tags is already GOOD at
`min_tags_for_good = 5`, so it is not in the band at all. Bucketing on
the first number says how many rows the user actually sees as thin;
bucketing on the second says how many posts are thin at the source.

The lender side is measured through the real `candidates_that_may_lend`,
not a re-implementation of it, so the eligible-lender rate reported here
is the rate under today's similarity gate rather than an idealised one.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .applog import get_logger
from .tag_borrowing import candidates_that_may_lend

log = get_logger("tag_band_metrics")

# Set to a file path to turn live recording on. Absent - which is every
# normal run - and nothing in this module is reached at all.
ENV_OUTPUT_PATH = "HATATE_TAG_BAND_METRICS"

BUCKET_TAGLESS = "0"
BUCKET_UNDER_TAGGED = "1..min-1"
BUCKET_WELL_TAGGED = ">=min"

BUCKET_ORDER = (BUCKET_TAGLESS, BUCKET_UNDER_TAGGED, BUCKET_WELL_TAGGED)


def bucket_for(tag_count: int, min_tags_for_good: int) -> str:
    """Which band a tag count falls in.

    The boundary is `min_tags_for_good` rather than a hardcoded 5 because
    it is configurable (`core/config.py:132`, GUI-editable), and a
    measurement taken against the wrong boundary answers a question
    nobody asked.
    """
    if tag_count <= 0:
        return BUCKET_TAGLESS
    if tag_count < min_tags_for_good:
        return BUCKET_UNDER_TAGGED
    return BUCKET_WELL_TAGGED


@dataclass
class EntryMeasurement:
    """One searched entry's tag counts and lender situation.

    Deliberately holds counts and engine/site names only - no filenames,
    URLs or tag text - so a recorded run can be summarised, committed or
    pasted into a task without carrying anyone's library with it.
    """
    # What the status logic sees, and what the post itself supplied.
    entry_tag_count: int = 0
    chosen_booru_tag_count: int = 0
    chosen_engine_tag_count: int = 0
    entry_bucket: str = BUCKET_TAGLESS
    chosen_booru_bucket: str = BUCKET_TAGLESS

    chosen_engine: Optional[str] = None
    chosen_site: Optional[str] = None
    chosen_similarity: Optional[float] = None

    candidate_count: int = 0
    # Under today's gate: how many other matches are close enough to lend.
    eligible_lender_count: int = 0
    # The best eligible lender, whatever it holds - this is the "is there
    # anything there at all" number.
    best_lender_tag_count: int = 0
    best_lender_engine: Optional[str] = None
    best_lender_site: Optional[str] = None
    best_lender_similarity: Optional[float] = None
    # The best eligible lender that actually has recorded tags. Kept
    # separate because a lender with zero recorded tags may simply never
    # have been fetched - only the chosen candidate's page is read - and
    # reporting that as "nothing to lend" would be a guess.
    lenders_with_tags: int = 0
    best_tagged_lender_tag_count: int = 0
    best_tagged_lender_engine: Optional[str] = None
    best_tagged_lender_site: Optional[str] = None
    # True when that lender came from a different engine than the chosen
    # match, so cross-engine and same-engine lending can be told apart.
    tagged_lender_cross_engine: Optional[bool] = None

    # The tagless rescue already ran on this entry. Recorded so the
    # tagless band can be read honestly: post-borrow, a rescued entry no
    # longer looks tagless.
    already_borrowed: bool = False
    # Every match turned out to be dead, so the entry ends NOT_FOUND.
    all_matches_dead: bool = False
    # Whether `remote_available` was knowable here. False when measuring
    # from persisted results, which do not carry it - see
    # `core/tag_borrowing.py:62`, the one gate that cannot then be
    # reproduced, which makes the lender rate an upper bound.
    liveness_known: bool = True


def _site_of(candidate: Any) -> Optional[str]:
    name = getattr(candidate, "source_name", None)
    return str(name) if name else None


def _tag_count(candidate: Any) -> int:
    return len(getattr(candidate, "booru_tags", None) or ())


def measure_entry(
    entry_tag_count: int,
    candidates: Sequence[Any],
    chosen_index: int,
    min_tags_for_good: int,
    slack: float,
    *,
    liveness_known: bool = True,
) -> EntryMeasurement:
    """Measures one entry. Reads only; mutates nothing it is given."""
    m = EntryMeasurement(
        entry_tag_count=entry_tag_count,
        entry_bucket=bucket_for(entry_tag_count, min_tags_for_good),
        candidate_count=len(candidates),
        liveness_known=liveness_known,
    )
    if not candidates or not (0 <= chosen_index < len(candidates)):
        m.all_matches_dead = not candidates
        m.chosen_booru_bucket = bucket_for(0, min_tags_for_good)
        return m

    chosen = candidates[chosen_index]
    m.chosen_booru_tag_count = _tag_count(chosen)
    m.chosen_engine_tag_count = len(getattr(chosen, "engine_tags", None) or ())
    m.chosen_booru_bucket = bucket_for(m.chosen_booru_tag_count, min_tags_for_good)
    m.chosen_engine = getattr(chosen, "engine", None)
    m.chosen_site = _site_of(chosen)
    m.chosen_similarity = getattr(chosen, "similarity", None)
    m.already_borrowed = bool(getattr(chosen, "tags_borrowed_from", None))

    # The real gate, not a copy of it: whatever bounds borrowing today
    # bounds this number too.
    lenders = candidates_that_may_lend(list(candidates), chosen_index, slack)
    m.eligible_lender_count = len(lenders)
    if not lenders:
        return m

    best = lenders[0]
    m.best_lender_tag_count = _tag_count(best)
    m.best_lender_engine = getattr(best, "engine", None)
    m.best_lender_site = _site_of(best)
    m.best_lender_similarity = getattr(best, "similarity", None)

    tagged = [c for c in lenders if _tag_count(c) > 0]
    m.lenders_with_tags = len(tagged)
    if tagged:
        richest = max(tagged, key=_tag_count)
        m.best_tagged_lender_tag_count = _tag_count(richest)
        m.best_tagged_lender_engine = getattr(richest, "engine", None)
        m.best_tagged_lender_site = _site_of(richest)
        m.tagged_lender_cross_engine = (
            getattr(richest, "engine", None) != m.chosen_engine
        )
    return m


@dataclass
class BucketSummary:
    """Aggregate for one band."""
    bucket: str = BUCKET_TAGLESS
    entries: int = 0
    with_eligible_lender: int = 0
    with_tagged_lender: int = 0
    cross_engine_lenders: int = 0
    same_engine_lenders: int = 0
    # Sum of the richest tagged lender's tag count, over the entries that
    # have one. Kept as a sum so the mean can be shown without storing
    # every row.
    lender_tag_total: int = 0
    lender_tag_max: int = 0
    already_borrowed: int = 0
    # Site of the chosen match -> count, so "which sites land in this
    # band" is answerable.
    chosen_sites: Dict[str, int] = field(default_factory=dict)
    # Site of the best ELIGIBLE lender, whether or not it has recorded
    # tags. This is the honest "who would be asked" figure: lender_sites
    # below only sees lenders that happen to have been fetched already,
    # which during a normal search is almost none of them.
    eligible_lender_sites: Dict[str, int] = field(default_factory=dict)
    lender_sites: Dict[str, int] = field(default_factory=dict)

    @property
    def eligible_lender_rate(self) -> float:
        return (self.with_eligible_lender / self.entries) if self.entries else 0.0

    @property
    def tagged_lender_rate(self) -> float:
        return (self.with_tagged_lender / self.entries) if self.entries else 0.0

    @property
    def mean_lender_tags(self) -> float:
        n = self.with_tagged_lender
        return (self.lender_tag_total / n) if n else 0.0


def _accumulate(summary: BucketSummary, m: EntryMeasurement) -> None:
    summary.entries += 1
    if m.already_borrowed:
        summary.already_borrowed += 1
    if m.chosen_site:
        summary.chosen_sites[m.chosen_site] = summary.chosen_sites.get(m.chosen_site, 0) + 1
    if m.eligible_lender_count:
        summary.with_eligible_lender += 1
        if m.best_lender_site:
            site = m.best_lender_site
            summary.eligible_lender_sites[site] = summary.eligible_lender_sites.get(site, 0) + 1
    if m.best_tagged_lender_tag_count > 0:
        summary.with_tagged_lender += 1
        summary.lender_tag_total += m.best_tagged_lender_tag_count
        summary.lender_tag_max = max(summary.lender_tag_max, m.best_tagged_lender_tag_count)
        if m.tagged_lender_cross_engine:
            summary.cross_engine_lenders += 1
        else:
            summary.same_engine_lenders += 1
        if m.best_tagged_lender_site:
            site = m.best_tagged_lender_site
            summary.lender_sites[site] = summary.lender_sites.get(site, 0) + 1


def summarize(
    measurements: Sequence[EntryMeasurement], *, by: str = "entry",
) -> Dict[str, BucketSummary]:
    """Aggregates per band.

    `by="entry"` buckets on `len(entry.tags)` - the number the status
    logic uses, so this answers "how many rows look thin to the user".
    `by="chosen_booru"` buckets on `len(chosen.booru_tags)` - so this
    answers "how many posts are thin at the source". Both are needed and
    they do not agree.
    """
    if by not in ("entry", "chosen_booru"):
        raise ValueError(f"unknown bucketing: {by!r}")
    out = {name: BucketSummary(bucket=name) for name in BUCKET_ORDER}
    for m in measurements:
        key = m.entry_bucket if by == "entry" else m.chosen_booru_bucket
        _accumulate(out[key], m)
    return out


def format_table(summary: Dict[str, BucketSummary], *, title: str) -> str:
    """The summary as a markdown table."""
    lines = [
        f"### {title}",
        "",
        "| band | entries | eligible lender | with tagged lender | "
        "mean lender tags | max | cross-engine | same-engine | already borrowed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in BUCKET_ORDER:
        s = summary[name]
        lines.append(
            f"| `{name}` | {s.entries} | {s.with_eligible_lender} "
            f"({s.eligible_lender_rate * 100:.1f}%) | {s.with_tagged_lender} "
            f"({s.tagged_lender_rate * 100:.1f}%) | {s.mean_lender_tags:.1f} | "
            f"{s.lender_tag_max} | {s.cross_engine_lenders} | {s.same_engine_lenders} | "
            f"{s.already_borrowed} |"
        )
    return "\n".join(lines)


def summary_to_dict(summary: Dict[str, BucketSummary]) -> Dict[str, Any]:
    """JSON-ready, with the derived rates spelled out."""
    out: Dict[str, Any] = {}
    for name, s in summary.items():
        d = asdict(s)
        d["eligible_lender_rate"] = s.eligible_lender_rate
        d["tagged_lender_rate"] = s.tagged_lender_rate
        d["mean_lender_tags"] = s.mean_lender_tags
        out[name] = d
    return out


class Recorder:
    """Appends one JSON object per measured entry.

    JSONL and append-only on purpose: a library run is long, and a run
    that dies halfway should leave the rows it already produced rather
    than an unwritten in-memory list. Every write is flushed for the same
    reason.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.written = 0

    def record(self, measurement: EntryMeasurement) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(measurement), sort_keys=True) + "\n")
            self.written += 1
        except OSError as exc:
            # Instrumentation must never take a search down with it.
            log.warning("could not write tag-band measurement to %s: %s", self.path, exc)

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> Optional["Recorder"]:
        """A recorder when the env var names a file, otherwise None."""
        source = os.environ if env is None else env
        path = (source.get(ENV_OUTPUT_PATH) or "").strip()
        if not path:
            return None
        log.info("recording tag-band measurements to %s", path)
        return cls(path)


def load_measurements(path: str) -> List[EntryMeasurement]:
    """Reads a JSONL file back, skipping any row a killed run truncated."""
    known = set(EntryMeasurement().__dict__)
    out: List[EntryMeasurement] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                log.warning("skipping unreadable measurement row in %s", path)
                continue
            out.append(EntryMeasurement(**{k: v for k, v in row.items() if k in known}))
    return out
