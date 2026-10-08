"""Tags applied by rule - found / not-found / low-tag (DAN-77).

Three configurable lists of tags the app adds itself based on how a
search turned out, so that "every image this program could not place"
is a tag you can search a Hydrus library for rather than a red row in a
window you have to keep open.

Every test here fails on the parent commit, most of them at import or
attribute lookup because the settings and the rule did not exist. The
four that do not are the ones guarding against the rule feeding itself,
and they are the reason this file is longer than the feature:

  * `_decide_status` counted `len(entry.tags)`. A re-search keeps the
    by-rule tags a previous search left behind - only the search-engine
    and booru sources get replaced - so `hatate:few tags` counted
    towards the very threshold it reports on, and enough configured
    tags would promote a POOR entry to GOOD. The rule would have been
    changing the outcome it exists to describe.
  * the DAN-55 tag-band instrumentation counted the same way, and would
    have inflated its bands on exactly the thin entries it exists to
    study.

Both counts are identical to the old ones when the lists are empty,
which is the default, so neither is a behaviour change for anyone who
has not asked for this.

The cache-hit test is the other one worth reading. `search_cache` stores
status and candidates but NOT `entry.tags`, so a result served from the
cache arrives with no by-rule tags on it. Without a hook on that path the
feature would work once per image and then silently stop - and after one
pass over a library, every image is cached.
"""
import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

from core.config import Settings
from core.models import ImageEntry, MatchStatus, Tag, TagSource
from core.search_engine import _decide_status, search_image
from core.tag_rules import (
    OUTCOME_TAG_SOURCE, apply_outcome_tags, countable_tags, outcome_tags_for,
)

FOUND = "hatate:found"
NOT_FOUND = "hatate:not found"
FEW = "hatate:few tags"


def _settings(**kwargs) -> Settings:
    """Settings with all three lists configured, so a rule that fires the
    wrong one is visible rather than merely absent."""
    s = Settings()
    s.tags_for_found = [FOUND]
    s.tags_for_not_found = [NOT_FOUND]
    s.tags_for_low_tag_count = [FEW]
    for key, value in kwargs.items():
        setattr(s, key, value)
    return s


def _entry(status: MatchStatus, real_tags: int = 0) -> ImageEntry:
    e = ImageEntry(path=os.path.join(tempfile.gettempdir(), "a.jpg"))
    e.status = status
    e.tags = [Tag(name=f"t{i}", source=TagSource.BOORU) for i in range(real_tags)]
    return e


def _displays(entry: ImageEntry):
    return [t.display for t in entry.tags]


def _rule_tags(entry: ImageEntry):
    return [t.display for t in entry.tags if t.source is OUTCOME_TAG_SOURCE]


class TestDefaultsDoNothing(unittest.TestCase):
    """The feature is off until configured. These are tags no site put on
    the image and nobody typed, so an uninvited one is the app putting its
    own bookkeeping into somebody's library."""

    def test_all_three_lists_are_empty_by_default(self):
        s = Settings()
        self.assertEqual(s.tags_for_found, [])
        self.assertEqual(s.tags_for_not_found, [])
        self.assertEqual(s.tags_for_low_tag_count, [])

    def test_nothing_is_added_for_any_outcome_by_default(self):
        s = Settings()
        for status in MatchStatus:
            with self.subTest(status=status):
                e = _entry(status, real_tags=1)
                self.assertEqual(apply_outcome_tags(e, s), [])
                self.assertEqual(_displays(e), ["t0"])

    def test_the_lists_are_independent(self):
        """Filling in one does not switch on the others."""
        s = Settings()
        s.tags_for_not_found = [NOT_FOUND]
        found = _entry(MatchStatus.GOOD, real_tags=9)
        apply_outcome_tags(found, s)
        self.assertEqual(_rule_tags(found), [])

        missing = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(missing, s)
        self.assertEqual(_rule_tags(missing), [NOT_FOUND])


