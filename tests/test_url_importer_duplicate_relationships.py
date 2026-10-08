"""Duplicate relationships on the path where HYDRUS did the downloading
(DAN-71 - the half of DAN-39 that was never built).

DAN-39 wired record_duplicate_relationship into download_and_send(), the
one import path where Hatate fetched the file itself and therefore still
had the bytes to compare. Anyone on auto_import_method = "url_importer"
had set_hydrus_duplicate_relationships switched on and got no
relationships whatsoever, and nothing anywhere said so. These tests are
about that gap, in four layers that fail in different ways:

  * the READ-BACK - HydrusClient.file_id_for_hash, because the endpoint
    that hands over a file's bytes is addressed by file_id and the only
    thing this path has is a hash.
  * the CORE - relate_confirmed_url_import: off costs nothing, every
    DAN-39 guard still holds, and no failure fetching the file back may
    escape into the import that already succeeded.
  * the AUTO-IMPORT WIRING - that SearchWorker now polls for the
    confirmed hash when the relationship setting is on, and pairs against
    the hash the local copy had rather than the one the import produced.
  * the BATCH WIRING - that HydrusImportPollWorker records the
    relationship on its own thread and carries a warning out through
    entry_resolved, which previously had no way to report one.

RUN AGAINST BOTH PARENTS, which is what makes each layer's claim checkable
on its own:

  * 4c1acd9 (`origin/main` - none of this exists): 29 of the 34 fail, as 3
    failures and 26 errors.
  * c4a4735 (the core landed, nothing wired to it): 10 fail - the 3
    TestAutoImportWiring positives as failures and all 7
    TestBatchPollWorkerWiring cases as errors. Everything else passes,
    which is the point of running it there: it separates the wiring's
    claim from the core's.

The 5 that pass against BOTH parents pass deliberately, and would be
worthless if they did not:

  * TestFeatureOff's two default checks - the setting is off out of the
    box, and off for a config written before it existed. That has to hold
    on both sides of a change that writes into somebody's library, so
    passing on both IS the result.
  * TestAutoImportWiring's three negatives -
    test_nothing_is_confirmed_or_related_when_both_settings_are_off,
    test_removal_still_confirms_on_its_own_with_relationships_off and
    test_an_unconfirmed_import_relates_nothing. These guard against the
    change OVER-reaching: the poll's condition gained a second reason to
    fire and must not have lost its first one or started firing for no
    reason. The parent already behaves correctly in those three cases,
    which is exactly why they are pinned here.
"""
import hashlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from PIL import Image, ImageDraw

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    HAVE_QT = True
except ImportError:                                  # pragma: no cover - environment
    HAVE_QT = False


def _pattern(size=240):
    """Structured enough to hash meaningfully - a flat fill hashes to
    nothing and would make every distance here a 0."""
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for x in range(size):
        for y in range(size):
            pixels[x, y] = ((x * 3 + y) % 256, (x * x + y * 7) % 256, (x + y * 5) % 256)
    return image


def _png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


BASE = _pattern()
# The same images, and the same measured distances, as
# tests/test_hydrus_duplicate_relationships.py - deliberately, so the two
# paths are shown reaching the same verdicts from the same input rather
# than each being graded against its own fixtures.
LOCAL_PNG = _png(BASE)
SAME_PICTURE_BIGGER = _png(BASE.resize((480, 480), Image.LANCZOS))     # 64-bit 0, 256-bit 1
EDITED_VARIANT = _png(
    (lambda im: (ImageDraw.Draw(im).rectangle((0, 168, 240, 240), fill=(250, 250, 250)), im)[1])
    (BASE.copy()))                                                     # 64-bit 7, 256-bit 21
DIFFERENT_PICTURE = _png(_pattern(200).rotate(90).resize((240, 240)))

LOCAL_HASH = hashlib.sha256(LOCAL_PNG).hexdigest()
NEW_HASH = hashlib.sha256(SAME_PICTURE_BIGGER).hexdigest()
CONFIRMED_URL = "http://example.com/post/123"


