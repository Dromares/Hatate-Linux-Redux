"""Finding a setting among eight tabs of them."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtWidgets import QApplication

from core.config import Settings
from gui import settings_search

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestMatching(unittest.TestCase):
    ENTRIES = [
        (0, "General", "Delay between searches", object()),
        (4, "Hydrus", "Access key", object()),
        (3, "SauceNAO", "Pause searching when the daily quota runs out", object()),
        (6, "Site Logins", "Sankaku cookies", object()),
    ]

    def test_words_match_in_any_order(self):
        """Nobody remembers the wording, only roughly what it said - so a
        plain substring match finds far too little."""
        hits = settings_search.matches(self.ENTRIES, "quota pause")
        self.assertEqual([h[2] for h in hits],
                         ["Pause searching when the daily quota runs out"])

    def test_the_tab_name_counts_as_part_of_the_name(self):
        """People search with half a location and half a name. The Hydrus
        field is labelled just "Access key", so matching the label alone
        answers nothing for a query that is perfectly clear."""
        hits = settings_search.matches(self.ENTRIES, "hydrus key")
        self.assertEqual([h[2] for h in hits], ["Access key"])

    def test_an_empty_query_matches_nothing(self):
        """Rather than everything, which would dump the whole dialog into
        a 150px list the moment the box is focused and cleared."""
        for query in ("", "   "):
            with self.subTest(query=query):
                self.assertEqual(settings_search.matches(self.ENTRIES, query), [])

    def test_shorter_names_come_first(self):
        hits = settings_search.matches(self.ENTRIES, "s")
        self.assertLessEqual(len(hits[0][2]), len(hits[-1][2]))

    def test_matching_ignores_case(self):
        self.assertTrue(settings_search.matches(self.ENTRIES, "SANKAKU COOKIES"))


class TestHighlightReentrancy(unittest.TestCase):
    """A double-click on a search result fires highlight() twice on the
    same widget before the first revert has run (settings_dialog wires
    both itemClicked and itemActivated to the same handler). The second
    call must not corrupt what the first call restores to.

    The reverts are driven by hand instead of a real QTimer/event loop:
    a genuine wait-for-timers loop nested inside the wider test run was
    observed to crash the interpreter (segfault) when this module ran
    alongside the rest of the suite, so the scheduling is faked and the
    two pending callbacks are invoked directly, in the order Qt would
    have fired them.
    """

    def test_a_second_highlight_inside_the_revert_window_still_restores(self):
        from unittest.mock import patch

        from PyQt6.QtWidgets import QLabel

        from gui import settings_search as ss

        _app()
        label = QLabel("Hint")
        label.setObjectName("Hint")

        scheduled = []
        with patch.object(ss.QTimer, "singleShot",
                           side_effect=lambda _ms, cb: scheduled.append(cb)):
            ss.highlight(label)
            # A second click landing while the first highlight is still up.
            ss.highlight(label)

        self.assertEqual(len(scheduled), 2)
        for revert in scheduled:
            revert()

        self.assertEqual(label.objectName(), "Hint")


class TestIndexingTheRealDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()
        from gui.settings_dialog import SettingsDialog
        cls.dialog = SettingsDialog(Settings())

    @classmethod
    def tearDownClass(cls):
        cls.dialog.deleteLater()

    def test_it_finds_a_useful_number_of_settings(self):
        """A bare handful would mean the walk is missing whole tabs."""
        self.assertGreater(len(self.dialog._search_index), 40)

    def test_every_tab_is_reachable_by_its_own_name(self):
        """Shortcuts keeps its actions in a table rather than in labels,
        so without an entry for the tab itself, searching "shortcuts"
        answers nothing at all."""
        tabs = {name for _i, name, _n, _w in self.dialog._search_index}
        for expected in ("General", "Engine", "Import", "SauceNAO", "Hydrus",
                         "Tag Namespaces", "Site Logins", "Shortcuts"):
            with self.subTest(tab=expected):
                self.assertIn(expected, tabs)
                self.assertTrue(
                    settings_search.matches(self.dialog._search_index, expected))

    def test_explanations_are_not_indexed_as_names(self):
        """The dialog is full of paragraph-long labels explaining what an
        engine does. Indexed as setting names they swamp every result."""
        for _i, _tab, name, _w in self.dialog._search_index:
            with self.subTest(name=name[:40]):
                self.assertLessEqual(len(name), settings_search.MAX_NAME_LENGTH)

    def test_a_known_setting_is_findable(self):
        hits = settings_search.matches(self.dialog._search_index, "cookies")
        self.assertTrue(hits, "expected the Site Logins cookie fields")


if __name__ == "__main__":
    unittest.main()