class TestWhichListApplies(unittest.TestCase):
    def test_not_found_gets_the_not_found_tag(self):
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, _settings())
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

    def test_good_gets_the_found_tag(self):
        e = _entry(MatchStatus.GOOD, real_tags=9)
        apply_outcome_tags(e, _settings())
        self.assertEqual(_rule_tags(e), [FOUND])

    def test_poor_is_a_match_too(self):
        """POOR is "found, worth a look", not "not found" - it has a
        matched URL and candidates. Tagging it `hatate:not found` would
        make the not-found set a lie, which is the one thing that set has
        to be right about to be worth searching for."""
        e = _entry(MatchStatus.POOR, real_tags=9)
        apply_outcome_tags(e, _settings())
        self.assertIn(FOUND, _rule_tags(e))
        self.assertNotIn(NOT_FOUND, _rule_tags(e))

    def test_error_gets_nothing(self):
        """A search that failed has no outcome yet - it is retried. Tagging
        the image `hatate:not found` because SauceNAO timed out would be a
        claim about the image the app has not actually made."""
        e = _entry(MatchStatus.ERROR)
        self.assertEqual(apply_outcome_tags(e, _settings()), [])
        self.assertEqual(_rule_tags(e), [])

    def test_unsearched_and_searching_get_nothing(self):
        for status in (MatchStatus.NOT_SEARCHED, MatchStatus.SEARCHING):
            with self.subTest(status=status):
                e = _entry(status)
                self.assertEqual(apply_outcome_tags(e, _settings()), [])


class TestLowTagRule(unittest.TestCase):
    """min_tags_for_good is reused rather than configured a second time -
    it is already the number that makes the row yellow."""

    def test_added_on_top_of_found_when_under_the_threshold(self):
        s = _settings()
        e = _entry(MatchStatus.POOR, real_tags=s.match_conditions.min_tags_for_good - 1)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [FOUND, FEW])

    def test_not_added_at_the_threshold(self):
        s = _settings()
        e = _entry(MatchStatus.GOOD, real_tags=s.match_conditions.min_tags_for_good)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [FOUND])

    def test_follows_the_configured_threshold(self):
        s = _settings()
        s.match_conditions.min_tags_for_good = 3
        under = _entry(MatchStatus.GOOD, real_tags=2)
        apply_outcome_tags(under, s)
        self.assertIn(FEW, _rule_tags(under))

        over = _entry(MatchStatus.GOOD, real_tags=3)
        apply_outcome_tags(over, s)
        self.assertNotIn(FEW, _rule_tags(over))

    def test_not_added_to_a_not_found_entry(self):
        """A not-found entry has no tags by definition, so it satisfies any
        threshold trivially. Labelling it both "not found" and "few tags"
        says nothing the first tag did not, and would put the whole
        not-found set into the few-tags set as a side effect."""
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, _settings())
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

    def test_can_be_used_on_its_own(self):
        s = Settings()
        s.tags_for_low_tag_count = [FEW]
        e = _entry(MatchStatus.POOR, real_tags=1)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [FEW])


