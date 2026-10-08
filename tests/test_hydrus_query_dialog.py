"""Files > Query Hydrus: tags in, working-list rows out (DAN-49, stretch).

gui/hydrus_query_dialog.py sat at 36% - the construction was covered by
the DAN-21 harness, the two handlers were not. Both of them touch the
user's data: `_do_search` reads their library, and `_do_import` downloads
files to a temp dir and builds the ImageEntry rows the rest of the app
then works on. The duplicate-skipping in particular is the kind of thing
that looks right until somebody imports the same selection twice.

The dialog is constructed for real, offscreen, with its HydrusClient
replaced - so what is under test is the wiring between the list widget,
the client calls and the entries produced, not Qt.

`gui.message` is stubbed throughout: a real QMessageBox would block on
exec(). The stubs also make "did the user get told" assertable, which is
half the behaviour in a dialog whose failures are all partial.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-query-dialog-")

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


def _hydrus_file(file_id, file_hash, mime="image/png", width=800, height=600, tags=None):
    from core.hydrus_client import HydrusFile
    return HydrusFile(file_id=file_id, hash=file_hash, mime=mime, width=width, height=height,
                      tags=list(tags or []))


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class _Dialog(unittest.TestCase):
    def setUp(self):
        from core.config import Settings
        from gui.hydrus_query_dialog import HydrusQueryDialog

        self.settings = Settings()
        self.messages = {"warning": [], "critical": [], "information": []}
        patcher = patch.multiple(
            "gui.message",
            warning=lambda parent, title, text, **kw: self.messages["warning"].append(text),
            critical=lambda parent, title, text, **kw: self.messages["critical"].append(text),
            information=lambda parent, title, text, **kw: self.messages["information"].append(text),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.dialog = HydrusQueryDialog(self.settings)
        # The dialog's OWN set, deliberately. `existing_hashes or set()`
        # means an empty set passed in is replaced rather than adopted -
        # see test_an_empty_existing_set_is_not_adopted below, which pins
        # that rather than letting these tests trip over it.
        self.existing = self.dialog.existing_hashes
        self.addCleanup(self.dialog.deleteLater)
        self.client = MagicMock()
        self.dialog.client = self.client

    def _search(self, tags_text, file_ids=(1,), metadata=None):
        self.dialog.tags_edit.setPlainText(tags_text)
        self.client.search_files.return_value = list(file_ids)
        self.client.get_file_metadata.return_value = list(
            metadata if metadata is not None else [_hydrus_file(1, "aa" * 32)])
        self.dialog._do_search()

    def _select(self, *rows):
        from PyQt6.QtCore import QItemSelectionModel
        model = self.dialog.results_list.selectionModel()
        model.clearSelection()
        for row in rows:
            model.select(self.dialog.results_list.model().index(row, 0),
                         QItemSelectionModel.SelectionFlag.Select)

    def _import(self, accepted=None):
        """Runs _do_import with accept() stubbed, so the dialog does not
        need an event loop to close."""
        with patch.object(self.dialog, "accept") as accept:
            self.dialog._do_import()
        if accepted is not None:
            self.assertEqual(accept.called, accepted)
        return accept


class TestConstruction(_Dialog):
    def test_a_non_empty_existing_set_is_adopted_by_reference(self):
        """main_window hands in the hashes already in the working list and
        relies on the dialog marking them; it does not read the set back,
        which is why the aliasing below is harmless there."""
        from gui.hydrus_query_dialog import HydrusQueryDialog
        caller_set = {"ab" * 32}
        dialog = HydrusQueryDialog(self.settings, existing_hashes=caller_set)
        self.addCleanup(dialog.deleteLater)
        self.assertIs(dialog.existing_hashes, caller_set)

    def test_an_empty_existing_set_is_not_adopted(self):
        """`existing_hashes or set()` - an empty set is falsy, so the
        dialog builds its own and the caller's never sees the imports.
        Pinned as current behaviour, not endorsed: main_window's only call
        site discards its set afterwards, so nothing depends on it either
        way, but a future caller expecting to be updated would be wrong.
        """
        from gui.hydrus_query_dialog import HydrusQueryDialog
        caller_set = set()
        dialog = HydrusQueryDialog(self.settings, existing_hashes=caller_set)
        self.addCleanup(dialog.deleteLater)
        self.assertIsNot(dialog.existing_hashes, caller_set)

    def test_the_client_is_built_from_the_hydrus_settings_it_was_given(self):
        from gui.hydrus_query_dialog import HydrusQueryDialog
        self.settings.hydrus.access_key = "KEY"
        dialog = HydrusQueryDialog(self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertIs(dialog.client.settings, self.settings.hydrus)

    def test_several_files_can_be_selected_at_once(self):
        """Importing one file at a time from a tag search would make the
        dialog close after every file."""
        from PyQt6.QtWidgets import QListWidget
        self.assertEqual(self.dialog.results_list.selectionMode(),
                         QListWidget.SelectionMode.ExtendedSelection)


class TestSearch(_Dialog):
    def test_one_tag_per_line_is_anded_and_blank_lines_are_dropped(self):
        self._search("cat\n\n  dog  \n")
        self.client.search_files.assert_called_once_with(["cat", "dog"])

    def test_no_tags_at_all_warns_and_asks_hydrus_nothing(self):
        """A tagless /search_files is "every file in the library", which
        for a real Hydrus is not a query anybody meant to run."""
        self.dialog.tags_edit.setPlainText("   \n\n")
        self.dialog._do_search()
        self.client.search_files.assert_not_called()
        self.assertEqual(len(self.messages["warning"]), 1)

    def test_each_matching_file_becomes_one_row_carrying_its_details(self):
        self._search("cat", metadata=[
            _hydrus_file(7, "ab" * 32, mime="image/jpeg", width=1920, height=1080,
                         tags=["cat", "blue"]),
            _hydrus_file(8, "cd" * 32),
        ])
        rows = [self.dialog.results_list.item(i).text()
                for i in range(self.dialog.results_list.count())]
        self.assertEqual(len(rows), 2)
        self.assertIn(("ab" * 32)[:12], rows[0])
        self.assertIn("1920x1080", rows[0])
        self.assertIn("2 tag(s)", rows[0])
        self.assertEqual(self.dialog._file_ids, [7, 8])

    def test_a_file_already_in_the_working_list_is_marked_as_such(self):
        """So the user is not left wondering why their selection imported
        fewer rows than they picked."""
        self.existing.add("ab" * 32)
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32)])
        self.assertIn("[already in list]", self.dialog.results_list.item(0).text())

    def test_no_matches_says_so_rather_than_leaving_an_empty_list(self):
        self._search("nothing", file_ids=[], metadata=[])
        self.assertEqual(self.dialog.results_list.count(), 0)
        self.assertEqual(len(self.messages["information"]), 1)
        # No file ids means no point asking for metadata.
        self.client.get_file_metadata.assert_not_called()

    def test_a_second_search_replaces_the_first_results_rather_than_appending(self):
        """_file_ids and the list widget are indexed together by _do_import,
        so a stale row would import the wrong file."""
        self._search("cat", metadata=[_hydrus_file(1, "aa" * 32), _hydrus_file(2, "bb" * 32)])
        self._search("dog", metadata=[_hydrus_file(9, "cc" * 32)])
        self.assertEqual(self.dialog.results_list.count(), 1)
        self.assertEqual(self.dialog._file_ids, [9])

    def test_a_hydrus_failure_is_reported_and_leaves_the_previous_results_alone(self):
        from core.hydrus_client import HydrusError
        self._search("cat", metadata=[_hydrus_file(1, "aa" * 32)])
        self.client.search_files.side_effect = HydrusError(
            "Hydrus rejected the access key (401) - check Settings > Hydrus", 401)
        self.dialog.tags_edit.setPlainText("dog")
        self.dialog._do_search()
        self.assertEqual(len(self.messages["critical"]), 1)
        self.assertIn("401", self.messages["critical"][0])
        self.assertEqual(self.dialog.results_list.count(), 1)

    def test_a_metadata_failure_is_reported_too(self):
        from core.hydrus_client import HydrusError
        self.dialog.tags_edit.setPlainText("cat")
        self.client.search_files.return_value = [1]
        self.client.get_file_metadata.side_effect = HydrusError("HTTP 500", 500)
        self.dialog._do_search()
        self.assertEqual(len(self.messages["critical"]), 1)


class TestImport(_Dialog):
    def test_nothing_selected_warns_and_downloads_nothing(self):
        self._search("cat")
        self._select()
        self._import(accepted=False)
        self.client.download_file.assert_not_called()
        self.assertEqual(len(self.messages["warning"]), 1)

    def test_a_selected_file_is_downloaded_and_becomes_an_entry(self):
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32, mime="image/jpeg")])
        self._select(0)
        self._import(accepted=True)

        self.client.download_file.assert_called_once()
        file_id, dest = self.client.download_file.call_args[0]
        self.assertEqual(file_id, 7)
        # The extension comes off the mime Hydrus reported, so the file on
        # disk is something PIL and the search engines can open.
        self.assertTrue(dest.endswith(("ab" * 32) + ".jpeg"), dest)

        self.assertEqual(len(self.dialog.imported_entries), 1)
        entry = self.dialog.imported_entries[0]
        self.assertEqual(entry.path, dest)
        self.assertEqual(entry.hydrus_file_id, 7)
        self.assertEqual(entry.hydrus_hash, "ab" * 32)

    def test_a_missing_mime_falls_back_to_jpg_rather_than_no_extension(self):
        """An extensionless path defeats every "is this an image" check
        downstream, including the one that decides whether to search it."""
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32, mime=None)])
        self._select(0)
        self._import()
        self.assertTrue(self.client.download_file.call_args[0][1].endswith(".jpg"))

    def test_hydrus_tags_arrive_on_the_entry_marked_as_hydrus_tags(self):
        from core.models import TagSource
        self._search("cat", metadata=[
            _hydrus_file(7, "ab" * 32, tags=["creator:somebody", "blue_eyes"])])
        self._select(0)
        self._import()
        entry = self.dialog.imported_entries[0]
        by_name = {t.name: t for t in entry.tags}
        self.assertEqual(set(by_name), {"somebody", "blue_eyes"})
        self.assertEqual(by_name["somebody"].namespace, "creator")
        # An unnamespaced tag must get None, not "" - an empty namespace
        # silently breaks dedup (see the test_parsers_and_tags regression).
        self.assertIsNone(by_name["blue_eyes"].namespace)
        for tag in entry.tags:
            self.assertEqual(tag.source, TagSource.HYDRUS)

    def test_a_namespaced_tag_keeps_the_rest_of_its_colons(self):
        """`series:fate/stay night: heaven's feel` splits once, not on every
        colon."""
        from gui.hydrus_query_dialog import _parse_tag
        tag = _parse_tag("series:a: b")
        self.assertEqual((tag.namespace, tag.name), ("series", "a: b"))

    def test_the_namespace_remap_setting_is_applied_to_imported_tags(self):
        self.settings.enable_tag_namespace_remap = True
        self.settings.tag_namespace_remap = {"creator": "artist"}
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32, tags=["creator:somebody"])])
        self._select(0)
        self._import()
        self.assertEqual(self.dialog.imported_entries[0].tags[0].namespace, "artist")

    def test_a_file_already_in_the_working_list_is_skipped_and_not_downloaded(self):
        self.existing.add("ab" * 32)
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32), _hydrus_file(8, "cd" * 32)])
        self._select(0, 1)
        self._import()
        self.assertEqual(self.dialog.skipped_duplicate_count, 1)
        self.assertEqual([c[0][0] for c in self.client.download_file.call_args_list], [8])
        self.assertEqual(len(self.dialog.imported_entries), 1)

    def test_the_same_file_selected_twice_in_one_go_is_only_imported_once(self):
        """Hydrus can hold one file under two rows of a search (it cannot,
        but a re-search plus a stale selection can produce the same shape),
        and each import adds its hash to existing_hashes precisely so the
        second occurrence is caught within the same selection."""
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32), _hydrus_file(8, "ab" * 32)])
        self._select(0, 1)
        self._import()
        self.assertEqual(self.client.download_file.call_count, 1)
        self.assertEqual(self.dialog.skipped_duplicate_count, 1)
        self.assertEqual(len(self.dialog.imported_entries), 1)

    def test_an_imported_hash_is_recorded_so_a_later_query_marks_it(self):
        self._search("cat", metadata=[_hydrus_file(7, "ab" * 32)])
        self._select(0)
        self._import()
        self.assertIn("ab" * 32, self.existing)

    def test_one_failed_download_costs_that_file_and_not_the_selection(self):
        """The failure mode worth naming: a 403 on file three of ten must
        not throw away the seven that worked."""
        from core.hydrus_client import HydrusError
        self._search("cat", metadata=[_hydrus_file(i, f"{i:02x}" * 32) for i in (1, 2, 3)])
        self._select(0, 1, 2)

        def _download(file_id, dest):
            if file_id == 2:
                raise HydrusError("Access key lacks permission for this action (403)", 403)

        self.client.download_file.side_effect = _download
        self._import(accepted=True)
        self.assertEqual([e.hydrus_file_id for e in self.dialog.imported_entries], [1, 3])
        self.assertEqual(len(self.messages["warning"]), 1)
        self.assertIn("403", self.messages["warning"][0])
        # A file that failed to download must not be recorded as present.
        self.assertNotIn("02" * 32, self.existing)

    def test_every_file_failing_still_closes_the_dialog_with_no_entries(self):
        from core.hydrus_client import HydrusError
        self._search("cat", metadata=[_hydrus_file(1, "aa" * 32)])
        self._select(0)
        self.client.download_file.side_effect = HydrusError("HTTP 500", 500)
        self._import(accepted=True)
        self.assertEqual(self.dialog.imported_entries, [])

    def test_the_download_directory_is_a_throwaway_temp_dir(self):
        """These files are working copies for IQDB/SauceNAO to read, not
        somewhere in the user's own folders."""
        self._search("cat")
        self._select(0)
        self._import()
        dest = self.client.download_file.call_args[0][1]
        self.assertTrue(os.path.dirname(dest).startswith(tempfile.gettempdir()), dest)
        self.assertIn("hatate-linux-", dest)


if __name__ == "__main__":
    unittest.main()