class _Fixture(unittest.TestCase):
    """A local file on disk, plus a Hydrus that answers like a real one
    for the pieces this path leans on - including handing the imported
    file's bytes back when asked for them, which is the new step."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="hatate-url-dup-rel-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.local_path = os.path.join(self.tmp, "local.png")
        with open(self.local_path, "wb") as fh:
            fh.write(LOCAL_PNG)

        import core.hydrus_import as hi_module
        self.hi = hi_module

    def _entry(self):
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path=self.local_path)
        entry.hydrus_hash = LOCAL_HASH               # the copy Hydrus already holds
        entry.matched_url = CONFIRMED_URL
        entry.candidates = [MatchCandidate(url=CONFIRMED_URL, similarity=97.0,
                                           similarity_measured=True)]
        entry.select_candidate(0)
        entry.tags = []
        return entry

    def _client(self, imported=SAME_PICTURE_BIGGER, present=True, missing_permission=None,
                file_id=41):
        """Hydrus as this path actually uses it: a file id for the
        confirmed hash, the file's bytes streamed to a path, and the
        deletion-state/permission answers record_duplicate_relationship
        checks before writing anything."""
        client = MagicMock()
        client.file_id_for_hash.return_value = file_id

        def _download(fid, dest_path):
            with open(dest_path, "wb") as fh:
                fh.write(imported)
        client.download_file.side_effect = _download
        client.deletion_states.return_value = {
            LOCAL_HASH: "present" if present else "deleted"}
        client.missing_permission.return_value = missing_permission
        client.get_url_files.return_value = {
            "url_file_statuses": [{"status": 2, "hash": NEW_HASH}]}
        return client

    @staticmethod
    def _settings(enabled, remove_after_import=False):
        return SimpleNamespace(set_hydrus_duplicate_relationships=enabled,
                               remove_after_import=remove_after_import,
                               search_timeout=30.0)

    def _relate(self, client, settings, local_hash=LOCAL_HASH, new_hash=NEW_HASH):
        return self.hi.relate_confirmed_url_import(
            self._entry(), client, settings, local_hash, new_hash)


class TestFeatureOff(_Fixture):
    """The criterion that matters most on a change that writes into
    somebody's library: off means off, and off costs nothing."""

    def test_it_is_off_by_default(self):
        from core.config import Settings
        self.assertIs(Settings().set_hydrus_duplicate_relationships, False)

    def test_an_existing_config_that_never_heard_of_it_reads_as_off(self):
        """Every user upgrading into this has a config file written before
        the setting existed. Opting in has to be something they did, not
        something a missing key did for them."""
        from dataclasses import asdict
        from core.config import Settings
        stored = asdict(Settings())
        stored.pop("set_hydrus_duplicate_relationships")
        self.assertFalse(Settings(**stored).set_hydrus_duplicate_relationships)

    def test_the_file_is_not_even_fetched_back_when_off(self):
        """The setting is checked before the read-back, not after it. Off
        must not cost a file-id lookup, a download, a temp file or a
        comparison - so this asserts on all of them, not just on the
        absence of a relationship call."""
        client = self._client()
        with patch.object(self.hi, "duplicate_verdict") as verdict:
            warning = self._relate(client, self._settings(False))
        self.assertIsNone(warning)
        client.file_id_for_hash.assert_not_called()
        client.download_file.assert_not_called()
        verdict.assert_not_called()
        client.deletion_states.assert_not_called()
        client.missing_permission.assert_not_called()
        client.set_file_relationship.assert_not_called()
        client.set_kings.assert_not_called()

    def test_settings_of_none_is_treated_as_off(self):
        client = self._client()
        self.assertIsNone(self._relate(client, None))
        client.file_id_for_hash.assert_not_called()
        client.set_file_relationship.assert_not_called()


