"""Which similarity numbers are real, and whether the app says so.

IQDB and SauceNAO measure. ascii2d and the Google engines do not: their
"similarity" is the result's position in their own list, rendered as
80/78/76. Both arrived in the Similarity column as "80%", so a reader had
no way to tell a measured 80 from a made-up one.

MatchCandidate.similarity_measured already recorded the difference, but
nothing downstream carried it: select_candidate dropped it on the way to
ImageEntry, so no display surface could read it, and the cache codec
omitted it, so a restart both forgot which numbers were real and paid to
re-measure them.

These pin the whole path - model, codec, table, preview, dropdown,
export - because the failure mode is silent: every one of them renders a
perfectly plausible number either way.
"""
import os
import tempfile
import unittest

from . import _path  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="hatate-sim-tests-"))

try:
    import PyQt6  # noqa: F401
    HAVE_QT = True
except ImportError:  # pragma: no cover - environment without Qt
    HAVE_QT = False

from core.models import ImageEntry, MatchCandidate, MatchStatus
from core.similarity_display import ESTIMATE_CHIP, similarity_label, similarity_tooltip


def _measured(**kw) -> MatchCandidate:
    kw.setdefault("url", "https://danbooru.donmai.us/posts/1")
    kw.setdefault("similarity", 92.0)
    kw.setdefault("engine", "IQDB")
    kw.setdefault("similarity_measured", True)
    return MatchCandidate(**kw)


def _ordinal(**kw) -> MatchCandidate:
    kw.setdefault("url", "https://www.ascii2d.net/x")
    kw.setdefault("similarity", 80.0)
    kw.setdefault("engine", "ascii2d")
    kw.setdefault("similarity_measured", False)
    return MatchCandidate(**kw)


class TestTheEntryCarriesTheFlag(unittest.TestCase):
    """select_candidate is the only route from a candidate to the fields
    every display surface reads. It copied the number and left behind the
    one thing that says what the number means."""

    def test_selecting_a_measured_candidate_marks_the_entry(self):
        entry = ImageEntry(path="/tmp/a.png", candidates=[_measured()])
        entry.select_candidate(0)
        self.assertEqual(entry.similarity, 92.0)
        self.assertTrue(entry.similarity_measured)

    def test_selecting_an_ordinal_candidate_leaves_the_entry_unmarked(self):
        entry = ImageEntry(path="/tmp/a.png", candidates=[_ordinal()])
        entry.select_candidate(0)
        self.assertEqual(entry.similarity, 80.0)
        self.assertFalse(entry.similarity_measured)

    def test_switching_candidates_updates_the_flag_with_the_number(self):
        """The flag has to move with the selection, or the entry ends up
        claiming an ascii2d ranking was measured because the candidate
        looked at before it was."""
        entry = ImageEntry(path="/tmp/a.png", candidates=[_measured(), _ordinal()])
        entry.select_candidate(0)
        entry.select_candidate(1)
        self.assertFalse(entry.similarity_measured)
        entry.select_candidate(0)
        self.assertTrue(entry.similarity_measured)

    def test_resetting_a_result_clears_the_flag(self):
        entry = ImageEntry(path="/tmp/a.png", candidates=[_measured()])
        entry.select_candidate(0)
        entry.reset_result()
        self.assertIsNone(entry.similarity)
        self.assertFalse(entry.similarity_measured)

    def test_dropping_every_candidate_clears_the_flag(self):
        """The no-match branch clears similarity; leaving the flag set
        would mark a nonexistent number as measured."""
        entry = ImageEntry(path="/tmp/a.png",
                           candidates=[_measured(remote_available=False)])
        entry.select_candidate(0)
        self.assertEqual(entry.drop_unavailable_candidates(), 1)
        self.assertEqual(entry.status, MatchStatus.NOT_FOUND)
        self.assertIsNone(entry.similarity)
        self.assertFalse(entry.similarity_measured)