class TestTheRuleDoesNotFeedItself(unittest.TestCase):
    """The by-rule tags must not count towards the threshold they report
    on. A re-search keeps them - only the search-engine and booru sources
    are replaced on `select_candidate` - so a naive `len(entry.tags)`
    lets them accumulate into a promotion."""

    def test_countable_tags_excludes_the_rule_source(self):
        e = _entry(MatchStatus.GOOD, real_tags=2)
        e.tags.append(Tag(name="few tags", source=OUTCOME_TAG_SOURCE, namespace="hatate"))
        e.tags.append(Tag(name="mine", source=TagSource.USER))
        self.assertEqual(len(e.tags), 4)
        self.assertEqual(len(countable_tags(e.tags)), 3)

    def test_countable_tags_is_a_no_op_when_the_feature_is_off(self):
        e = _entry(MatchStatus.GOOD, real_tags=4)
        self.assertEqual(countable_tags(e.tags), e.tags)

    def test_stale_rule_tags_do_not_promote_poor_to_good(self):
        """REGRESSION GUARD. On the parent commit `_decide_status` counted
        `len(entry.tags)`, so four real tags plus a configured
        `hatate:found` and `hatate:few tags` reached five and the entry
        came back GOOD - the rule deciding the outcome it describes."""
        s = _settings()
        s.match_conditions.require_larger_than_local = False
        e = _entry(MatchStatus.POOR, real_tags=s.match_conditions.min_tags_for_good - 1)
        # What a previous search of this same entry left behind.
        e.tags.append(Tag(name="found", source=OUTCOME_TAG_SOURCE, namespace="hatate"))
        e.tags.append(Tag(name="few tags", source=OUTCOME_TAG_SOURCE, namespace="hatate"))
        self.assertEqual(_decide_status(e, s), MatchStatus.POOR)

    def test_low_tag_rule_reads_the_same_count(self):
        """Applying twice must not tip the entry over its own threshold."""
        s = _settings()
        e = _entry(MatchStatus.POOR, real_tags=s.match_conditions.min_tags_for_good - 1)
        apply_outcome_tags(e, s)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [FOUND, FEW])


class TestIdempotencyAndTransitions(unittest.TestCase):
    def test_applying_twice_leaves_one_copy(self):
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, _settings())
        apply_outcome_tags(e, _settings())
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

    def test_a_later_match_clears_the_not_found_tag(self):
        """The point of filing these under one source nothing else writes:
        an image that was not found and now is must not end up carrying
        both tags and claiming each."""
        s = _settings()
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

        e.status = MatchStatus.GOOD
        e.add_tags([Tag(name=f"t{i}", source=TagSource.BOORU) for i in range(9)])
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [FOUND])

    def test_an_error_on_re_search_clears_a_stale_outcome(self):
        """ERROR adds nothing, but it must still drop what the last search
        claimed - the app no longer knows the image was not found, so it
        must not keep saying so."""
        s = _settings()
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        e.status = MatchStatus.ERROR
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [])

    def test_unconfiguring_the_feature_clears_the_tags_on_next_search(self):
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, _settings())
        apply_outcome_tags(e, Settings())
        self.assertEqual(_rule_tags(e), [])

    def test_user_and_hydrus_tags_are_never_touched(self):
        s = _settings()
        e = _entry(MatchStatus.NOT_FOUND)
        e.tags = [
            Tag(name="mine", source=TagSource.USER),
            Tag(name="theirs", source=TagSource.HYDRUS),
        ]
        apply_outcome_tags(e, s)
        e.status = MatchStatus.GOOD
        apply_outcome_tags(e, s)
        self.assertIn("mine", _displays(e))
        self.assertIn("theirs", _displays(e))

    def test_no_op_application_does_not_dirty_the_entry(self):
        """The common case is the feature being off. Marking every searched
        entry dirty for the session store on a call that changed nothing
        would make an autosave write the whole set each time."""
        e = _entry(MatchStatus.GOOD, real_tags=2)
        e.clear_dirty() if hasattr(e, "clear_dirty") else None
        before = getattr(e, "_dirty", None)
        apply_outcome_tags(e, Settings())
        self.assertEqual(getattr(e, "_dirty", None), before)


