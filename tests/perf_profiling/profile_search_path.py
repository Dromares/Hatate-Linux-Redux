"""DAN-27 Stage A: profiles the local, CPU-bound search/matching path
against a synthetic library.

Not a test - deliberately named so `unittest discover` (pattern
`test*.py`) never picks it up, and slow enough (minutes, at the larger
library sizes) that it must never run as part of the normal suite or
count against the coverage floor in .coveragerc.

Measure-only. Nothing here changes core/ or gui/ behaviour; it only calls
the real functions with synthetic data and records how long they took.

Usage:
    venv/bin/python3 -m tests.perf_profiling.profile_search_path --size 1000
    venv/bin/python3 -m tests.perf_profiling.profile_search_path --size 50000 --no-cprofile

Network is never touched: search_image() itself is not called (it would
hit IQDB/SauceNAO/booru hosts). Instead this drives the pieces of the
search/matching path that run after a network response would have
arrived - the CPU-bound comparison, ranking, caching and table-model
work - with synthetic "already downloaded" candidate thumbnails standing
in for what a real search would have fetched.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import os
import pstats
import random
import resource
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.perf_profiling.generate_library import generate_library, LibraryEntry  # noqa: E402

from core import entry_filter as entry_filter_mod  # noqa: E402
from core import image_prep  # noqa: E402
from core import lru_cache as lru_cache_mod  # noqa: E402
from core import ranking  # noqa: E402
from core import search_cache  # noqa: E402
from core import session_db  # noqa: E402
from core.config import Settings  # noqa: E402
from core.image_compare import local_prints  # noqa: E402
from core.models import ImageEntry, MatchCandidate, MatchStatus  # noqa: E402
from core.similarity_check import measure_ordinal_similarities  # noqa: E402

from gui.image_table_model import ImageTableModel, sort_key_for_column, COLUMNS  # noqa: E402

# The 10 modules DAN-27 named as the path to measure. Recorded here (not
# just in the report) so a diff of this list against the issue is exact,
# not remembered.
IN_SCOPE_MODULES = [
    "core/search_engine.py", "core/image_compare.py", "core/similarity_check.py",
    "core/image_prep.py", "core/ranking.py", "core/search_cache.py",
    "core/lru_cache.py", "core/session_db.py", "core/entry_filter.py",
    "gui/image_table_model.py",
]

# A few candidate URLs whose host ranking.find_parser() recognises, so
# quality_bonus's PARSER_BONUS branch is actually exercised rather than
# always taking the "no parser" path.
CANDIDATE_HOSTS = [
    "https://danbooru.donmai.us/posts/{n}",
    "https://gelbooru.com/index.php?page=post&s=view&id={n}",
    "https://www.pixiv.net/en/artworks/{n}",
    "https://example-unrecognised-host.test/post/{n}",
]

MAX_CANDIDATES_PER_ENTRY = 3
LRU_CACHE_CAP = 200  # matches the ballpark of a real thumbnail icon cache


@contextmanager
def stage(stages: Dict[str, float], name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        stages[name] = stages.get(name, 0.0) + (time.perf_counter() - start)


def _file_hash(path: str) -> str:
    # Stands in for hydrus_tag_lookup.hash_file (out of scope: not one of
    # the 10 named modules) - any stable per-file key does for exercising
    # search_cache, which only cares that the key is a string.
    return hashlib.sha256(path.encode()).hexdigest()


THUMB_MAX_DIMENSION = 300  # a search engine's own preview is small; using
                           # the source file's full bytes here would both
                           # misrepresent a real search result's memory
                           # footprint and make dhash's resize step do
                           # needless work on multi-megapixel input


def _make_thumb_bytes(path: str) -> bytes:
    from PIL import Image
    with Image.open(path) as im:
        im.thumbnail((THUMB_MAX_DIMENSION, THUMB_MAX_DIMENSION), Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=85)
        return buf.getvalue()


def build_candidates(rng: random.Random, entry: LibraryEntry, pool: List[LibraryEntry],
                      by_path: Dict[str, LibraryEntry]) -> List[MatchCandidate]:
    """Synthetic "already downloaded" candidates for one entry - standing
    in for what a real search's IQDB/SauceNAO/ascii2d responses plus a
    booru-page fetch would have produced by the time the CPU-bound path
    takes over. thumb_bytes is pre-filled so measure_ordinal_similarities
    never calls remote.download_bytes; it's a resized preview, not the
    source file's own bytes, matching what an engine's thumbnail actually
    is (`pool`/`by_path` are precomputed once by the caller - rebuilding
    a library-sized dict per entry would make candidate-building itself
    quadratic in the library size, contaminating the very thing being
    measured)."""
    candidates = []
    sources = [entry.near_duplicate_of] if entry.near_duplicate_of else []
    n = rng.randint(1, MAX_CANDIDATES_PER_ENTRY)
    while len(sources) < n:
        sources.append(rng.choice(pool).path)

    for i, source_path in enumerate(sources[:n]):
        src = by_path.get(source_path, entry)
        thumb_bytes = _make_thumb_bytes(src.path)
        # Roughly matches the real mix: IQDB/SauceNAO already carry a real
        # similarity score (reports_real_similarity == True); ascii2d and
        # the Google engines report position only, so they're the ones
        # measure_ordinal_similarities has to do work for.
        if i == 0 and rng.random() < 0.4:
            engine, measured, similarity = "IQDB", True, rng.uniform(60, 99)
        else:
            engine, measured, similarity = "ascii2d", False, rng.uniform(50, 95)
        host = rng.choice(CANDIDATE_HOSTS).format(n=rng.randint(1, 999999))
        candidates.append(MatchCandidate(
            url=host, source_name=None, thumb_url=host, thumb_bytes=thumb_bytes,
            similarity=similarity, similarity_measured=measured,
            width=src.width, height=src.height, engine=engine,
        ))
    return candidates


def run_pipeline(entries_meta: List[LibraryEntry], cache_dir: Path, session_path: Path,
                  seed: int) -> Dict[str, float]:
    rng = random.Random(seed)
    settings = Settings()
    stages: Dict[str, float] = {}

    search_cache.SEARCH_CACHE_DIR = cache_dir  # scratch dir, never the user's real one

    entries: List[ImageEntry] = []
    for meta in entries_meta:
        with stage(stages, "read_dimensions"):
            from PIL import Image
            try:
                with Image.open(meta.path) as im:
                    w, h = im.size
            except OSError:
                w, h = None, None
        entry = ImageEntry(path=meta.path, local_width=w, local_height=h)
        entry.hydrus_hash = _file_hash(meta.path)
        entries.append(entry)

    # -- cache: miss path (nothing written yet) -------------------------
    for entry in entries:
        with stage(stages, "cache_load_miss"):
            search_cache.load_cached_result(entry.hydrus_hash)

    # -- the per-entry comparison/ranking/prep pipeline ------------------
    # Precomputed once: see build_candidates' docstring on why rebuilding
    # these per entry would make this harness itself O(n^2).
    by_path = {e.path: e for e in entries_meta}
    candidate_pool = entries_meta if len(entries_meta) <= 400 else rng.sample(entries_meta, 400)
    for meta, entry in zip(entries_meta, entries, strict=True):
        candidates = build_candidates(rng, meta, candidate_pool, by_path)

        with stage(stages, "local_prints"):
            prints = local_prints(entry.path)
        # measure_ordinal_similarities recomputes local_prints itself
        # (it isn't given ours) - that duplication is real: search_engine
        # never caches a local fingerprint across the single search it's
        # computed for either. Timed under measure_similarity, not here.
        del prints

        with stage(stages, "measure_similarity"):
            measure_ordinal_similarities(entry, candidates, settings)

        with stage(stages, "rank_candidates"):
            ranked = ranking.rank_candidates(candidates, entry.local_width, entry.local_height, entry.filename)
        entry.candidates = ranked
        if ranked:
            entry.select_candidate(0)
        entry.status = MatchStatus.GOOD if ranked else MatchStatus.NOT_FOUND

        with stage(stages, "prepare_upload"):
            image_prep.prepare_upload_bytes(entry.path)

        with stage(stages, "cache_save"):
            search_cache.save_cached_result(entry.hydrus_hash, entry)

    # -- cache: hit path (re-import / duplicate-add case) ----------------
    hit_sample = entries[: max(1, len(entries) // 5)]
    for entry in hit_sample:
        with stage(stages, "cache_load_hit"):
            search_cache.load_cached_result(entry.hydrus_hash)

    # -- entry_filter: one no-op pass, one real filter ---------------------
    with stage(stages, "entry_filter_noop"):
        entry_filter_mod.EntryFilter().apply(entries)
    real_filter = entry_filter_mod.EntryFilter(statuses={MatchStatus.GOOD}, text="img_0")
    with stage(stages, "entry_filter_active"):
        real_filter.apply(entries)

    # -- session_db: full save (quit), incremental save (autosave), load --
    with stage(stages, "session_save_full"):
        ok, revisions = session_db.save_entries(entries, path=session_path)
    assert ok
    # Simulate an autosave a few seconds later: a small fraction of rows
    # changed (the user reviewed a few), the rest didn't.
    for entry in rng.sample(entries, max(1, len(entries) // 50)):
        entry.reviewed = True
    with stage(stages, "session_save_incremental"):
        session_db.save_entries(entries, last_revisions=revisions, path=session_path, pass_number=1)
    with stage(stages, "session_load"):
        session_db.load_entries(path=session_path)

    # -- lru_cache: a full scroll's worth of icon-cache churn -------------
    cache = lru_cache_mod.LRUCache(LRU_CACHE_CAP)
    with stage(stages, "lru_cache_churn"):
        for entry in entries:
            cache[entry.path] = object()
            if entry.path in cache:
                cache.get(entry.path)

    # -- gui/image_table_model: build, full refresh, render, sort ---------
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QIcon
    app = QApplication.instance() or QApplication([])

    with stage(stages, "gui_table_refresh"):
        model = ImageTableModel(entries, thumbnail_for=lambda e: QIcon())
        model.refresh_all()

    with stage(stages, "gui_table_data_scan"):
        from PyQt6.QtCore import Qt
        for row in range(model.rowCount()):
            for col in range(len(COLUMNS)):
                idx = model.index(row, col)
                model.data(idx, Qt.ItemDataRole.DisplayRole)
                model.data(idx, Qt.ItemDataRole.ForegroundRole)

    with stage(stages, "gui_sort_all_columns"):
        for col in range(len(COLUMNS)):
            key = sort_key_for_column(col)
            sorted(entries, key=key)

    del app
    return stages


def _write_pstats(profiler: cProfile.Profile, out_dir: Path) -> None:
    prof_path = out_dir / "profile.prof"
    profiler.dump_stats(str(prof_path))
    stats = pstats.Stats(str(prof_path))
    stats.sort_stats("cumulative")
    with (out_dir / "top20_cumulative.txt").open("w") as fh:
        stats.stream = fh
        stats.print_stats(20)
    stats.sort_stats("tottime")
    with (out_dir / "top20_tottime.txt").open("w") as fh:
        stats.stream = fh
        stats.print_stats(20)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--library-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-cprofile", action="store_true",
                         help="wall-clock only - for sizes where cProfile's "
                              "per-call overhead makes a full profile infeasible")
    parser.add_argument("--keep-library", action="store_true",
                         help="don't delete the generated library afterwards")
    args = parser.parse_args()

    out_dir = args.out or (REPO_ROOT / "profiling" / "DAN-27" / str(args.size))
    out_dir.mkdir(parents=True, exist_ok=True)
    library_root = args.library_root or (REPO_ROOT / ".dan27_scratch" / f"library_{args.size}")
    cache_dir = REPO_ROOT / ".dan27_scratch" / f"cache_{args.size}"
    session_path = REPO_ROOT / ".dan27_scratch" / f"session_{args.size}.db"
    for p in (cache_dir, session_path.parent):
        p.mkdir(parents=True, exist_ok=True)
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True)
    if session_path.exists():
        session_path.unlink()

    print(f"Generating a {args.size}-entry synthetic library under {library_root} ...", flush=True)
    t0 = time.perf_counter()
    entries_meta = generate_library(library_root, args.size, seed=args.seed)
    gen_time = time.perf_counter() - t0
    print(f"Generated {len(entries_meta)} entries in {gen_time:.1f}s", flush=True)

    print(f"Running the pipeline (cProfile={'off' if args.no_cprofile else 'on'}) ...", flush=True)
    profiler = None if args.no_cprofile else cProfile.Profile()
    t0 = time.perf_counter()
    if profiler:
        profiler.enable()
    stages = run_pipeline(entries_meta, cache_dir, session_path, args.seed)
    if profiler:
        profiler.disable()
    total_wall = time.perf_counter() - t0
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    result = {
        "size": args.size,
        "generation_seconds": gen_time,
        "total_pipeline_seconds": total_wall,
        "stage_seconds": stages,
        "peak_rss_kb": peak_rss_kb,
        "cprofile": profiler is not None,
    }
    (out_dir / "summary.json").write_text(json.dumps(result, indent=2))

    lines = [
        f"DAN-27 profile: size={args.size} cprofile={'on' if profiler else 'off'}",
        f"generation: {gen_time:.2f}s",
        f"total pipeline wall time: {total_wall:.2f}s",
        f"peak RSS: {peak_rss_kb / 1024:.1f} MB",
        "",
        "per-stage wall time (summed across all entries):",
    ]
    for name, seconds in sorted(stages.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:28s} {seconds:10.3f}s  ({seconds / max(total_wall, 1e-9) * 100:5.1f}%)")
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    if profiler:
        _write_pstats(profiler, out_dir)
        print(f"pstats written to {out_dir}")

    if not args.keep_library:
        shutil.rmtree(library_root, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)
        if session_path.exists():
            session_path.unlink()


if __name__ == "__main__":
    main()