class TestTheFlagSurvivesARestart(unittest.TestCase):
    """The codec is shared by the search cache and the session store, so
    one omission lost the flag in both. Losing it costs twice: the
    numbers stop being distinguishable, AND similarity_check re-measures
    everything it reads as unmeasured, re-downloading thumbnails to redo
    work already done."""

    def _roundtrip(self, candidate) -> MatchCandidate:
        import json

        from core.search_cache import _candidate_from_dict, _candidate_to_dict
        # Through JSON, not just the dicts: the cache is a file, and a
        # value that cannot be encoded would pass a dict-only test.
        return _candidate_from_dict(json.loads(json.dumps(_candidate_to_dict(candidate))))

    def test_a_measured_candidate_is_still_measured_after_a_reload(self):
        self.assertTrue(self._roundtrip(_measured()).similarity_measured)

    def test_an_ordinal_candidate_is_still_unmeasured_after_a_reload(self):
        restored = self._roundtrip(_ordinal())
        self.assertFalse(restored.similarity_measured)
        self.assertEqual(restored.similarity, 80.0)

    def test_a_cache_entry_written_before_the_flag_existed_reads_as_unmeasured(self):
        """The honest reading: such an entry may hold either kind of
        number, and claiming it was measured would be a guess."""
        from core.search_cache import _candidate_from_dict
        old = {"url": "https://danbooru.donmai.us/posts/1", "similarity": 88.0,
               "engine": "IQDB"}
        self.assertFalse(_candidate_from_dict(old).similarity_measured)

    def test_a_restored_measured_candidate_is_not_queued_for_re_measuring(self):
        """similarity_check picks up exactly the candidates whose flag is
        clear, so the persistence bug and the re-download it caused are
        the same bug."""
        from core import engines
        restored = self._roundtrip(_measured(engine="Google Lens", thumb_url="t"))
        self.assertFalse(engines.reports_real_similarity(restored.engine))
        needs_measuring = (not restored.similarity_measured
                           and not engines.reports_real_similarity(restored.engine))
        self.assertFalse(needs_measuring)

    def test_the_session_restores_the_flag_onto_the_entry(self):
        from core.session import _entry_from_dict, _entry_to_dict
        entry = ImageEntry(path="/tmp/a.png", candidates=[_ordinal(), _measured()])
        entry.select_candidate(1)
        restored = _entry_from_dict(_entry_to_dict(entry))
        self.assertTrue(restored.similarity_measured)
        restored.select_candidate(0)
        self.assertFalse(restored.similarity_measured)


class TestHowTheNumberIsSaid(unittest.TestCase):
    def test_a_measured_score_reads_as_an_ordinary_number(self):
        self.assertEqual(similarity_label(91.6, True), "92%")

    def test_a_ranking_is_marked(self):
        self.assertEqual(similarity_label(80.0, False), "~80%")

    def test_no_match_says_nothing_either_way(self):
        self.assertEqual(similarity_label(None, False), "")
        self.assertEqual(similarity_label(None, True), "")
        self.assertEqual(similarity_tooltip(None, True), "")

    def test_the_tooltip_explains_which_kind_it_is(self):
        self.assertIn("measured by comparing", similarity_tooltip(92.0, True))
        self.assertIn("not a measurement", similarity_tooltip(80.0, False))


class TestTheExportSaysWhichNumbersAreReal(unittest.TestCase):
    def _row(self, entry) -> dict:
        from core.export import FIELDS
        return {f.key: f.extract(entry) for f in FIELDS}

    def test_the_companion_field_sits_with_the_number(self):
        from core.export import HEADERS
        self.assertIn("similarity_measured", HEADERS)
        self.assertEqual(HEADERS.index("similarity_measured"),
                         HEADERS.index("similarity") + 1)

    def test_a_measured_and_an_ordinal_row_export_differently(self):
        measured_entry = ImageEntry(path="/tmp/a.png", candidates=[_measured()])
        measured_entry.select_candidate(0)
        ordinal_entry = ImageEntry(path="/tmp/b.png", candidates=[_ordinal()])
        ordinal_entry.select_candidate(0)
        self.assertIs(self._row(measured_entry)["similarity_measured"], True)
        self.assertIs(self._row(ordinal_entry)["similarity_measured"], False)

    def test_a_row_with_no_match_exports_blank_rather_than_false(self):
        """"False" next to an empty similarity would read as "measured and
        found to be nothing"."""
        self.assertEqual(self._row(ImageEntry(path="/tmp/c.png"))["similarity_measured"], "")