class TestTagText(unittest.TestCase):
    def test_namespace_is_split_off(self):
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, _settings())
        tag = e.tags[0]
        self.assertEqual(tag.namespace, "hatate")
        self.assertEqual(tag.name, "not found")
        self.assertEqual(tag.display, NOT_FOUND)

    def test_a_tag_with_no_namespace_is_kept_bare(self):
        s = Settings()
        s.tags_for_not_found = ["unsourced"]
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertIsNone(e.tags[0].namespace)
        self.assertEqual(e.tags[0].name, "unsourced")

    def test_only_the_first_colon_splits(self):
        s = Settings()
        s.tags_for_not_found = ["series:Re:Zero"]
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertEqual((e.tags[0].namespace, e.tags[0].name), ("series", "Re:Zero"))

    def test_blank_and_colon_only_entries_are_dropped(self):
        """The lists are free text a user typed into a box; an empty tag is
        not something to write into a library."""
        s = Settings()
        s.tags_for_not_found = ["", "   ", ":", NOT_FOUND]
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

    def test_duplicates_are_written_once(self):
        s = Settings()
        s.tags_for_not_found = [NOT_FOUND, NOT_FOUND, "hatate:not found"]
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), [NOT_FOUND])

    def test_the_found_and_low_tag_lists_can_overlap(self):
        s = Settings()
        s.tags_for_found = ["hatate:seen"]
        s.tags_for_low_tag_count = ["hatate:seen"]
        e = _entry(MatchStatus.POOR, real_tags=1)
        apply_outcome_tags(e, s)
        self.assertEqual(_rule_tags(e), ["hatate:seen"])

    def test_outcome_tags_for_is_usable_without_an_entry(self):
        s = _settings()
        self.assertEqual(
            [t.display for t in outcome_tags_for(MatchStatus.NOT_FOUND, 0, s)], [NOT_FOUND],
        )


class TestWrittenUnderTheFilterableSource(unittest.TestCase):
    """DAN-72's tag-source filter landing first was the precondition for
    this feature. What makes it work is that these tags carry a source
    nothing else in the app writes, so unticking it keeps them local."""

    def test_every_rule_tag_carries_the_hatate_source(self):
        s = _settings()
        for status, count in ((MatchStatus.NOT_FOUND, 0), (MatchStatus.POOR, 1)):
            with self.subTest(status=status):
                e = _entry(status, real_tags=count)
                applied = apply_outcome_tags(e, s)
                self.assertTrue(applied)
                self.assertTrue(all(t.source is TagSource.HATATE for t in applied))

    def test_the_filter_can_keep_them_out_of_a_send(self):
        from core.models import tags_to_send

        s = _settings()
        s.filter_sent_tags_by_source = True
        s.enabled_tag_sources = ["User", "Hydrus", "Booru"]  # Hatate-linux unticked
        e = _entry(MatchStatus.NOT_FOUND)
        e.tags = [Tag(name="real", source=TagSource.BOORU)]
        apply_outcome_tags(e, s)
        self.assertIn(NOT_FOUND, _displays(e))
        self.assertEqual([t.display for t in tags_to_send(e, s)], ["real"])

    def test_they_reach_a_send_when_the_source_is_ticked(self):
        from core.models import tags_to_send

        s = _settings()
        s.filter_sent_tags_by_source = True
        e = _entry(MatchStatus.NOT_FOUND)
        apply_outcome_tags(e, s)
        self.assertIn(NOT_FOUND, [t.display for t in tags_to_send(e, s)])


# --- Through a real search ------------------------------------------
#
# The unit tests above prove the rule. These prove it is actually wired
# to the four places a result is finalised, which is the part a refactor
# can quietly undo.

_jpeg = io.BytesIO()
Image.new("RGB", (100, 100), color="red").save(_jpeg, format="JPEG")
VALID_JPEG = _jpeg.getvalue()


def _image_file():
    """A throwaway file whose bytes are unique to this call - the search
    cache is keyed on file CONTENT, so shared bytes would replay one
    test's result into the next."""
    path = os.path.join(tempfile.mkdtemp(), "x.png")
    with open(path, "wb") as fh:
        fh.write(b"x" + uuid.uuid4().bytes)
    return path


