#!/usr/bin/env python3
"""Measures the under-tagged band from results a real library run already produced.

DAN-55. Read-only: nothing here changes tags, borrowing, or status.

There are two ways to get these numbers and this script is the cheap one.
The other is `HATATE_TAG_BAND_METRICS=<file>` on a live run, which records
the same rows as searches happen (`core/search_engine.py`,
`core/tag_band_metrics.Recorder`) - accurate, but it costs a full
rate-limited pass over the library, tens of hours for a few thousand
images. This script instead reads the saved session, which IS the output
of runs that already happened, and reconstructs the measurement from it.

What the saved data cannot tell us, stated up front because it bounds
every number this prints:

  * `remote_available` is not persisted (`core/models.py:130`), so the
    dead-candidate exclusion at `core/tag_borrowing.py:62` cannot be
    reproduced. The eligible-lender counts are therefore an UPPER BOUND.
  * `booru_tags_fetched` is not persisted either, and only the chosen
    candidate's page is ever read during a search. A lender showing zero
    tags may have no tags or may simply never have been looked at, and the
    two are indistinguishable here. That is why "eligible lender" and
    "lender with recorded tags" are counted separately: the first is what
    the gate allows, the second is what is provably there.

Usage:

    python3 tools/measure_tag_bands.py --out profiling/DAN-55
    python3 tools/measure_tag_bands.py --jsonl run.jsonl --out /tmp/x

The second form summarises a JSONL file recorded by a live run instead,
which has neither caveat above.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import session_db  # noqa: E402
from core.models import MatchStatus  # noqa: E402
from core.paths import SEARCH_CACHE_DIR, SESSION_DB  # noqa: E402
from core.tag_band_metrics import (  # noqa: E402
    BUCKET_ORDER,
    EntryMeasurement,
    format_table,
    load_measurements,
    measure_entry,
    summarize,
    summary_to_dict,
)

# A search that produced a match. NOT_FOUND/ERROR/NOT_SEARCHED entries have
# no chosen candidate, so there is no tag band to put them in - they are
# counted and reported, not measured.
MEASURABLE = (MatchStatus.GOOD, MatchStatus.POOR)


def measure_session(
    db_path: Path, min_tags_for_good: int, slack: float,
) -> tuple[List[EntryMeasurement], Dict[str, int]]:
    """Every matched entry in the saved session, measured.

    The database is copied before it is opened. `session_db._connect`
    applies schema setup, and a measurement has no business writing to the
    user's live session.
    """
    counts: Dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="tag-bands-") as tmp:
        copy = Path(tmp) / "session.db"
        shutil.copy2(db_path, copy)
        entries, skipped = session_db.load_entries(copy)
    counts["session_rows"] = len(entries)
    counts["skipped_corrupt"] = skipped

    out: List[EntryMeasurement] = []
    for entry in entries:
        counts[f"status_{entry.status.value}"] = counts.get(f"status_{entry.status.value}", 0) + 1
        if entry.status not in MEASURABLE or not entry.candidates:
            continue
        out.append(
            measure_entry(
                len(entry.tags),
                entry.candidates,
                entry.selected_candidate_index,
                min_tags_for_good,
                slack,
                liveness_known=False,  # remote_available was never saved
            )
        )
    counts["measured"] = len(out)
    return out, counts


def sample_lender_tags(
    db_path: Path, min_tags_for_good: int, slack: float, *,
    sample_size: int, delay: float, timeout: float, seed: int,
) -> Dict[str, object]:
    """Fetches a sample of would-be lenders and counts what they hold.

    The one question the saved data cannot answer is what a lender would
    actually contribute, because only the chosen candidate's page is ever
    read - so a lender's recorded tag list is empty for reasons that have
    nothing to do with whether tags exist. The module docstring in
    `core/tag_borrowing.py` hit the same wall and resolved it the same
    way: fetch a sample and look.

    Read-only, one request per sampled lender, paced by `delay`. The
    entries sampled are those in the under-tagged band that have an
    eligible lender with nothing recorded on it - i.e. exactly the cases
    where borrowing would fire and the outcome is unknown.
    """
    import random
    import time

    from core.boorus import fetch_page_info, find_parser
    from core.tag_borrowing import candidates_that_may_lend

    with tempfile.TemporaryDirectory(prefix="tag-bands-") as tmp:
        copy = Path(tmp) / "session.db"
        shutil.copy2(db_path, copy)
        entries, _ = session_db.load_entries(copy)

    population = []
    for entry in entries:
        if entry.status not in MEASURABLE or not entry.candidates:
            continue
        band = bucket_for_entry(entry, min_tags_for_good)
        if band != "1..min-1":
            continue
        lenders = candidates_that_may_lend(
            entry.candidates, entry.selected_candidate_index, slack,
        )
        lenders = [c for c in lenders if not c.booru_tags]
        if lenders:
            population.append((entry, lenders[0]))

    random.Random(seed).shuffle(population)
    sample = population[:sample_size]

    results: List[Dict[str, object]] = []
    for i, (_entry, lender) in enumerate(sample):
        site = lender.source_name or "?"
        if find_parser(lender.url) is None:
            # No parser for this site, so borrowing could never read it
            # either. A real outcome, not a failed measurement - and it
            # costs no request, so it does not wait out the delay.
            results.append({"site": site, "tags": 0, "outcome": "no parser"})
        else:
            if i:
                time.sleep(delay)
            try:
                info = fetch_page_info(lender.url, timeout)
                results.append({
                    "site": site,
                    "tags": len(info.tags),
                    "outcome": "ok" if info.tags else "fetched, no tags",
                })
            except Exception as exc:  # noqa: BLE001 - every failure shape is a data point
                results.append({
                    "site": site, "tags": 0,
                    "outcome": f"failed: {type(exc).__name__}",
                })
        # Printed for every row, including the ones that cost no request:
        # a progress log with holes in it reads as a crashed sample.
        print(f"  sampled {i + 1}/{len(sample)}: {results[-1]}", file=sys.stderr)

    with_tags = [r for r in results if int(r["tags"] or 0) > 0]  # type: ignore[call-overload]
    counts = sorted(int(r["tags"] or 0) for r in with_tags)  # type: ignore[call-overload]
    return {
        "band_population": len(population),
        "sampled": len(sample),
        "lenders_with_tags": len(with_tags),
        "tag_counts": counts,
        "mean_tags_when_present": (sum(counts) / len(counts)) if counts else 0.0,
        "median_tags_when_present": counts[len(counts) // 2] if counts else 0,
        "outcomes": _tally(str(r["outcome"]) for r in results),
        "sites": _tally(str(r["site"]) for r in results),
        "seed": seed,
    }


def bucket_for_entry(entry, min_tags_for_good: int) -> str:
    from core.tag_band_metrics import bucket_for

    return bucket_for(len(entry.tags), min_tags_for_good)


def _tally(values) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def top_sites(counter: Dict[str, int], limit: int = 8) -> List[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: -kv[1])[:limit]


def render(
    measurements: List[EntryMeasurement], counts: Dict[str, int], *, source: str,
    sample: Optional[Dict[str, object]] = None,
) -> str:
    by_entry = summarize(measurements, by="entry")
    by_chosen = summarize(measurements, by="chosen_booru")

    lines: List[str] = [
        "# DAN-55 - tag bands and eligible lenders",
        "",
        f"Source: {source}",
        f"Entries measured: {counts.get('measured', 0)} "
        f"of {counts.get('session_rows', 0)} in the session.",
        "",
        format_table(by_entry, title="Bucketed on len(entry.tags) - what _decide_status counts"),
        "",
        format_table(
            by_chosen,
            title="Bucketed on len(chosen.booru_tags) - what the matched post itself supplied",
        ),
        "",
        "## Which sites land in each band (chosen match)",
        "",
    ]
    for name in BUCKET_ORDER:
        s = by_entry[name]
        sites = ", ".join(f"{site} {n}" for site, n in top_sites(s.chosen_sites)) or "-"
        lines.append(f"- `{name}` ({s.entries} entries): {sites}")
    lines += ["", "## Which sites would be ASKED to lend (best eligible lender)", ""]
    for name in BUCKET_ORDER:
        s = by_entry[name]
        sites = ", ".join(f"{site} {n}" for site, n in top_sites(s.eligible_lender_sites)) or "-"
        lines.append(f"- `{name}` ({s.with_eligible_lender} with an eligible lender): {sites}")

    lines += [
        "",
        "## Which sites already have recorded tags to lend",
        "",
        "Almost none, and that is an artefact rather than a finding: a search reads only the",
        "chosen candidate's page, so a lender's saved tag list is empty whether or not tags",
        "exist. The sample below is how that gap gets closed.",
        "",
    ]
    for name in BUCKET_ORDER:
        s = by_entry[name]
        sites = ", ".join(f"{site} {n}" for site, n in top_sites(s.lender_sites)) or "-"
        lines.append(f"- `{name}` ({s.with_tagged_lender} with a tagged lender): {sites}")

    if sample is not None:
        mean_tags_when_present = sample["mean_tags_when_present"]
        assert isinstance(mean_tags_when_present, (int, float))
        lines += [
            "",
            "## Sampled lenders in the 1..min-1 band (fetched live)",
            "",
            f"- band entries with an unfetched eligible lender: {sample['band_population']}",
            f"- sampled: {sample['sampled']} (seed {sample['seed']})",
            f"- lenders that turned out to have tags: {sample['lenders_with_tags']}",
            f"- tags when present: mean {float(mean_tags_when_present):.1f}, "
            f"median {sample['median_tags_when_present']}, counts {sample['tag_counts']}",
            "",
            "Outcomes:",
            "",
        ]
        outcomes = sample["outcomes"]
        assert isinstance(outcomes, dict)
        for outcome, n in sorted(outcomes.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {outcome}: {n}")
        sampled_sites = sample["sites"]
        assert isinstance(sampled_sites, dict)
        lines += ["", "Sites sampled: "
                  + (", ".join(f"{s} {n}" for s, n in top_sites(sampled_sites, 12)) or "-")]

    lines += ["", "## Entry statuses in the session", ""]
    for key, n in sorted(counts.items()):
        if key.startswith("status_"):
            lines.append(f"- {key[len('status_'):]}: {n}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-db", default=str(SESSION_DB), help="saved session to read")
    ap.add_argument("--jsonl", default=None,
                    help="summarise a live-run recording instead of the session")
    ap.add_argument("--min-tags-for-good", type=int, default=None,
                    help="band boundary; default reads the saved config")
    ap.add_argument("--slack", type=float, default=None,
                    help="similarity slack; default reads the saved config")
    ap.add_argument("--out", default=None, help="directory for report.md + summary.json")
    ap.add_argument("--search-cache", default=str(SEARCH_CACHE_DIR),
                    help="search-cache directory, counted for context only")
    ap.add_argument("--sample-lenders", type=int, default=0,
                    help="fetch this many would-be lenders in the 1..min-1 band and count "
                         "their tags. Costs one request each; 0 (the default) fetches nothing")
    ap.add_argument("--sample-delay", type=float, default=3.0,
                    help="seconds between sampled fetches")
    ap.add_argument("--sample-timeout", type=float, default=30.0)
    ap.add_argument("--sample-seed", type=int, default=20550,
                    help="fixed so a sample can be re-taken identically")
    args = ap.parse_args(argv)

    min_tags = args.min_tags_for_good
    slack = args.slack
    if min_tags is None or slack is None:
        # Read the user's real settings rather than the dataclass defaults:
        # measuring against a boundary the app is not using answers nothing.
        from core.config import Settings

        settings = Settings.load()
        if min_tags is None:
            min_tags = settings.match_conditions.min_tags_for_good
        if slack is None:
            slack = getattr(settings, "borrow_tags_similarity_slack", 0.0)

    sample: Optional[Dict[str, object]] = None
    if args.jsonl:
        measurements = load_measurements(args.jsonl)
        counts = {"measured": len(measurements), "session_rows": len(measurements)}
        source = f"live-run recording {args.jsonl}"
    else:
        db = Path(args.session_db)
        if not db.exists():
            print(f"no saved session at {db}", file=sys.stderr)
            return 2
        measurements, counts = measure_session(db, min_tags, slack)
        source = (
            f"saved session {db} (min_tags_for_good={min_tags}, slack={slack}); "
            f"search_cache holds {_cache_size(args.search_cache)} entries"
        )
        if args.sample_lenders > 0:
            print(f"sampling {args.sample_lenders} lender pages "
                  f"({args.sample_delay}s apart)", file=sys.stderr)
            sample = sample_lender_tags(
                db, min_tags, slack,
                sample_size=args.sample_lenders, delay=args.sample_delay,
                timeout=args.sample_timeout, seed=args.sample_seed,
            )

    report = render(measurements, counts, source=source, sample=sample)
    print(report)

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(report, encoding="utf-8")
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "source": source,
                    "min_tags_for_good": min_tags,
                    "borrow_tags_similarity_slack": slack,
                    "counts": counts,
                    "lender_sample": sample,
                    "by_entry_tags": summary_to_dict(summarize(measurements, by="entry")),
                    "by_chosen_booru_tags": summary_to_dict(
                        summarize(measurements, by="chosen_booru")
                    ),
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {out / 'report.md'} and {out / 'summary.json'}", file=sys.stderr)
    return 0


def _cache_size(path: str) -> int:
    try:
        return len(os.listdir(path))
    except OSError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