class TestThePreviewPanelSaysIt(unittest.TestCase):
    def _entry(self, measured):
        entry = ImageEntry(path="/tmp/a.png", booru_name="Danbooru",
                           local_width=1000, local_height=1000)
        entry.similarity = 76.0
        entry.similarity_measured = measured
        return entry

    def test_a_measured_caption_is_unchanged(self):
        from gui.preview_text import matched_caption
        self.assertEqual(matched_caption(self._entry(True)),
                         "Matched image: Danbooru (76% similar)")

    def test_an_ordinal_caption_says_so_in_words(self):
        from gui.preview_text import matched_caption
        self.assertEqual(matched_caption(self._entry(False)),
                         "Matched image: Danbooru (~76% similar - ranking, not measured)")

    def test_the_banner_marks_a_ranking(self):
        from gui.preview_text import comparison_banner_text
        candidate = MatchCandidate(url="u", width=2000, height=2000)
        self.assertIn("76% similar", comparison_banner_text(self._entry(True), candidate))
        self.assertNotIn("ranking", comparison_banner_text(self._entry(True), candidate))
        self.assertIn("~76% similar (ranking)",
                      comparison_banner_text(self._entry(False), candidate))


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestTheTableColumnSaysIt(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtGui import QIcon
        from gui.image_table_model import ImageTableModel
        self.entries = []
        self.model = ImageTableModel(self.entries, lambda e: QIcon())

    def _add(self, measured):
        entry = ImageEntry(path="/tmp/a.png")
        entry.similarity = 80.0
        entry.similarity_measured = measured
        self.entries.append(entry)
        self.model.refresh_all()
        return entry

    def _cell(self, role):
        from gui.image_table_model import COL_SIMILARITY
        return self.model.data(self.model.index(0, COL_SIMILARITY), role)

    def test_a_ranking_is_marked_in_the_cell(self):
        from PyQt6.QtCore import Qt
        self._add(False)
        self.assertEqual(self._cell(Qt.ItemDataRole.DisplayRole), "~80%")

    def test_a_measured_score_stays_a_plain_number(self):
        from PyQt6.QtCore import Qt
        self._add(True)
        self.assertEqual(self._cell(Qt.ItemDataRole.DisplayRole), "80%")

    def test_only_a_ranking_gets_a_chip(self):
        from gui.image_table_model import CHIP_ROLE
        self._add(False)
        self.assertEqual(self._cell(CHIP_ROLE), ESTIMATE_CHIP)
        self.entries[0].similarity_measured = True
        self.assertIsNone(self._cell(CHIP_ROLE))

    def test_an_unmatched_row_gets_neither_mark_nor_chip(self):
        from PyQt6.QtCore import Qt
        from gui.image_table_model import CHIP_ROLE
        entry = ImageEntry(path="/tmp/a.png")
        self.entries.append(entry)
        self.model.refresh_all()
        self.assertEqual(self._cell(Qt.ItemDataRole.DisplayRole), "")
        self.assertIsNone(self._cell(CHIP_ROLE))

    def test_the_tooltip_explains_the_mark(self):
        from PyQt6.QtCore import Qt
        self._add(False)
        self.assertIn("not a measurement", self._cell(Qt.ItemDataRole.ToolTipRole))

    def test_the_chip_key_has_a_glyph_and_weight(self):
        """A key with no glyph paints as plain text, which would silently
        drop the marker back to exactly what it was before. Mode-
        independent (gui/theme.py's docstring) - glyph and weight-tier
        don't change with the theme, only the tier's resolved colour
        does - so this no longer needs the per-mode loop."""
        from gui import theme
        self.assertIsNotNone(theme.status_glyph(ESTIMATE_CHIP))
        self.assertIsNotNone(theme.status_weight(ESTIMATE_CHIP))

    def test_the_chip_keeps_the_number_rather_than_replacing_it(self):
        """Status and Sent chips substitute a short word for the cell's
        text. Here the number IS the content, so the key must stay out of
        CHIP_LABELS."""
        from gui import theme
        self.assertEqual(theme.chip_label(ESTIMATE_CHIP, "~80%"), "~80%")