class TestFeatureOn(_Fixture):
    """What each verdict asks Hydrus to do, once the file has been read
    back out of it.

    Hydrus's enum values are imported per-test rather than at class level
    so this module still IMPORTS against a tree without the feature -
    which is what lets each behaviour be shown failing individually
    against the parent, rather than the whole file collapsing into one
    loader error that proves much less.
    """

    def test_the_confirmed_file_is_read_back_and_paired_as_the_better_copy(self):
        from core.hydrus_client import DUPLICATE_BETTER
        client = self._client(SAME_PICTURE_BIGGER)
        warning = self._relate(client, self._settings(True))
        self.assertIsNone(warning)
        # Read back by its own confirmed hash, via the file id - Hydrus's
        # bytes endpoint takes no hash.
        client.file_id_for_hash.assert_called_once_with(NEW_HASH)
        self.assertEqual(client.download_file.call_args[0][0], 41)
        client.set_file_relationship.assert_called_once_with(
            NEW_HASH, LOCAL_HASH, DUPLICATE_BETTER)
        client.set_kings.assert_called_once_with([NEW_HASH])

    def test_a_different_edit_is_filed_as_an_alternate_and_crowns_nobody(self):
        from core.hydrus_client import DUPLICATE_ALTERNATE
        edited_hash = hashlib.sha256(EDITED_VARIANT).hexdigest()
        client = self._client(EDITED_VARIANT)
        warning = self._relate(client, self._settings(True), new_hash=edited_hash)
        self.assertIsNone(warning)
        client.set_file_relationship.assert_called_once_with(
            edited_hash, LOCAL_HASH, DUPLICATE_ALTERNATE)
        client.set_kings.assert_not_called()

    def test_an_unconfident_verdict_writes_nothing(self):
        """Same rule as the download path's: anything that is not one of
        the two confident answers touches nothing."""
        client = self._client(DIFFERENT_PICTURE)
        warning = self._relate(client, self._settings(True),
                               new_hash=hashlib.sha256(DIFFERENT_PICTURE).hexdigest())
        self.assertIsNone(warning)
        client.set_file_relationship.assert_not_called()
        client.set_kings.assert_not_called()

    def test_the_deletion_state_check_still_guards_the_write(self):
        """Hydrus answers 200 for a pairing against a hash it is not
        holding, so an unchecked call would look like it worked and leave
        the user nothing to find or undo."""
        client = self._client(present=False)
        warning = self._relate(client, self._settings(True))
        self.assertIsNone(warning)
        client.deletion_states.assert_called_once_with([LOCAL_HASH])
        client.set_file_relationship.assert_not_called()

    def test_nothing_is_written_when_there_is_no_local_hash(self):
        client = self._client()
        self.assertIsNone(self._relate(client, self._settings(True), local_hash=None))
        client.file_id_for_hash.assert_not_called()
        client.set_file_relationship.assert_not_called()

    def test_nothing_is_written_when_hydrus_landed_on_the_file_the_user_had(self):
        """Hydrus's own downloader can perfectly well resolve the URL to
        the exact file already in the library. One file cannot be a
        duplicate of itself, and the read-back is not worth paying for."""
        client = self._client()
        self.assertIsNone(self._relate(client, self._settings(True),
                                       new_hash=LOCAL_HASH.upper()))
        client.file_id_for_hash.assert_not_called()
        client.set_file_relationship.assert_not_called()

    def test_a_missing_permission_is_named_and_the_import_still_stands(self):
        client = self._client(missing_permission="the access key lacks 'Edit File Relationships'")
        warning = self._relate(client, self._settings(True))
        self.assertIn("Edit File Relationships", warning)
        self.assertTrue(warning.startswith("Imported, but not marked as a duplicate"))
        client.set_file_relationship.assert_not_called()


