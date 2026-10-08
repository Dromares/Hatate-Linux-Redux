"""Dedicated coverage for core/tag_rules.py's WIRING, not its functions.

The functions in core/tag_rules.py (apply_namespace_remap, apply_tag_blacklist,
with_rating_tag, split_tag_text, apply_inline_edit, the outcome-tag family)
already sit at 100% branch coverage - tests/test_parsers_and_tags.py and
tests/test_outcome_tags.py exercise every one of them directly, in isolation,
thoroughly. That is not duplicated here.

What is NOT covered anywhere else is how those functions are actually
COMPOSED and CALLED at each of their real call sites - and the module's own
docstring names the exact hazard: namespace remapping runs before the
blacklist, so a blacklist pattern has to be written against the tag's
REMAPPED form. Swap that composition order in core/engine_runner.py or
core/search_engine.py - or forget to wire a setting through at a new call
site - and every existing test still passes, because each one only proves
the two functions are individually correct, never that production code
calls them in the right order with the right settings. That silent gap is
exactly the class of bug this ticket (DAN-166) exists to close.

Three call sites, three gaps, closed below:

  * core/search_engine.py's fetch_candidate_details and
    core/engine_runner.py's SauceNAO collector both compose
    apply_tag_blacklist(apply_namespace_remap(tags, settings), settings).
    Nothing proved that composition runs in that order rather than the
    reverse - both read identically at a glance, and behave identically
    for a pattern that doesn't care about the remap.
  * core/hydrus_tag_lookup.py's apply_existing_hydrus_tags remaps
    namespaces on tags Hydrus already holds. Every existing test for that
    function leaves tag_namespace_remap off and feeds it an unnamespaced
    tag, so the remap call on that path has never actually fired.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

from core.boorus import BooruPageInfo
from core.config import Settings
from core.models import ImageEntry, MatchCandidate, Tag, TagSource


def _image_path():
    path = os.path.join(tempfile.mkdtemp(), "x.jpg")
    Image.new("RGB", (64, 64), "red").save(path)
    return path


class TestRemapRunsBeforeBlacklistOnTheBooruPagePath(unittest.TestCase):
    """core/search_engine.py:fetch_candidate_details composes
    apply_tag_blacklist(apply_namespace_remap(tags, settings), settings).

    A pattern written against the PRE-remap namespace must survive - the
    blacklist never sees that spelling, because by the time it runs the
    tag has already been renamed. A pattern written against the
    POST-remap namespace must catch it. Getting this backwards (or
    reordering the two calls) silently changes which patterns fire for
    everyone who has both features configured, with no test anywhere else
    able to see it.
    """

    def _settings(self):
        s = Settings()
        s.enable_tag_namespace_remap = True
        s.tag_namespace_remap = {"artist": "creator"}
        s.enable_tag_blacklist = True
        return s

    def _fetch(self, settings):
        from core.search_engine import fetch_candidate_details

        candidate = MatchCandidate(url="https://danbooru.donmai.us/posts/1")
        page_info = BooruPageInfo(
            tags=[Tag("someone", TagSource.BOORU, "artist")], fetched=True,
        )
        with patch("core.search_engine.fetch_page_info", return_value=page_info):
            fetch_candidate_details(candidate, settings)
        return [t.display for t in candidate.booru_tags]

    def test_a_pattern_against_the_pre_remap_namespace_never_fires(self):
        s = self._settings()
        s.tag_blacklist = ["artist:*"]
        self.assertEqual(self._fetch(s), ["creator:someone"])

    def test_a_pattern_against_the_post_remap_namespace_fires(self):
        s = self._settings()
        s.tag_blacklist = ["creator:*"]
        self.assertEqual(self._fetch(s), [])


class TestRemapRunsBeforeBlacklistOnTheSauceNaoPath(unittest.TestCase):
    """The same composition, at a different call site:
    core/engine_runner.py's SauceNAO collector applies
    apply_tag_blacklist(apply_namespace_remap(list(m.tags), settings),
    settings) to the engine's OWN tags - before any booru page is ever
    fetched. Unlike the booru-page path above, nothing anywhere else turns
    the blacklist and remap settings on for this call site at all, so this
    closes that gap end to end through search_image rather than by calling
    the collector directly.
    """

    def _settings(self):
        s = Settings()
        s.primary_engine = "saucenao"
        s.secondary_engine_mode = "disabled"
        s.enable_ascii2d = False
        s.enable_tracemoe = False
        s.enable_iqdb3d = False
        s.enable_google_images = False
        s.enable_google_lens = False
        s.enable_yandex = False
        s.enable_pawchive = False
        s.enable_pawchive_index = False
        s.retrieve_tags_from_booru = False  # isolate the engine-native tags
        s.drop_dead_matches = False
        s.match_conditions.require_larger_than_local = False
        s.saucenao.api_key = "k"
        s.saucenao.use_json_api = True
        s.saucenao.min_similarity = 50
        s.saucenao.tag_types = ["creator"]
        s.enable_tag_namespace_remap = True
        s.tag_namespace_remap = {"creator": "artist"}
        s.enable_tag_blacklist = True
        return s

    def _run(self, settings):
        import core.search_engine as se

        response = {
            "header": {"status": 0, "long_remaining": 100, "long_limit": 100},
            "results": [{
                "header": {"similarity": "95", "index_name": "danbooru",
                           "thumbnail": "https://img1.saucenao.com/t.jpg"},
                "data": {"ext_urls": ["https://danbooru.donmai.us/posts/1"],
                         "creator": "someone"},
            }],
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response

        entry = ImageEntry(path=_image_path())
        with patch("requests.Session.post", return_value=resp), \
             patch.object(se, "_load_local_dimensions", lambda e: None):
            se.search_image(entry, settings, use_cache=False)
        return [t.display for t in entry.tags if t.source is TagSource.SEARCH_ENGINE]

    def test_the_pre_remap_namespace_pattern_never_fires(self):
        s = self._settings()
        s.tag_blacklist = ["creator:*"]
        self.assertEqual(self._run(s), ["artist:someone"])

    def test_the_post_remap_namespace_pattern_fires(self):
        s = self._settings()
        s.tag_blacklist = ["artist:*"]
        self.assertEqual(self._run(s), [])


class TestExistingHydrusTagsAreActuallyRemapped(unittest.TestCase):
    """core/hydrus_tag_lookup.py:apply_existing_hydrus_tags remaps the
    namespace on tags Hydrus already holds for a file, same as a freshly
    searched tag would be - per the module docstring, this is one of the
    three places remapping is meant to apply. Every existing test for this
    function (tests/test_core_utils.py) runs with the remap feature off
    and an unnamespaced tag, so the remap call on this path (
    core/hydrus_tag_lookup.py line ~185) has never actually executed under
    test.
    """

    def _entry(self):
        path = os.path.join(tempfile.mkdtemp(), "a.jpg")
        with open(path, "wb") as fh:
            fh.write(b"x" * 10)
        entry = ImageEntry(path=path)
        entry.hydrus_hash = "a" * 64
        return entry

    def test_a_remapped_namespace_reaches_the_imported_tag(self):
        from core import hydrus_tag_lookup as htl

        settings = Settings()
        settings.hydrus.access_key = "k"
        settings.enable_tag_namespace_remap = True
        settings.tag_namespace_remap = {"artist": "creator"}

        entry = self._entry()
        client = MagicMock()
        client.get_tags_and_sizes_for_hashes.return_value = {
            entry.hydrus_hash: (["artist:someone"], None),
        }
        with patch.object(htl, "HydrusClient", return_value=client):
            result = htl.apply_existing_hydrus_tags([entry], settings)

        self.assertEqual(result.tagged_count, 1)
        self.assertEqual([t.display for t in entry.tags], ["creator:someone"])

    def test_disabled_leaves_the_original_namespace(self):
        """Same input, feature off: the existing tests already prove an
        unnamespaced tag passes through unchanged, but not that a
        NAMESPACED one is left alone rather than being remapped anyway."""
        from core import hydrus_tag_lookup as htl

        settings = Settings()
        settings.hydrus.access_key = "k"

        entry = self._entry()
        client = MagicMock()
        client.get_tags_and_sizes_for_hashes.return_value = {
            entry.hydrus_hash: (["artist:someone"], None),
        }
        with patch.object(htl, "HydrusClient", return_value=client):
            htl.apply_existing_hydrus_tags([entry], settings)

        self.assertEqual([t.display for t in entry.tags], ["artist:someone"])


if __name__ == "__main__":
    unittest.main()