def _search_settings(**kwargs) -> Settings:
    s = _settings(**kwargs)
    s.retrieve_tags_from_booru = False
    s.enable_ascii2d = False
    s.enable_tracemoe = False
    s.enable_iqdb3d = False
    s.enable_google_images = False
    s.enable_google_lens = False
    s.enable_yandex = False
    s.enable_pawchive = False
    s.enable_pawchive_index = False
    s.secondary_engine_mode = "disabled"
    s.primary_engine = "iqdb"
    s.drop_dead_matches = False
    s.match_conditions.require_larger_than_local = False
    return s


def _iqdb_match(tags=()):
    """One IQDB hit carrying `tags` as search-engine tags. They have to be
    real Tag objects: select_candidate puts them straight onto the entry's
    tag list, so a bare string reaches Hydrus-bound code as one."""
    from core.iqdb import IqdbMatch

    return IqdbMatch(
        url="https://danbooru.donmai.us/posts/1", thumb_url=None,
        similarity=97.0, width=800, height=600, source_name="Danbooru",
        is_best_match=True,
        unnamespaced_tags=[Tag(name=t, source=TagSource.SEARCH_ENGINE) for t in tags],
    )


def _run_search(entry, settings, iqdb_result, use_cache=False):
    with patch("core.iqdb.search", return_value=list(iqdb_result)), \
         patch("core.search_engine._load_local_dimensions", lambda e: None), \
         patch("core.remote.download_bytes", return_value=None), \
         patch("core.remote.fetch_remote_info", return_value=(None, None)):
        return search_image(entry, settings, use_cache=use_cache)


