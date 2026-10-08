"""Recording where a match came from as a note on the file in Hydrus
(DAN-78, slate #10 of the DAN-64 cycle-9 slate).

Four separate things, kept apart because they fail in different ways:

  * the TEXT - core/provenance_note.py, which turns an entry into the
    note's body. The interesting part is what it refuses to say: a bare
    percentage for an ascii2d/Google ordinal, or a note at all for a file
    with no match.
  * the WIRING - core/hydrus_import.write_provenance_note: off costs
    nothing, a file Hydrus is not holding is left alone, and a missing
    permission or an old client degrades into a named sentence without
    taking the import down with it.
  * the REQUEST - that what goes on the wire is the shape Hydrus's Client
    API documents for /add_notes/set_notes, with the permission id
    Hydrus's own docs give.
  * the PATHS - that all three ways a file reaches Hydrus write the note,
    including the URL-importer one, which is the half DAN-71 found missing
    for duplicate relationships and which would go the same way here.

RUN AGAINST PARENT 875df5c8 (`origin/main`): 38 of these 44 tests fail
there - 35 as errors on the absent `core.provenance_note`,
`write_provenance_note`, `finish_url_import`, `HydrusClient.set_note`,
`PERMISSION_EDIT_FILE_NOTES` and `Settings.write_hydrus_provenance_note`,
and 3 as plain assertion failures where the note simply never gets
written (TestFeatureOn.test_end_to_end_through_download_and_send and one
positive in each of the two worker-wiring classes).

The 6 that pass against the parent pass deliberately, and would be
worthless if they did not:

  * TestFeatureOff.test_a_whole_import_is_unchanged - the
    byte-identical-when-disabled regression test. Passing on BOTH sides of
    the commit is exactly what it is for.
  * the four negatives that guard against this change OVER-reaching:
    TestAutoImportWiring's
    test_nothing_is_confirmed_or_noted_when_every_setting_is_off,
    test_removal_still_confirms_on_its_own_with_the_note_off and
    test_an_unconfirmed_import_gets_no_note, plus
    TestBatchPollWorkerWiring.test_nothing_is_written_when_the_setting_is_off.
    The confirmation poll's condition gained a third reason to fire and
    must not have lost its first, and nothing may be written for an import
    Hydrus never confirmed. The parent is already correct in those cases,
    which is precisely why they are pinned here.
  * TestPermissionTable.test_the_permission_name_is_hydrus_own - the name
    "Edit File Notes" was already in HYDRUS_PERMISSIONS before this
    change, just unused. It guards the table, not the feature.
"""
import hashlib
import io
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from PIL import Image

try:
    from PyQt6.QtCore import Qt
    HAVE_QT = True
except ImportError:                                  # pragma: no cover - environment
    HAVE_QT = False


