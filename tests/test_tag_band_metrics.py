"""Tests for the tag-band instrumentation (DAN-55).

The point of the instrumentation is a number nobody has yet, so the tests
are mostly about the two traps that would make that number wrong:

  * `len(entry.tags)` and `len(chosen.booru_tags)` are different things.
    Bucketing on the wrong one inflates the band, because a thin post that
    also carried engine tags is already GOOD.
  * the tagless rescue rewrites `chosen.booru_tags`, so a measurement taken
    after it runs sees no tagless entries at all.

Also asserts the instrumentation is genuinely off by default and changes
nothing when on, since it lives in the middle of the search path.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from core.config import Settings
from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
from core.tag_band_metrics import (
    BUCKET_TAGLESS,
    BUCKET_UNDER_TAGGED,
    BUCKET_WELL_TAGGED,
    EntryMeasurement,
    Recorder,
    bucket_for,
    format_table,
    load_measurements,
    measure_entry,
    summarize,
    summary_to_dict,
)


def candidate(url, similarity, booru=(), engine_tags=(), engine="IQDB",
              site=None, available=None, borrowed_from=None):
    return MatchCandidate(
        url=url,
        source_name=site,
        similarity=similarity,
        engine=engine,
        booru_tags=[Tag(t, TagSource.BOORU) for t in booru],
        engine_tags=[Tag(t, TagSource.SEARCH_ENGINE) for t in engine_tags],
        remote_available=available,
        tags_borrowed_from=borrowed_from,
    )


class TestBuckets(unittest.TestCase):
    def test_the_boundary_is_min_tags_for_good_not_a_hardcoded_five(self):
        self.assertEqual(bucket_for(0, 5), BUCKET_TAGLESS)
        self.assertEqual(bucket_for(1, 5), BUCKET_UNDER_TAGGED)
        self.assertEqual(bucket_for(4, 5), BUCKET_UNDER_TAGGED)
        self.assertEqual(bucket_for(5, 5), BUCKET_WELL_TAGGED)
        # A library configured for 10 has a wider band, and the measurement
        # has to follow the setting rather than the default.
        self.assertEqual(bucket_for(5, 10), BUCKET_UNDER_TAGGED)
        self.assertEqual(bucket_for(10, 10), BUCKET_WELL_TAGGED)

    def test_min_of_zero_puts_everything_above_tagless_in_range(self):
        self.assertEqual(bucket_for(0, 0), BUCKET_TAGLESS)
        self.assertEqual(bucket_for(1, 0), BUCKET_WELL_TAGGED)


class TestMeasureEntry(unittest.TestCase):
    def test_entry_tags_and_chosen_booru_tags_are_bucketed_separately(self):
        # The trap DAN-54 flagged: 3 booru tags plus 2 engine tags is GOOD
        # at min_tags_for_good=5, so this row is NOT in the band the user
        # sees - even though the post itself is thin.
        chosen = candidate("https://paheal.net/1", 90.0, booru=("a", "b", "c"),
                           engine_tags=("d", "e"))
        m = measure_entry(5, [chosen], 0, 5, 5.0)
        self.assertEqual(m.entry_bucket, BUCKET_WELL_TAGGED)
        self.assertEqual(m.chosen_booru_bucket, BUCKET_UNDER_TAGGED)
        self.assertEqual(m.entry_tag_count, 5)
        self.assertEqual(m.chosen_booru_tag_count, 3)
        self.assertEqual(m.chosen_engine_tag_count, 2)

    def test_it_uses_the_real_similarity_gate(self):
        chosen = candidate("https://mangadex.org/1", 90.0)
        close = candidate("https://gelbooru.com/1", 87.0, booru=("x", "y"))
        far = candidate("https://danbooru.donmai.us/1", 40.0, booru=("z",) * 50)
        m = measure_entry(0, [chosen, close, far], 0, 5, 5.0)
        self.assertEqual(m.eligible_lender_count, 1)
        self.assertEqual(m.best_lender_tag_count, 2)
        # The 50-tag match is too weak to lend, so it must not show up as
        # what borrowing "would contribute".
        self.assertEqual(m.best_tagged_lender_tag_count, 2)

    def test_zero_slack_excludes_a_weaker_match(self):
        chosen = candidate("https://mangadex.org/1", 90.0)
        close = candidate("https://gelbooru.com/1", 89.0, booru=("x",))
        self.assertEqual(measure_entry(0, [chosen, close], 0, 5, 0.0).eligible_lender_count, 0)
        self.assertEqual(measure_entry(0, [chosen, close], 0, 5, 1.0).eligible_lender_count, 1)

    def test_an_eligible_lender_with_no_recorded_tags_is_counted_apart(self):
        # Only the chosen candidate's page is read during a search, so a
        # lender showing zero tags may just never have been fetched.
        # Reporting that as "nothing to lend" would be a guess.
        chosen = candidate("https://mangadex.org/1", 90.0)
        blank = candidate("https://sankakucomplex.com/1", 90.0)
        m = measure_entry(0, [chosen, blank], 0, 5, 5.0)
        self.assertEqual(m.eligible_lender_count, 1)
        self.assertEqual(m.lenders_with_tags, 0)
        self.assertEqual(m.best_tagged_lender_tag_count, 0)

    def test_cross_engine_lending_is_distinguishable(self):
        chosen = candidate("https://mangadex.org/1", 90.0, engine="SauceNAO")
        lender = candidate("https://gelbooru.com/1", 90.0, booru=("x",), engine="IQDB",
                           site="Gelbooru")
        m = measure_entry(0, [chosen, lender], 0, 5, 5.0)
        self.assertTrue(m.tagged_lender_cross_engine)
        self.assertEqual(m.best_tagged_lender_site, "Gelbooru")

        same = candidate("https://gelbooru.com/2", 90.0, booru=("x",), engine="SauceNAO")
        m2 = measure_entry(0, [chosen, same], 0, 5, 5.0)
        self.assertFalse(m2.tagged_lender_cross_engine)

    def test_the_richest_lender_is_reported_not_merely_the_closest(self):
        chosen = candidate("https://mangadex.org/1", 90.0)
        closest = candidate("https://a.test/1", 90.0, booru=("x",))
        richest = candidate("https://b.test/1", 88.0, booru=("x", "y", "z"))
        m = measure_entry(0, [chosen, closest, richest], 0, 5, 5.0)
        # best_lender_* follows the gate's own ordering (best match first)...
        self.assertEqual(m.best_lender_tag_count, 1)
        # ...while the contribution question is about how much is available.
        self.assertEqual(m.best_tagged_lender_tag_count, 3)
        self.assertEqual(m.lenders_with_tags, 2)

    def test_an_already_rescued_entry_is_flagged(self):
        chosen = candidate("https://mangadex.org/1", 90.0, booru=("x",),
                           borrowed_from="https://gelbooru.com/1")
        self.assertTrue(measure_entry(1, [chosen], 0, 5, 5.0).already_borrowed)

    def test_no_candidates_is_recorded_rather_than_crashing(self):
        m = measure_entry(0, [], 0, 5, 5.0)
        self.assertTrue(m.all_matches_dead)
        self.assertEqual(m.eligible_lender_count, 0)
        # An out-of-range index is a bug elsewhere, not a reason to lose the row.
        self.assertEqual(measure_entry(3, [candidate("u", 90.0)], 7, 5, 5.0).entry_tag_count, 3)

    def test_it_does_not_mutate_what_it_measures(self):
        chosen = candidate("https://mangadex.org/1", 90.0)
        lender = candidate("https://gelbooru.com/1", 90.0, booru=("x", "y"))
        measure_entry(0, [chosen, lender], 0, 5, 5.0)
        self.assertEqual(chosen.booru_tags, [])
        self.assertIsNone(chosen.tags_borrowed_from)
        self.assertEqual(len(lender.booru_tags), 2)


class TestSummarize(unittest.TestCase):
    def _rows(self):
        return [
            # tagless, one 40-tag cross-engine lender
            EntryMeasurement(entry_bucket=BUCKET_TAGLESS, chosen_booru_bucket=BUCKET_TAGLESS,
                             eligible_lender_count=1, best_tagged_lender_tag_count=40,
                             tagged_lender_cross_engine=True, best_tagged_lender_site="Gelbooru",
                             chosen_site="MangaDex"),
            # tagless, an eligible lender but nothing recorded on it
            EntryMeasurement(entry_bucket=BUCKET_TAGLESS, chosen_booru_bucket=BUCKET_TAGLESS,
                             eligible_lender_count=2, chosen_site="Sankaku"),
            # under-tagged, no lender at all
            EntryMeasurement(entry_bucket=BUCKET_UNDER_TAGGED,
                             chosen_booru_bucket=BUCKET_UNDER_TAGGED, chosen_site="paheal"),
            # under-tagged with a same-engine 6-tag lender
            EntryMeasurement(entry_bucket=BUCKET_UNDER_TAGGED,
                             chosen_booru_bucket=BUCKET_UNDER_TAGGED,
                             eligible_lender_count=1, best_tagged_lender_tag_count=6,
                             tagged_lender_cross_engine=False, best_tagged_lender_site="Rule34",
                             chosen_site="paheal"),
            # well tagged, bucketed differently by the two questions
            EntryMeasurement(entry_bucket=BUCKET_WELL_TAGGED,
                             chosen_booru_bucket=BUCKET_UNDER_TAGGED, chosen_site="paheal"),
        ]

    def test_rates_are_per_band(self):
        s = summarize(self._rows(), by="entry")
        self.assertEqual(s[BUCKET_TAGLESS].entries, 2)
        self.assertEqual(s[BUCKET_TAGLESS].with_eligible_lender, 2)
        self.assertEqual(s[BUCKET_TAGLESS].with_tagged_lender, 1)
        self.assertAlmostEqual(s[BUCKET_TAGLESS].eligible_lender_rate, 1.0)
        self.assertAlmostEqual(s[BUCKET_TAGLESS].tagged_lender_rate, 0.5)
        self.assertAlmostEqual(s[BUCKET_TAGLESS].mean_lender_tags, 40.0)

        band = s[BUCKET_UNDER_TAGGED]
        self.assertEqual(band.entries, 2)
        self.assertEqual(band.with_eligible_lender, 1)
        self.assertEqual(band.cross_engine_lenders, 0)
        self.assertEqual(band.same_engine_lenders, 1)
        self.assertEqual(band.lender_tag_max, 6)
        self.assertEqual(band.chosen_sites, {"paheal": 2})

    def test_the_two_bucketings_disagree_and_both_are_reported(self):
        by_entry = summarize(self._rows(), by="entry")
        by_chosen = summarize(self._rows(), by="chosen_booru")
        self.assertEqual(by_entry[BUCKET_UNDER_TAGGED].entries, 2)
        self.assertEqual(by_chosen[BUCKET_UNDER_TAGGED].entries, 3)
        self.assertEqual(by_entry[BUCKET_WELL_TAGGED].entries, 1)
        self.assertEqual(by_chosen[BUCKET_WELL_TAGGED].entries, 0)

    def test_an_empty_band_has_no_rate_rather_than_a_division_error(self):
        s = summarize([], by="entry")
        self.assertEqual(s[BUCKET_UNDER_TAGGED].entries, 0)
        self.assertEqual(s[BUCKET_UNDER_TAGGED].eligible_lender_rate, 0.0)
        self.assertEqual(s[BUCKET_UNDER_TAGGED].mean_lender_tags, 0.0)

    def test_an_unknown_bucketing_is_rejected(self):
        with self.assertRaises(ValueError):
            summarize([], by="whatever")

    def test_the_table_and_json_carry_the_rates(self):
        s = summarize(self._rows(), by="entry")
        table = format_table(s, title="T")
        self.assertIn("| `1..min-1` | 2 | 1 (50.0%)", table)
        as_json = summary_to_dict(s)
        self.assertAlmostEqual(as_json[BUCKET_TAGLESS]["tagged_lender_rate"], 0.5)
        self.assertEqual(as_json[BUCKET_UNDER_TAGGED]["entries"], 2)


class TestRecorder(unittest.TestCase):
    def test_off_unless_the_environment_names_a_file(self):
        self.assertIsNone(Recorder.from_env({}))
        self.assertIsNone(Recorder.from_env({"HATATE_TAG_BAND_METRICS": "   "}))
        self.assertIsNotNone(Recorder.from_env({"HATATE_TAG_BAND_METRICS": "/tmp/x.jsonl"}))

    def test_rows_round_trip_and_a_truncated_one_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.jsonl")
            rec = Recorder(path)
            rec.record(EntryMeasurement(entry_tag_count=3, entry_bucket=BUCKET_UNDER_TAGGED))
            rec.record(EntryMeasurement(entry_tag_count=9, entry_bucket=BUCKET_WELL_TAGGED))
            self.assertEqual(rec.written, 2)
            # A killed run leaves a half-written last line; the rows before
            # it are the whole reason for writing JSONL.
            with open(path, "a", encoding="utf-8") as fh:
                fh.write('{"entry_tag_count": 4, "entry_b')
            rows = load_measurements(path)
        self.assertEqual([r.entry_tag_count for r in rows], [3, 9])

    def test_an_unwritable_path_does_not_raise(self):
        rec = Recorder(os.path.join(tempfile.gettempdir(), "no-such-dir-dan55", "m.jsonl"))
        rec.record(EntryMeasurement())
        self.assertEqual(rec.written, 0)

    def test_unknown_fields_in_a_recording_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"entry_tag_count": 2, "from_a_later_version": True}) + "\n")
                fh.write("\n")
            rows = load_measurements(path)
        self.assertEqual([r.entry_tag_count for r in rows], [2])


class TestSearchEngineHook(unittest.TestCase):
    """The hook sits in the middle of the search path, so 'it does nothing
    when off' is the assertion that matters most."""

    def setUp(self):
        from core import search_engine
        self.se = search_engine
        search_engine._tag_band_recorder = None
        search_engine._tag_band_resolved = False
        self.addCleanup(self._reset)

    def _reset(self):
        self.se._tag_band_recorder = None
        self.se._tag_band_resolved = False

    def _entry(self):
        e = ImageEntry(path="/tmp/x.png")
        e.status = MatchStatus.POOR
        return e

    def test_no_recorder_means_no_measurement(self):
        os.environ.pop("HATATE_TAG_BAND_METRICS", None)
        self.assertIsNone(self.se.tag_band_recorder())
        entry = self._entry()
        self.assertIsNone(
            self.se._measure_tag_band(entry, [candidate("u", 90.0)], 0, Settings())
        )
        # And completing a None measurement is a no-op, not a crash.
        self.se._finish_tag_band_measurement(None, entry, Settings())

    def test_the_recorder_is_resolved_once_per_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.jsonl")
            os.environ["HATATE_TAG_BAND_METRICS"] = path
            self.addCleanup(os.environ.pop, "HATATE_TAG_BAND_METRICS", None)
            first = self.se.tag_band_recorder()
            os.environ["HATATE_TAG_BAND_METRICS"] = path + ".other"
            self.assertIs(self.se.tag_band_recorder(), first)

    def test_the_final_tag_count_comes_from_the_entry_after_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.jsonl")
            os.environ["HATATE_TAG_BAND_METRICS"] = path
            self.addCleanup(os.environ.pop, "HATATE_TAG_BAND_METRICS", None)

            entry = self._entry()
            chosen = candidate("https://mangadex.org/1", 90.0)
            lender = candidate("https://gelbooru.com/1", 90.0, booru=("x", "y", "z"))
            settings = Settings()
            snapshot = self.se._measure_tag_band(entry, [chosen, lender], 0, settings)
            self.assertIsNotNone(snapshot)
            # Pre-borrow: the chosen match really has no tags of its own.
            self.assertEqual(snapshot.chosen_booru_tag_count, 0)
            self.assertEqual(snapshot.eligible_lender_count, 1)

            # The rescue then runs and select_candidate() copies its tags
            # onto the entry, exactly as the real path does.
            chosen.booru_tags = list(lender.booru_tags)
            chosen.tags_borrowed_from = lender.url
            entry.candidates = [chosen, lender]
            entry.select_candidate(0)
            self.se._finish_tag_band_measurement(snapshot, entry, settings)

            rows = load_measurements(path)
        self.assertEqual(len(rows), 1)
        # The band the user's row lands in counts the borrowed tags...
        self.assertEqual(rows[0].entry_tag_count, 3)
        self.assertEqual(rows[0].entry_bucket, BUCKET_UNDER_TAGGED)
        # ...while the tagless band is still measurable, because the
        # snapshot was taken before the rescue wrote to chosen.booru_tags.
        self.assertEqual(rows[0].chosen_booru_bucket, BUCKET_TAGLESS)

    def test_a_measurement_failure_does_not_break_a_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HATATE_TAG_BAND_METRICS"] = os.path.join(tmp, "m.jsonl")
            self.addCleanup(os.environ.pop, "HATATE_TAG_BAND_METRICS", None)
            entry = self._entry()

            class Exploding:
                @property
                def booru_tags(self):
                    raise RuntimeError("boom")

            self.assertIsNone(
                self.se._measure_tag_band(entry, [Exploding()], 0, Settings())
            )


if __name__ == "__main__":
    unittest.main()