class TestHookedIntoASearch(unittest.TestCase):
    def test_a_fresh_no_match_is_tagged_not_found(self):
        entry = ImageEntry(path=_image_file())
        entry.hydrus_hash = "hash-no-match"
        _run_search(entry, _search_settings(), [])
        self.assertEqual(entry.status, MatchStatus.NOT_FOUND)
        self.assertEqual(_rule_tags(entry), [NOT_FOUND])

    def test_a_fresh_match_with_few_tags_gets_both(self):
        entry = ImageEntry(path=_image_file())
        entry.hydrus_hash = "hash-thin-match"
        _run_search(entry, _search_settings(), [_iqdb_match(["solo", "smile"])])
        self.assertIn(entry.status, (MatchStatus.GOOD, MatchStatus.POOR))
        self.assertEqual(_rule_tags(entry), [FOUND, FEW])

    def test_a_fresh_match_with_plenty_of_tags_gets_only_found(self):
        entry = ImageEntry(path=_image_file())
        entry.hydrus_hash = "hash-fat-match"
        tags = [f"tag{i}" for i in range(9)]
        _run_search(entry, _search_settings(), [_iqdb_match(tags)])
        self.assertEqual(entry.status, MatchStatus.GOOD)
        self.assertEqual(_rule_tags(entry), [FOUND])

    def test_an_errored_search_is_not_tagged(self):
        """Every engine failed, so the status is ERROR rather than
        NOT_FOUND. The image must not be labelled not-found for it."""
        entry = ImageEntry(path=_image_file())
        entry.hydrus_hash = "hash-error"
        from core.iqdb import IqdbError

        with patch("core.iqdb.search", side_effect=IqdbError("iqdb is down")), \
             patch("core.search_engine._load_local_dimensions", lambda e: None):
            search_image(entry, _search_settings(), use_cache=False)
        self.assertEqual(entry.status, MatchStatus.ERROR)
        self.assertEqual(_rule_tags(entry), [])

    def test_a_cache_hit_still_gets_its_tags(self):
        """REGRESSION GUARD, and the reason the cache path needs its own
        hook. `save_cached_result` stores status and candidates but not
        `entry.tags`, so a cached result arrives with no by-rule tags on
        it. Without this the rule works once per image and then stops -
        and after one pass over a library, every image is cached."""
        settings = _search_settings()
        path = _image_file()

        first = ImageEntry(path=path)
        _run_search(first, settings, [], use_cache=True)
        self.assertEqual(first.status, MatchStatus.NOT_FOUND)
        self.assertEqual(_rule_tags(first), [NOT_FOUND])

        # Same file, fresh entry, cache allowed. The engine must not be
        # reached; the tags must be there anyway.
        second = ImageEntry(path=path)
        with patch("core.iqdb.search", side_effect=AssertionError("cache was not used")), \
             patch("core.search_engine._load_local_dimensions", lambda e: None):
            search_image(second, settings, use_cache=True)

        self.assertEqual(second.result_source, "cached")
        self.assertEqual(second.status, MatchStatus.NOT_FOUND)
        self.assertEqual(_rule_tags(second), [NOT_FOUND])

    def test_a_cache_hit_on_a_match_is_tagged_found(self):
        settings = _search_settings()
        path = _image_file()

        first = ImageEntry(path=path)
        _run_search(first, settings, [_iqdb_match([f"tag{i}" for i in range(9)])],
                    use_cache=True)
        self.assertEqual(_rule_tags(first), [FOUND])

        second = ImageEntry(path=path)
        with patch("core.iqdb.search", side_effect=AssertionError("cache was not used")), \
             patch("core.search_engine._load_local_dimensions", lambda e: None), \
             patch("core.remote.download_bytes", return_value=None), \
             patch("core.remote.fetch_remote_info", return_value=(None, None)):
            search_image(second, settings, use_cache=True)

        self.assertEqual(second.result_source, "cached")
        self.assertEqual(_rule_tags(second), [FOUND])

    def test_nothing_is_added_to_any_search_path_by_default(self):
        """The whole feature off, end to end. No path may invent a tag."""
        settings = _search_settings()
        settings.tags_for_found = []
        settings.tags_for_not_found = []
        settings.tags_for_low_tag_count = []

        for label, result in (("no match", []), ("match", [_iqdb_match(["solo"])])):
            with self.subTest(label):
                entry = ImageEntry(path=_image_file())
                entry.hydrus_hash = f"hash-default-{label}"
                _run_search(entry, settings, result)
                self.assertEqual(
                    [t for t in entry.tags if t.source is OUTCOME_TAG_SOURCE], [],
                )

    def test_a_re_search_that_now_matches_drops_the_not_found_tag(self):
        """The transition that matters in practice: a library re-run after
        a booru came back online must not leave images claiming both."""
        settings = _search_settings()
        entry = ImageEntry(path=_image_file())
        entry.hydrus_hash = "hash-transition"

        _run_search(entry, settings, [])
        self.assertEqual(_rule_tags(entry), [NOT_FOUND])

        _run_search(entry, settings, [_iqdb_match([f"tag{i}" for i in range(9)])])
        self.assertEqual(_rule_tags(entry), [FOUND])
        self.assertNotIn(NOT_FOUND, _displays(entry))


class TestTagBandInstrumentationIsNotInflated(unittest.TestCase):
    """DAN-55's instrumentation measures how many tags the engines and
    boorus actually yielded. Counting the app's own by-rule tags would
    inflate the band on exactly the thin entries it exists to study, and
    by however many tags the user happened to configure."""

    def test_the_recorded_count_excludes_rule_tags(self):
        from core.search_engine import _finish_tag_band_measurement
        from core.tag_band_metrics import EntryMeasurement

        s = _settings()
        entry = _entry(MatchStatus.POOR, real_tags=2)
        apply_outcome_tags(entry, s)
        self.assertEqual(len(entry.tags), 4)  # 2 real + found + few tags

        measurement = EntryMeasurement()
        recorded = []

        class _Recorder:
            def record(self, m):
                recorded.append(m)

        with patch("core.search_engine.tag_band_recorder", return_value=_Recorder()):
            _finish_tag_band_measurement(measurement, entry, s)

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].entry_tag_count, 2)


try:
    from PyQt6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

_app = None