def _png(size=64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (20, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


LOCAL_PNG = _png()
DOWNLOADED_PNG = _png(96)

# A fixed moment, so the rendered date is an assertion rather than a race.
MOMENT = time.mktime((2026, 3, 4, 17, 8, 0, 0, 0, -1))


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hatate-prov-note-")
        self.local_path = os.path.join(self.tmp, "local.png")
        with open(self.local_path, "wb") as fh:
            fh.write(LOCAL_PNG)
        self.local_hash = hashlib.sha256(LOCAL_PNG).hexdigest()
        self.new_hash = hashlib.sha256(DOWNLOADED_PNG).hexdigest()

        import core.hydrus_import as hi_module
        self.hi = hi_module

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entry(self, measured=True, similarity=92.0, engine="IQDB",
               site="Danbooru", url="https://danbooru.donmai.us/posts/123"):
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path=self.local_path)
        entry.hydrus_hash = self.local_hash
        entry.candidates = [MatchCandidate(
            url=url or "", source_name=site, similarity=similarity or 0.0,
            similarity_measured=measured, engine=engine,
            direct_file_url="https://danbooru.donmai.us/big.png")]
        entry.select_candidate(0)
        if similarity is None:
            entry.similarity = None
        entry.matched_url = url
        entry.booru_name = site
        entry.tags = []
        return entry

    def _client(self, present=True, missing_permission=None):
        client = MagicMock()

        def _import(path):
            with open(path, "rb") as fh:
                return {"hash": hashlib.sha256(fh.read()).hexdigest(), "status": 1}

        client.import_file.side_effect = _import
        client.file_id_for_hash.return_value = 41

        def _download(_file_id, dest_path):
            with open(dest_path, "wb") as fh:
                fh.write(DOWNLOADED_PNG)

        # Only the duplicate-relationship half of finish_url_import reads
        # the imported file back; the note never needs the bytes. Answered
        # anyway so the two halves can be seen failing independently.
        client.download_file.side_effect = _download
        # Answers "present" for whatever hash it is asked about, which is
        # what a Hydrus holding the file it just imported does.
        client.deletion_states.side_effect = lambda hashes: {
            h: ("present" if present else "deleted") for h in hashes}
        client.missing_permission.return_value = missing_permission
        return client

    @staticmethod
    def _settings(enabled, note_name=""):
        return SimpleNamespace(write_hydrus_provenance_note=enabled,
                               hydrus_provenance_note_name=note_name,
                               set_hydrus_duplicate_relationships=False,
                               search_timeout=30.0)


class TestNoteText(_Fixture):
    """core/provenance_note.py - what the note actually says."""

    def _text(self, **kwargs):
        from core.provenance_note import provenance_note
        return provenance_note(self._entry(**kwargs), sent_at=MOMENT)

    def test_it_records_the_five_facts_asked_for(self):
        text = self._text()
        self.assertIn("Engine: IQDB", text)
        self.assertIn("Site: Danbooru", text)
        self.assertIn("92%", text)
        self.assertIn("https://danbooru.donmai.us/posts/123", text)
        self.assertIn("2026-03-04 17:08", text)

    def test_it_says_which_program_wrote_it(self):
        """A note in somebody's library with no attribution is a mystery
        note. The tool's name is the first line."""
        self.assertTrue(self._text().startswith("Match found by Hatate-Linux-Redux."))

    def test_a_measured_similarity_is_written_as_a_plain_number(self):
        from core.similarity_display import ESTIMATE_MARK, MEASURED_NOTE
        text = self._text(measured=True)
        self.assertIn(f"Similarity: 92% - {MEASURED_NOTE}", text)
        self.assertNotIn(ESTIMATE_MARK, text)

    def test_an_unmeasured_similarity_is_marked_and_explained(self):
        """THE point of this test file. ascii2d and the Google engines
        report no similarity at all - what the app shows for one of their
        results is that result's POSITION in the list (see DAN-69 /
        core/similarity_display.py). Writing "80%" into a user's library
        for one of those would be a claim this app cannot support, and a
        note is far more durable than a table cell."""
        from core.similarity_display import UNMEASURED_NOTE
        text = self._text(measured=False, similarity=80.0, engine="ascii2d")
        self.assertIn("~80%", text)
        self.assertIn(UNMEASURED_NOTE, text)

    def test_a_missing_fact_is_left_out_rather_than_written_as_unknown(self):
        text = self._text(similarity=None, site=None)
        self.assertNotIn("Similarity", text)
        self.assertNotIn("Site:", text)
        self.assertNotIn("None", text)
        self.assertIn("Engine: IQDB", text)     # what IS known still gets written

    def test_either_half_of_a_match_on_its_own_is_still_worth_a_note(self):
        """A URL with no engine behind it (a session restored from an
        older format), or an engine with the URL cleared off the entry.
        Each is half a provenance, and half is more than the library keeps
        today - so it gets written rather than dropped for being
        incomplete."""
        from core.models import ImageEntry
        from core.provenance_note import provenance_note
        url_only = ImageEntry(path=self.local_path)
        url_only.matched_url = "https://example.com/posts/1"
        text = provenance_note(url_only, sent_at=MOMENT)
        self.assertIn("https://example.com/posts/1", text)
        self.assertNotIn("Engine:", text)

        entry = self._entry()
        entry.matched_url = None
        text = provenance_note(entry, sent_at=MOMENT)
        self.assertIn("Engine: IQDB", text)
        self.assertNotIn("Match:", text)

    def test_no_match_means_no_note_at_all(self):
        """An upload of a file nobody matched has no provenance to record,
        and "imported by Hatate" on its own is noise in a library the user
        has to live with."""
        from core.models import ImageEntry
        from core.provenance_note import provenance_note
        self.assertIsNone(provenance_note(ImageEntry(path=self.local_path), sent_at=MOMENT))

    def test_the_date_defaults_to_now(self):
        from core.provenance_note import provenance_note
        text = provenance_note(self._entry())
        self.assertIn(time.strftime("%Y-%m-%d", time.localtime()), text)

    def test_the_note_name_falls_back_when_the_setting_is_blank(self):
        from core.provenance_note import DEFAULT_NOTE_NAME, note_name
        self.assertEqual(note_name(""), DEFAULT_NOTE_NAME)
        self.assertEqual(note_name(None), DEFAULT_NOTE_NAME)
        self.assertEqual(note_name("   "), DEFAULT_NOTE_NAME)
        self.assertEqual(note_name(" my notes "), "my notes")

    def test_the_default_name_says_which_program_owns_it(self):
        """The app REPLACES this note on every send, so its name has to be
        obviously the app's rather than something a user would pick."""
        from core.provenance_note import DEFAULT_NOTE_NAME
        self.assertIn("hatate", DEFAULT_NOTE_NAME.lower())


class TestFeatureOff(_Fixture):
    """With the setting off, an import does exactly what it did before
    this existed."""

    def test_it_is_off_by_default(self):
        from core.config import Settings
        self.assertIs(Settings().write_hydrus_provenance_note, False)

    def test_the_note_name_is_blank_by_default(self):
        from core.config import Settings
        self.assertEqual(Settings().hydrus_provenance_note_name, "")

    def test_a_setting_absent_from_a_saved_config_reads_as_off(self):
        from core.config import Settings
        stored = Settings().__dict__.copy()
        stored.pop("write_hydrus_provenance_note")
        stored.pop("hydrus_provenance_note_name")
        self.assertFalse(Settings(**stored).write_hydrus_provenance_note)

    def test_nothing_is_asked_of_hydrus_and_no_text_is_built(self):
        """Not just "no note call" - no permission probe, no
        deletion_states, and no note rendered either. The flag is checked
        on the first line precisely so that off costs nothing."""
        client = self._client()
        with patch("core.provenance_note.provenance_note") as render:
            warning = self.hi.write_provenance_note(
                self._entry(), client, self._settings(False), self.new_hash)
        self.assertIsNone(warning)
        render.assert_not_called()
        client.set_note.assert_not_called()
        client.deletion_states.assert_not_called()
        client.missing_permission.assert_not_called()

    def test_settings_of_none_is_treated_as_off(self):
        """send_file_upload's and download_and_send's `settings` are
        optional and several callers leave them out. That must read as
        off, not crash."""
        client = self._client()
        self.assertIsNone(self.hi.write_provenance_note(
            self._entry(), client, None, self.new_hash))
        client.set_note.assert_not_called()

    @patch("core.remote.download_bytes")
    def test_a_whole_import_is_unchanged(self, download):
        download.return_value = DOWNLOADED_PNG
        entry, client = self._entry(), self._client()
        result = self.hi.download_and_send(entry, client, 30.0, self.tmp, self._settings(False))
        self.assertTrue(result.success)
        self.assertIsNone(result.warning)
        self.assertIsNone(entry.error_message)
        self.assertTrue(entry.sent_to_hydrus)
        self.assertTrue(entry.hydrus_import_confirmed)
        client.set_note.assert_not_called()

    def test_the_url_importer_path_is_unchanged(self):
        client = self._client()
        entry = self._entry()
        warning = self.hi.finish_url_import(
            entry, client, self._settings(False), self.local_hash, self.new_hash)
        self.assertIsNone(warning)
        client.set_note.assert_not_called()
        client.set_file_relationship.assert_not_called()


class TestFeatureOn(_Fixture):
    """What the note write actually asks of Hydrus."""

    def test_the_note_is_written_onto_the_imported_file(self):
        from core.provenance_note import DEFAULT_NOTE_NAME
        client = self._client()
        warning = self.hi.write_provenance_note(
            self._entry(), client, self._settings(True), self.new_hash)
        self.assertIsNone(warning)
        file_hash, name, text = client.set_note.call_args[0]
        self.assertEqual(file_hash, self.new_hash)
        self.assertEqual(name, DEFAULT_NOTE_NAME)
        self.assertIn("https://danbooru.donmai.us/posts/123", text)

    def test_a_configured_name_is_used(self):
        client = self._client()
        self.hi.write_provenance_note(
            self._entry(), client, self._settings(True, "sources"), self.new_hash)
        self.assertEqual(client.set_note.call_args[0][1], "sources")

    def test_nothing_is_written_for_a_file_with_no_match(self):
        from core.models import ImageEntry
        client = self._client()
        self.assertIsNone(self.hi.write_provenance_note(
            ImageEntry(path=self.local_path), client, self._settings(True), self.new_hash))
        client.set_note.assert_not_called()

    def test_nothing_is_written_when_hydrus_is_not_holding_the_file(self):
        """Hydrus answers 200 for a note written against a hash it has
        never seen - and add_file reports a hash for a file it REFUSED as
        previously-deleted too - so an unchecked write would look like it
        worked and leave the user nothing to find."""
        client = self._client(present=False)
        self.assertIsNone(self.hi.write_provenance_note(
            self._entry(), client, self._settings(True), self.new_hash))
        client.set_note.assert_not_called()

    def test_no_hash_writes_nothing(self):
        client = self._client()
        self.assertIsNone(self.hi.write_provenance_note(
            self._entry(), client, self._settings(True), ""))
        client.set_note.assert_not_called()

    @patch("core.remote.download_bytes")
    def test_end_to_end_through_download_and_send(self, download):
        download.return_value = DOWNLOADED_PNG
        entry, client = self._entry(), self._client()
        result = self.hi.download_and_send(entry, client, 30.0, self.tmp, self._settings(True))
        self.assertTrue(result.success)
        self.assertIsNone(result.warning)
        client.set_note.assert_called_once()
        self.assertEqual(client.set_note.call_args[0][0], self.new_hash)

    def test_end_to_end_through_send_file_upload(self):
        entry, client = self._entry(), self._client()
        result = self.hi.send_file_upload(entry, client, self._settings(True))
        self.assertTrue(result.success)
        self.assertIsNone(result.warning)
        client.set_note.assert_called_once()
        # The LOCAL file's hash: this path uploads the user's own copy.
        self.assertEqual(client.set_note.call_args[0][0], self.local_hash)

    def test_end_to_end_through_the_url_importer_path(self):
        """The half DAN-71 found missing for duplicate relationships. A
        user on auto_import_method = "url_importer" must get the note
        too, or the feature is silently half-there."""
        client = self._client()
        warning = self.hi.finish_url_import(
            self._entry(), client, self._settings(True), self.local_hash, self.new_hash)
        self.assertIsNone(warning)
        client.set_note.assert_called_once()
        self.assertEqual(client.set_note.call_args[0][0], self.new_hash)


class TestDegradesGracefully(_Fixture):
    """A Hydrus that cannot do this must cost the user a sentence, not the
    import."""

    @patch("core.remote.download_bytes")
    def _import_with(self, client, download):
        download.return_value = DOWNLOADED_PNG
        entry = self._entry()
        return entry, self.hi.download_and_send(entry, client, 30.0, self.tmp,
                                                self._settings(True))

    def test_a_key_without_the_permission_names_it_and_says_where(self):
        client = self._client(missing_permission=(
            'your Hydrus access key does not have the "Edit File Notes" permission. '
            "Add it in Hydrus: services → review services → local → client api, "
            "then edit your access key and tick it"))
        entry, result = self._import_with(client)

        self.assertTrue(result.success, "the import itself must still succeed")
        self.assertTrue(entry.sent_to_hydrus)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("Edit File Notes", result.warning)
        self.assertIn("review services", result.warning)
        self.assertNotIn("403", result.warning)
        client.set_note.assert_not_called()

    def test_an_old_hydrus_says_so_rather_than_reporting_a_404(self):
        from core.hydrus_client import HydrusClient, HydrusError
        client = self._client()
        # The probe cannot tell: an old client still answers
        # /verify_access_key, it just has no such endpoint to call.
        client.set_note.side_effect = HydrusClient._notes_failure(
            HydrusError("Hydrus returned HTTP 404: ...", 404))
        entry, result = self._import_with(client)

        self.assertTrue(result.success)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("too old", result.warning)
        self.assertIn("/add_notes/set_notes", result.warning)

    def test_any_other_hydrus_failure_still_leaves_the_import_standing(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        client.set_note.side_effect = HydrusError("the wheels came off", 500)
        entry, result = self._import_with(client)

        self.assertTrue(result.success)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("the wheels came off", result.warning)

    def test_a_failed_note_does_not_hide_a_failed_relationship(self):
        """Both pieces of post-import bookkeeping can fail independently,
        and one message overwriting the other would report a partial
        success as a complete one."""
        from core.hydrus_client import HydrusError
        settings = self._settings(True)
        settings.set_hydrus_duplicate_relationships = True
        client = self._client()
        client.set_note.side_effect = HydrusError("no notes today", 500)
        client.set_file_relationship.side_effect = HydrusError("no pairs today", 500)
        warning = self.hi.finish_url_import(
            self._entry(), client, settings, self.local_hash, self.new_hash)
        self.assertIn("no notes today", warning)
        self.assertIn("no pairs today", warning)

    def test_the_upload_path_reports_it_alongside_its_own_warnings(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        client.add_tags.side_effect = HydrusError("tags refused", 500)
        client.set_note.side_effect = HydrusError("notes refused", 500)
        entry = self._entry()
        from core.models import Tag, TagSource
        entry.tags = [Tag(name="x", source=TagSource.BOORU)]
        result = self.hi.send_file_upload(entry, client, self._settings(True))
        self.assertTrue(result.success)
        self.assertIn("tags refused", result.warning)
        self.assertIn("notes refused", result.warning)


class TestClientWrapper(unittest.TestCase):
    """What goes on the wire, against Hydrus's documented shape."""

    def _client(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        return HydrusClient(HydrusSettings(access_key="k"))

    def test_the_note_request_is_the_documented_shape(self):
        client = self._client()
        with patch.object(client, "_post", return_value={}) as post:
            client.set_note("aa" * 32, "hatate: match source", "some text")
        path, body = post.call_args[0]
        self.assertEqual(path, "/add_notes/set_notes")
        self.assertEqual(body, {"hash": "aa" * 32,
                                "notes": {"hatate: match source": "some text"}})

    def test_the_existing_note_is_replaced_rather_than_appended_to(self):
        """merge_cleverly is deliberately not sent: this app owns the note
        it names, and a note that grew a fresh copy of itself on every
        re-send would be worse than no note."""
        client = self._client()
        with patch.object(client, "_post", return_value={}) as post:
            client.set_note("aa" * 32, "n", "t")
        body = post.call_args[0][1]
        self.assertNotIn("merge_cleverly", body)
        self.assertNotIn("extend_existing_note_if_needed", body)

    def test_a_missing_hash_or_name_is_refused_before_the_request(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        for file_hash, name in (("", "n"), ("aa" * 32, "")):
            with self.subTest(file_hash=file_hash, name=name):
                with patch.object(client, "_post") as post:
                    with self.assertRaises(HydrusError):
                        client.set_note(file_hash, name, "t")
                post.assert_not_called()

    def test_403_and_404_become_the_two_messages_a_user_can_act_on(self):
        from core.hydrus_client import HydrusClient, HydrusError
        forbidden = HydrusClient._notes_failure(HydrusError("403 text", 403))
        self.assertIn("Edit File Notes", str(forbidden))
        self.assertIn("review services", str(forbidden))

        old = HydrusClient._notes_failure(HydrusError("404 text", 404))
        self.assertIn("too old", str(old))
        self.assertIn("/add_notes/set_notes", str(old))

        # Anything else is passed through rather than dressed up as a
        # diagnosis it isn't.
        other = HydrusError("disk on fire", 500)
        self.assertIs(HydrusClient._notes_failure(other), other)


class TestPermissionTable(unittest.TestCase):
    def test_the_permission_id_is_hydrus_own(self):
        from core.hydrus_client import PERMISSION_EDIT_FILE_NOTES
        self.assertEqual(PERMISSION_EDIT_FILE_NOTES, 7)

    def test_the_permission_name_is_hydrus_own(self):
        from core.hydrus_client import HYDRUS_PERMISSIONS
        self.assertEqual(HYDRUS_PERMISSIONS[7], "Edit File Notes")

    def test_the_probe_reads_the_notes_permission(self):
        from core.config import HydrusSettings
        from core.hydrus_client import PERMISSION_EDIT_FILE_NOTES, HydrusClient
        client = HydrusClient(HydrusSettings(access_key="k"))
        with patch.object(client, "_get", return_value={"basic_permissions": [0, 1, 3, 7]}):
            self.assertIsNone(client.missing_permission(PERMISSION_EDIT_FILE_NOTES))
        with patch.object(client, "_get", return_value={"basic_permissions": [0, 1, 3]}):
            message = client.missing_permission(PERMISSION_EDIT_FILE_NOTES)
        self.assertIn("Edit File Notes", message)
        self.assertIn("review services", message)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestAutoImportWiring(_Fixture):
    """SearchWorker's url_importer path. The note needs the confirmed hash
    - it is the file the note goes onto - so turning the note on must be
    enough to make the confirmation poll happen. Gating it on the other two
    settings is exactly the shape of the DAN-71 bug, where the feature was
    silently off for anyone who keeps imported rows in the list."""

    def _worker_settings(self, note, remove_after_import=False):
        from core.config import Settings
        settings = Settings()
        settings.auto_import_method = "url_importer"
        settings.auto_import_min_similarity = 80.0
        settings.auto_import_enabled = True
        settings.remove_after_import = remove_after_import
        settings.set_hydrus_duplicate_relationships = False
        settings.write_hydrus_provenance_note = note
        return settings

    def _auto_import(self, settings, client):
        from core.models import MatchStatus
        from workers.search_worker import SearchWorker
        entry = self._entry()
        entry.status = MatchStatus.GOOD
        entry.similarity = 97.0
        worker = SearchWorker([], settings)
        results = []
        worker.auto_imported.connect(lambda e, r: results.append(r),
                                     Qt.ConnectionType.DirectConnection)
        with patch("core.hydrus_import.url_import_refusal", return_value=None), \
             patch("core.hydrus_import._forwarded_url", return_value=None):
            worker._maybe_auto_import(entry, client, self.tmp)
        return entry, results[0] if results else None

    def _client_for_worker(self):
        client = self._client()
        client.get_url_files.return_value = {
            "url_file_statuses": [{"status": 2, "hash": self.new_hash}]}
        return client

    def test_the_note_setting_alone_confirms_the_import_and_writes_the_note(self):
        client = self._client_for_worker()
        _entry, result = self._auto_import(self._worker_settings(True), client)
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed)
        client.set_note.assert_called_once()
        self.assertEqual(client.set_note.call_args[0][0], self.new_hash)

    def test_nothing_is_confirmed_or_noted_when_every_setting_is_off(self):
        """The poll is several extra API calls and up to a minute of
        waiting. Nothing that needs a hash on means nothing is paid for."""
        client = self._client_for_worker()
        with patch("workers.search_worker.poll_single_url_import") as poll:
            _entry, result = self._auto_import(self._worker_settings(False), client)
        poll.assert_not_called()
        self.assertIsNone(result.confirmed)
        client.set_note.assert_not_called()

    def test_removal_still_confirms_on_its_own_with_the_note_off(self):
        """The condition gained a third reason to poll; it did not lose the
        first one."""
        client = self._client_for_worker()
        _entry, result = self._auto_import(
            self._worker_settings(False, remove_after_import=True), client)
        self.assertTrue(result.confirmed)
        client.set_note.assert_not_called()

    def test_an_unconfirmed_import_gets_no_note(self):
        """There is no file in Hydrus yet for a note to go onto."""
        client = self._client_for_worker()
        client.get_url_files.return_value = {"url_file_statuses": []}
        settings = self._worker_settings(True)
        settings.url_import_confirm_timeout = 0.0
        settings.url_import_confirm_interval = 0.0
        _entry, result = self._auto_import(settings, client)
        self.assertFalse(result.confirmed)
        client.set_note.assert_not_called()


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestBatchPollWorkerWiring(_Fixture):
    """HydrusImportPollWorker - the manual "Send URL to Hydrus's Importer"
    batch. The other of the two URL-importer surfaces; it reports through
    entry_resolved's third argument rather than ImportResult.warning."""

    def _run_worker(self, client, settings):
        from workers.hydrus_import_poll_worker import HydrusImportPollWorker
        entry = self._entry()
        worker = HydrusImportPollWorker(client, [(entry, entry.matched_url)],
                                        timeout=0.0, interval=0.0, settings=settings)
        resolved = []
        worker.entry_resolved.connect(lambda *args: resolved.append(args),
                                      Qt.ConnectionType.DirectConnection)
        worker.run()                     # run() directly: no thread needed to test it
        return entry, resolved

    def _client_for_worker(self, **kwargs):
        client = self._client(**kwargs)
        client.get_url_files.return_value = {
            "url_file_statuses": [{"status": 2, "hash": self.new_hash}]}
        return client

    def test_the_note_is_written_and_no_warning_is_carried(self):
        client = self._client_for_worker()
        _entry, resolved = self._run_worker(client, self._settings(True))
        self.assertEqual(len(resolved), 1)
        _emitted_entry, emitted_hash, warning = resolved[0]
        self.assertEqual(emitted_hash, self.new_hash)
        self.assertIsNone(warning)
        client.set_note.assert_called_once()

    def test_entry_resolved_carries_the_note_warning(self):
        """The only place this path can say the note did not happen."""
        client = self._client_for_worker(missing_permission=(
            'your Hydrus access key does not have the "Edit File Notes" permission'))
        _entry, resolved = self._run_worker(client, self._settings(True))
        _emitted_entry, emitted_hash, warning = resolved[0]
        self.assertEqual(emitted_hash, self.new_hash)   # the import itself stands
        self.assertIn("Edit File Notes", warning)

    def test_nothing_is_written_when_the_setting_is_off(self):
        client = self._client_for_worker()
        _entry, resolved = self._run_worker(client, self._settings(False))
        self.assertIsNone(resolved[0][2])
        client.set_note.assert_not_called()


if __name__ == "__main__":
    unittest.main()