class TestReadBackFailures(_Fixture):
    """Fetching the file back is a new way for this to go wrong, and it
    happens AFTER Hydrus has already imported the file. None of it may
    reach the import."""

    def test_a_hash_hydrus_has_no_id_for_is_reported_not_raised(self):
        client = self._client(file_id=None)
        warning = self._relate(client, self._settings(True))
        self.assertIn("could not read the imported file back", warning)
        client.download_file.assert_not_called()
        client.set_file_relationship.assert_not_called()

    def test_a_failed_download_is_reported_not_raised(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        client.download_file.side_effect = HydrusError("Hydrus returned HTTP 500 for file 41")
        warning = self._relate(client, self._settings(True))
        self.assertIn("could not read the imported file back", warning)
        client.set_file_relationship.assert_not_called()

    def test_an_unwritable_temp_file_is_reported_not_raised(self):
        client = self._client()
        client.download_file.side_effect = OSError("No space left on device")
        warning = self._relate(client, self._settings(True))
        self.assertIn("could not read the imported file back", warning)

    def test_the_temp_file_is_cleaned_up_on_both_outcomes(self):
        """These are full-resolution originals, and on most Linux systems
        /tmp is RAM-backed - so a leak here costs memory until reboot."""
        import glob
        import tempfile
        pattern = os.path.join(tempfile.gettempdir(), "hatate-hydrus-relate-*")
        before = set(glob.glob(pattern))

        self._relate(self._client(), self._settings(True))
        failing = self._client()
        failing.download_file.side_effect = OSError("No space left on device")
        self._relate(failing, self._settings(True))

        self.assertEqual(set(glob.glob(pattern)) - before, set())


class TestFileIdLookup(unittest.TestCase):
    """HydrusClient.file_id_for_hash - the one client addition. Its whole
    job is turning a hash into the id /get_files/file wants."""

    def _client(self, response):
        from core.hydrus_client import HydrusClient, HydrusSettings
        client = HydrusClient(HydrusSettings(api_url="http://h", access_key="k"))
        client._get = MagicMock(return_value=response)
        return client

    def test_the_request_asks_for_identifiers_only(self):
        """The caller wants an id, not tags - and this runs against a file
        that may be very large in a library that may be very big."""
        import json
        client = self._client({"metadata": [{"hash": NEW_HASH, "file_id": 7}]})
        self.assertEqual(client.file_id_for_hash(NEW_HASH), 7)
        endpoint, params = client._get.call_args[0]
        self.assertEqual(endpoint, "/get_files/file_metadata")
        self.assertEqual(json.loads(params["hashes"]), [NEW_HASH])
        self.assertEqual(params["only_return_identifiers"], "true")

    def test_a_null_file_id_is_not_an_id(self):
        """A hash Hydrus has never seen still comes back as a row, with a
        null file_id - so the row's presence is not the answer."""
        client = self._client({"metadata": [{"hash": NEW_HASH, "file_id": None}]})
        self.assertIsNone(client.file_id_for_hash(NEW_HASH))

    def test_a_row_for_some_other_hash_is_not_used(self):
        client = self._client({"metadata": [{"hash": LOCAL_HASH, "file_id": 7}]})
        self.assertIsNone(client.file_id_for_hash(NEW_HASH))

    def test_the_hash_is_matched_case_insensitively(self):
        client = self._client({"metadata": [{"hash": NEW_HASH, "file_id": 7}]})
        self.assertEqual(client.file_id_for_hash(NEW_HASH.upper()), 7)

    def test_an_empty_response_and_an_empty_hash_are_both_none(self):
        self.assertIsNone(self._client({}).file_id_for_hash(NEW_HASH))
        self.assertIsNone(self._client({"metadata": []}).file_id_for_hash(NEW_HASH))
        self.assertIsNone(self._client({}).file_id_for_hash(""))

    def test_a_hydrus_failure_is_not_raised_at_the_caller(self):
        """The caller is bookkeeping that follows a successful import and
        must not raise - so the failure is absorbed here, as the other
        read helpers on this client do."""
        from core.hydrus_client import HydrusError
        client = self._client({})
        client._get.side_effect = HydrusError("Could not reach Hydrus")
        self.assertIsNone(client.file_id_for_hash(NEW_HASH))


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestAutoImportWiring(_Fixture):
    """SearchWorker's url_importer path - the one the ticket names. This is
    the regression: on the parent the confirmation poll only ran when
    remove_after_import was on, so a user who keeps imported rows in the
    list got no confirmed hash and therefore no relationship, silently."""

    def _worker(self, settings):
        from workers.search_worker import SearchWorker
        return SearchWorker([], settings)

    def _settings(self, enabled, remove_after_import=False):
        from core.config import Settings
        settings = Settings()
        settings.auto_import_method = "url_importer"
        settings.auto_import_min_similarity = 80.0
        settings.auto_import_enabled = True
        settings.remove_after_import = remove_after_import
        settings.set_hydrus_duplicate_relationships = enabled
        return settings

    def _auto_import(self, settings, client, entry=None):
        from core.models import MatchStatus
        entry = entry or self._entry()
        entry.status = MatchStatus.GOOD
        entry.similarity = 97.0
        worker = self._worker(settings)
        results = []
        worker.auto_imported.connect(lambda e, r: results.append(r),
                                     Qt.ConnectionType.DirectConnection)
        with patch("core.hydrus_import.url_import_refusal", return_value=None), \
             patch("core.hydrus_import._forwarded_url", return_value=None):
            worker._maybe_auto_import(entry, client, self.tmp)
        return entry, results[0] if results else None

    def test_the_relationship_setting_alone_is_enough_to_confirm_and_pair(self):
        """remove_after_import OFF, relationships ON. On the parent no poll
        happened at all here, so there was no hash to pair against."""
        from core.hydrus_client import DUPLICATE_BETTER
        client = self._client()
        entry, result = self._auto_import(self._settings(True), client)
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed)
        client.set_file_relationship.assert_called_once_with(
            NEW_HASH, LOCAL_HASH, DUPLICATE_BETTER)
        client.set_kings.assert_called_once_with([NEW_HASH])

    def test_the_pairing_uses_the_hash_the_local_copy_had(self):
        """entry.hydrus_hash is overwritten with the confirmed hash on this
        same path. Read it after, and the file gets paired with itself."""
        client = self._client()
        entry, _ = self._auto_import(self._settings(True), client)
        self.assertEqual(entry.hydrus_hash, NEW_HASH)
        hash_a, hash_b, _rel = client.set_file_relationship.call_args[0]
        self.assertEqual(hash_b, LOCAL_HASH)
        self.assertNotEqual(hash_a, hash_b)

    def test_nothing_is_confirmed_or_related_when_both_settings_are_off(self):
        """The poll is several extra API calls and up to a minute of
        waiting. Neither setting on means neither is paid for."""
        client = self._client()
        with patch("workers.search_worker.poll_single_url_import") as poll:
            _entry, result = self._auto_import(self._settings(False), client)
        poll.assert_not_called()
        self.assertIsNone(result.confirmed)
        client.set_file_relationship.assert_not_called()

    def test_removal_still_confirms_on_its_own_with_relationships_off(self):
        """The condition gained a second reason to poll; it did not lose
        the first one."""
        client = self._client()
        _entry, result = self._auto_import(
            self._settings(False, remove_after_import=True), client)
        self.assertTrue(result.confirmed)
        client.set_file_relationship.assert_not_called()

    def test_a_read_back_failure_becomes_a_warning_not_a_failed_import(self):
        client = self._client(file_id=None)
        entry, result = self._auto_import(self._settings(True), client)
        self.assertTrue(result.success)
        self.assertTrue(result.confirmed)
        self.assertIn("not marked as a duplicate", result.warning)
        self.assertEqual(entry.error_message, result.warning)

    def test_an_unconfirmed_import_relates_nothing(self):
        """Nothing was imported that anything could be a duplicate of."""
        client = self._client()
        client.get_url_files.return_value = {"url_file_statuses": []}
        settings = self._settings(True)
        settings.url_import_confirm_timeout = 0.0
        settings.url_import_confirm_interval = 0.0
        _entry, result = self._auto_import(settings, client)
        self.assertFalse(result.confirmed)
        client.file_id_for_hash.assert_not_called()
        client.set_file_relationship.assert_not_called()


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestBatchPollWorkerWiring(_Fixture):
    """HydrusImportPollWorker - the manual "Send URL to Hydrus's Importer"
    batch. Its entry_resolved signal had no equivalent of
    ImportResult.warning, so a relationship that could not be recorded had
    nowhere to be reported; that surface is part of this change."""

    def _run_worker(self, client, settings, entry=None):
        from workers.hydrus_import_poll_worker import HydrusImportPollWorker
        entry = entry or self._entry()
        worker = HydrusImportPollWorker(client, [(entry, CONFIRMED_URL)],
                                        timeout=0.0, interval=0.0, settings=settings)
        resolved = []
        worker.entry_resolved.connect(lambda *args: resolved.append(args),
                                      Qt.ConnectionType.DirectConnection)
        worker.run()                     # run() directly: no thread needed to test it
        return entry, resolved

    def test_the_relationship_is_recorded_and_no_warning_is_carried(self):
        from core.hydrus_client import DUPLICATE_BETTER
        client = self._client()
        _entry, resolved = self._run_worker(client, self._settings(True))
        self.assertEqual(len(resolved), 1)
        emitted_entry, emitted_hash, warning = resolved[0]
        self.assertEqual(emitted_hash, NEW_HASH)
        self.assertIsNone(warning)
        client.set_file_relationship.assert_called_once_with(
            NEW_HASH, LOCAL_HASH, DUPLICATE_BETTER)

    def test_entry_resolved_carries_the_warning(self):
        """The whole point of the third argument: this is the only place
        the batch path can say the relationship did not happen."""
        client = self._client(file_id=None)
        _entry, resolved = self._run_worker(client, self._settings(True))
        _emitted_entry, emitted_hash, warning = resolved[0]
        self.assertEqual(emitted_hash, NEW_HASH)     # the import itself stands
        self.assertIn("not marked as a duplicate", warning)

    def test_the_relationship_is_paired_before_the_hash_is_emitted(self):
        """The GUI handler is what overwrites entry.hydrus_hash with the
        confirmed hash, so the worker must have paired before it emits -
        otherwise the local hash is gone by then."""
        client = self._client()
        order = []
        client.set_file_relationship.side_effect = lambda *a: order.append("related")
        entry = self._entry()

        from workers.hydrus_import_poll_worker import HydrusImportPollWorker
        worker = HydrusImportPollWorker(client, [(entry, CONFIRMED_URL)], timeout=0.0,
                                        interval=0.0, settings=self._settings(True))
        worker.entry_resolved.connect(lambda *a: order.append("emitted"),
                                      Qt.ConnectionType.DirectConnection)
        worker.run()
        self.assertEqual(order, ["related", "emitted"])

    def test_an_unconfirmed_entry_emits_three_arguments_too(self):
        """Every emit has to match the signal, or the GUI slot breaks on
        the timeout path rather than the success one."""
        client = self._client()
        client.get_url_files.return_value = {"url_file_statuses": []}
        _entry, resolved = self._run_worker(client, self._settings(True))
        self.assertEqual(resolved, [(_entry, None, None)])

    def test_nothing_is_related_when_the_setting_is_off(self):
        client = self._client()
        _entry, resolved = self._run_worker(client, self._settings(False))
        self.assertEqual(resolved[0][2], None)
        client.file_id_for_hash.assert_not_called()
        client.set_file_relationship.assert_not_called()

    def test_no_settings_at_all_reads_as_off(self):
        """The argument is optional: a caller that does not pass it gets
        the behaviour this worker had before any of this existed."""
        client = self._client()
        _entry, resolved = self._run_worker(client, None)
        self.assertEqual(resolved[0][1], NEW_HASH)
        client.set_file_relationship.assert_not_called()

    def test_an_unexpected_failure_does_not_take_the_confirmation_down(self):
        """A confirmed import means the row can be removed from the list.
        Losing that over bookkeeping that follows it would be worse than
        the missing relationship - so even an error the core does not
        model is absorbed here."""
        client = self._client()
        client.file_id_for_hash.side_effect = RuntimeError("something nobody predicted")
        _entry, resolved = self._run_worker(client, self._settings(True))
        self.assertEqual(resolved[0][1], NEW_HASH)


if __name__ == "__main__":
    unittest.main()