def setUpModule():
    """One QApplication for the whole module - Qt allows only one."""
    global _app
    if HAVE_QT:
        _app = QApplication.instance() or QApplication([])


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestSettingsDialogSection(unittest.TestCase):
    """The three boxes in Settings > Tag Namespaces, and that what they
    hold reaches the Settings object. A setting with no way to set it is
    not a feature, and the dialog is where this one is switched on."""

    def _dialog(self, settings):
        from gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(settings)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_the_boxes_show_the_current_setting(self):
        s = _settings()
        s.tags_for_not_found = [NOT_FOUND, "hatate:unplaced"]
        dialog = self._dialog(s)
        self.assertEqual(dialog.tags_for_found_edit.toPlainText(), FOUND)
        self.assertEqual(
            dialog.tags_for_not_found_edit.toPlainText(),
            f"{NOT_FOUND}\nhatate:unplaced",
        )
        self.assertEqual(dialog.tags_for_low_tag_count_edit.toPlainText(), FEW)

    def test_the_boxes_are_empty_by_default(self):
        dialog = self._dialog(Settings())
        for name in ("tags_for_found_edit", "tags_for_not_found_edit",
                     "tags_for_low_tag_count_edit"):
            with self.subTest(name):
                self.assertEqual(getattr(dialog, name).toPlainText(), "")

    def test_what_is_typed_is_saved(self):
        s = Settings()
        dialog = self._dialog(s)
        dialog.tags_for_found_edit.setPlainText(FOUND)
        dialog.tags_for_not_found_edit.setPlainText(f"{NOT_FOUND}\nhatate:unplaced")
        dialog.tags_for_low_tag_count_edit.setPlainText(FEW)
        dialog.apply_to_settings()

        self.assertEqual(s.tags_for_found, [FOUND])
        self.assertEqual(s.tags_for_not_found, [NOT_FOUND, "hatate:unplaced"])
        self.assertEqual(s.tags_for_low_tag_count, [FEW])

    def test_blank_lines_are_not_saved(self):
        """People leave a trailing newline in a text box. It must not turn
        into an empty tag."""
        s = Settings()
        dialog = self._dialog(s)
        dialog.tags_for_not_found_edit.setPlainText(f"\n  \n{NOT_FOUND}\n\n")
        dialog.apply_to_settings()
        self.assertEqual(s.tags_for_not_found, [NOT_FOUND])

    def test_clearing_a_box_switches_that_rule_off(self):
        s = _settings()
        dialog = self._dialog(s)
        dialog.tags_for_not_found_edit.setPlainText("")
        dialog.apply_to_settings()
        self.assertEqual(s.tags_for_not_found, [])
        self.assertEqual(s.tags_for_found, [FOUND])  # the others are untouched

    def test_a_full_round_trip_leaves_the_settings_unchanged(self):
        """Open and OK without touching anything must not alter what is
        configured - the usual way a settings tab loses a value."""
        s = _settings()
        s.tags_for_not_found = [NOT_FOUND, "hatate:unplaced"]
        before = (list(s.tags_for_found), list(s.tags_for_not_found),
                  list(s.tags_for_low_tag_count))
        self._dialog(s).apply_to_settings()
        self.assertEqual(
            (s.tags_for_found, s.tags_for_not_found, s.tags_for_low_tag_count), before,
        )

    def test_the_section_is_findable_by_the_settings_search(self):
        """Settings search indexes the built widget tree, so a label longer
        than its cap is invisible to it. These three are how a user finds
        the feature at all."""
        from gui import settings_search

        dialog = self._dialog(Settings())
        names = [name.casefold() for _, _, name, _ in settings_search.index_tabs(dialog._pages)]
        # The index strips a label's trailing colon, so these are the
        # names as a searching user would match them.
        for expected in ("tags applied by rule", "when a match is found",
                         "when no match is found", "when a match has few tags"):
            with self.subTest(expected):
                self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
