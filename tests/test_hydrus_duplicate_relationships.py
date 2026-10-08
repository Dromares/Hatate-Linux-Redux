"""Telling Hydrus how a downloaded better copy relates to the file it
already had (DAN-39).

Three separate things are under test and they fail in different ways, so
they are kept apart here:

  * the DECISION - duplicate_verdict(), which turns two hash distances
    into "same picture", "different edit" or "say nothing". Driven both
    from real images and, for the cases real images cannot be made to
    land on reliably, from a synthesised Comparison.
  * the WIRING - which Hydrus call each verdict produces, and that a
    missing permission or an old client degrades into a named message
    without taking the import down with it.
  * the REQUEST - that what goes on the wire is the shape Hydrus's Client
    API documents, with the enum values it documents.

RUN AGAINST PARENT 0e0c21b: 40 of these 45 error out, which is the point
of them. The five that pass there pass deliberately and would be broken
if they did not:

  * the four TestDistanceFixtures checks assert properties of
    core/image_compare.py, which this change does not touch. They exist
    to keep the image fixtures honest, not to test new behaviour.
  * TestFeatureOff.test_a_whole_import_is_unchanged is the byte-identical
    -when-disabled regression test. Passing on BOTH sides of the commit
    is exactly what it is for.
"""
import hashlib
import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

from PIL import Image, ImageDraw


def _pattern(size=240):
    """A picture with enough structure in it to hash meaningfully. A flat
    fill hashes to nothing and would make every distance here a 0."""
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

# MEASURED against core/image_compare.py, with the distances each one
# actually produces - see the DistanceFixtures test below, which is what
# keeps these honest if the hashing ever changes.
#
# A bigger copy of the same picture: the whole point of the feature.
SAME_PICTURE_BIGGER = _png(BASE.resize((480, 480), Image.LANCZOS))     # 64-bit 0, 256-bit 1
# A different edit: a third of the picture papered over.
EDITED_VARIANT = _png(
    (lambda im: (ImageDraw.Draw(im).rectangle((0, 168, 240, 240), fill=(250, 250, 250)), im)[1])
    (BASE.copy()))                                                     # 64-bit 7, 256-bit 21
# The same artwork flipped - deliberately NOT given a relationship.
MIRRORED = _png(BASE.transpose(Image.FLIP_LEFT_RIGHT))
# Nothing to do with it.
DIFFERENT_PICTURE = _png(_pattern(200).rotate(90).resize((240, 240)))

LOCAL_PNG = _png(BASE)


class _Fixture(unittest.TestCase):
    """A local file on disk plus a Hydrus client that answers like a real
    one for the pieces this feature leans on."""

    def setUp(self):
        self.qt_patcher = patch.dict("sys.modules", {
            "PyQt6": MagicMock(), "PyQt6.QtCore": MagicMock(),
        })
        self.qt_patcher.start()

        self.tmp = tempfile.mkdtemp(prefix="hatate-dup-rel-")
        self.local_path = os.path.join(self.tmp, "local.png")
        with open(self.local_path, "wb") as fh:
            fh.write(LOCAL_PNG)
        self.local_hash = hashlib.sha256(LOCAL_PNG).hexdigest()

        import core.hydrus_import as hi_module
        self.hi = hi_module

    def tearDown(self):
        self.qt_patcher.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entry(self):
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path=self.local_path)
        entry.hydrus_hash = self.local_hash          # the copy Hydrus already holds
        entry.matched_url = "http://example.com/post/123"
        entry.candidates = [MatchCandidate(url="http://example.com/post/123",
                                           similarity=97.0, similarity_measured=True,
                                           direct_file_url="http://example.com/big.png")]
        entry.select_candidate(0)
        entry.tags = []
        return entry

    def _client(self, present=True, missing_permission=None):
        client = MagicMock()
        # Reports the real hash of the file it was handed, as a real
        # (non-transcoding) Hydrus does for a direct upload - anything
        # else trips download_and_send's DAN-17 hash-mismatch guard.
        def _import(path):
            with open(path, "rb") as fh:
                return {"hash": hashlib.sha256(fh.read()).hexdigest(), "status": 1}
        client.import_file.side_effect = _import
        client.deletion_states.return_value = {
            self.local_hash: "present" if present else "deleted"}
        client.missing_permission.return_value = missing_permission
        return client

    @staticmethod
    def _settings(enabled):
        from types import SimpleNamespace
        return SimpleNamespace(set_hydrus_duplicate_relationships=enabled,
                               search_timeout=30.0)


class TestDistanceFixtures(_Fixture):
    """The images above claim particular hash distances. If they ever stop
    producing them, every verdict test below is testing something other
    than what it says it is - so the claim is checked rather than trusted.
    """

    def _compare(self, data):
        from core.image_compare import compare_to_local, local_prints
        return compare_to_local(local_prints(self.local_path), data)

    def test_the_bigger_copy_is_a_confirmed_same_picture(self):
        from core.image_compare import DHASH_SAME_MAX, FINE_CONFIRM_MAX
        comparison = self._compare(SAME_PICTURE_BIGGER)
        self.assertLessEqual(comparison.distance, DHASH_SAME_MAX)
        self.assertLessEqual(comparison.fine_distance, FINE_CONFIRM_MAX)
        self.assertFalse(comparison.mirrored)

    def test_the_edited_variant_lands_in_the_edit_or_crop_band(self):
        from core.image_compare import DHASH_SAME_MAX, DHASH_SIMILAR_MAX
        comparison = self._compare(EDITED_VARIANT)
        self.assertGreater(comparison.distance, DHASH_SAME_MAX)
        self.assertLessEqual(comparison.distance, DHASH_SIMILAR_MAX)

    def test_the_mirrored_copy_is_recognised_as_mirrored(self):
        self.assertTrue(self._compare(MIRRORED).mirrored)

    def test_the_different_picture_is_past_the_band(self):
        from core.image_compare import DHASH_SIMILAR_MAX
        self.assertGreater(self._compare(DIFFERENT_PICTURE).distance, DHASH_SIMILAR_MAX)


class TestVerdict(_Fixture):
    """duplicate_verdict() on real images."""

    def test_a_bigger_copy_of_the_same_picture_is_the_same_picture(self):
        self.assertEqual(self.hi.duplicate_verdict(self.local_path, SAME_PICTURE_BIGGER),
                         self.hi.VERDICT_SAME_IMAGE)

    def test_a_different_edit_is_an_alternate(self):
        self.assertEqual(self.hi.duplicate_verdict(self.local_path, EDITED_VARIANT),
                         self.hi.VERDICT_ALTERNATE)

    def test_a_mirrored_copy_says_nothing(self):
        """The same artwork flipped is a real observation and not one
        Hydrus has a relationship for. It is not the same file, and
        'alternates' would be claiming an edit that nobody made."""
        self.assertIsNone(self.hi.duplicate_verdict(self.local_path, MIRRORED))

    def test_a_different_picture_says_nothing(self):
        self.assertIsNone(self.hi.duplicate_verdict(self.local_path, DIFFERENT_PICTURE))

    def test_unreadable_bytes_say_nothing(self):
        self.assertIsNone(self.hi.duplicate_verdict(self.local_path, b"not an image at all"))

    def test_an_unreadable_local_file_says_nothing(self):
        self.assertIsNone(self.hi.duplicate_verdict(os.path.join(self.tmp, "nope.png"),
                                                    SAME_PICTURE_BIGGER))


class TestVerdictDecisionTable(_Fixture):
    """The cases real images cannot be aimed at reliably, driven from a
    synthesised Comparison so each branch is exercised exactly."""

    def _verdict(self, distance, mirrored=False, fine_distance=0):
        from core.image_compare import Comparison
        with patch("core.image_compare.local_prints", return_value=object()), \
             patch("core.image_compare.compare_to_local",
                   return_value=Comparison(distance, mirrored, fine_distance)):
            return self.hi.duplicate_verdict(self.local_path, b"ignored")

    def test_no_distance_at_all_says_nothing(self):
        self.assertIsNone(self._verdict(None))

    def test_a_same_picture_the_fine_hash_contradicts_says_nothing(self):
        """The case core/image_compare.py measured directly: of 12 matches
        a 64-bit hash called identical and 256 bits did not, six were the
        same picture and six were variants or different. Evidence that
        disagrees with itself is not a verdict."""
        from core.image_compare import DHASH_SAME_MAX, FINE_CONFIRM_MAX
        self.assertIsNone(self._verdict(DHASH_SAME_MAX, fine_distance=FINE_CONFIRM_MAX + 1))

    def test_a_missing_fine_distance_says_nothing(self):
        self.assertIsNone(self._verdict(0, fine_distance=None))

    def test_the_boundaries_are_inclusive(self):
        from core.image_compare import DHASH_SAME_MAX, DHASH_SIMILAR_MAX, FINE_CONFIRM_MAX
        self.assertEqual(self._verdict(DHASH_SAME_MAX, fine_distance=FINE_CONFIRM_MAX),
                         self.hi.VERDICT_SAME_IMAGE)
        self.assertEqual(self._verdict(DHASH_SIMILAR_MAX, fine_distance=60),
                         self.hi.VERDICT_ALTERNATE)
        self.assertIsNone(self._verdict(DHASH_SIMILAR_MAX + 1, fine_distance=60))

    def test_mirrored_says_nothing_however_close(self):
        self.assertIsNone(self._verdict(0, mirrored=True, fine_distance=0))


class TestFeatureOff(_Fixture):
    """The acceptance criterion that matters most: with the setting off,
    an import does exactly what it did before this existed."""

    def test_it_is_off_by_default(self):
        from core.config import Settings
        self.assertIs(Settings().set_hydrus_duplicate_relationships, False)

    def test_nothing_is_asked_of_hydrus_and_nothing_is_measured(self):
        """Not just 'no relationship call' - no permission probe, no
        deletion_states, and no comparison either. The flag is checked on
        the first line precisely so that off costs nothing."""
        client = self._client()
        with patch.object(self.hi, "duplicate_verdict") as verdict:
            warning = self.hi.record_duplicate_relationship(
                self._entry(), client, self._settings(False),
                self.local_hash, SAME_PICTURE_BIGGER, "ff" * 32)
        self.assertIsNone(warning)
        verdict.assert_not_called()
        client.set_file_relationship.assert_not_called()
        client.set_kings.assert_not_called()
        client.deletion_states.assert_not_called()
        client.missing_permission.assert_not_called()

    def test_settings_of_none_is_treated_as_off(self):
        """download_and_send's `settings` is optional and several callers
        leave it out. That must read as off, not crash."""
        client = self._client()
        self.assertIsNone(self.hi.record_duplicate_relationship(
            self._entry(), client, None, self.local_hash, SAME_PICTURE_BIGGER, "ff" * 32))
        client.set_file_relationship.assert_not_called()

    @patch("core.remote.download_bytes")
    def test_a_whole_import_is_unchanged(self, download):
        """End to end through download_and_send: same result, same entry
        state, and not one relationship call."""
        download.return_value = SAME_PICTURE_BIGGER
        entry, client = self._entry(), self._client()
        result = self.hi.download_and_send(entry, client, 30.0, self.tmp, self._settings(False))
        self.assertTrue(result.success)
        self.assertIsNone(result.warning)
        self.assertIsNone(entry.error_message)
        self.assertTrue(entry.sent_to_hydrus)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertEqual(entry.hydrus_hash, hashlib.sha256(SAME_PICTURE_BIGGER).hexdigest())
        client.set_file_relationship.assert_not_called()
        client.set_kings.assert_not_called()


class TestFeatureOn(_Fixture):
    """What each verdict actually asks Hydrus to do.

    Hydrus's enum values are imported per-test rather than at class level
    so that this module still IMPORTS against a tree without the feature -
    which is what lets each behaviour below be shown failing individually
    against the parent commit, instead of the whole file collapsing into
    one loader error that proves much less.
    """

    def _run(self, downloaded, client=None):
        client = client or self._client()
        warning = self.hi.record_duplicate_relationship(
            self._entry(), client, self._settings(True), self.local_hash, downloaded,
            hashlib.sha256(downloaded).hexdigest())
        return client, warning

    def test_same_picture_sets_the_new_file_better_and_crowns_it(self):
        from core.hydrus_client import DUPLICATE_BETTER
        new_hash = hashlib.sha256(SAME_PICTURE_BIGGER).hexdigest()
        client, warning = self._run(SAME_PICTURE_BIGGER)
        self.assertIsNone(warning)
        client.set_file_relationship.assert_called_once_with(
            new_hash, self.local_hash, DUPLICATE_BETTER)
        # The better file is the king. 'set A as better' alone does not
        # guarantee that - see HydrusClient.set_kings.
        client.set_kings.assert_called_once_with([new_hash])

    def test_a_different_edit_is_filed_as_an_alternate_and_crowns_nobody(self):
        from core.hydrus_client import DUPLICATE_ALTERNATE
        new_hash = hashlib.sha256(EDITED_VARIANT).hexdigest()
        client, warning = self._run(EDITED_VARIANT)
        self.assertIsNone(warning)
        client.set_file_relationship.assert_called_once_with(
            new_hash, self.local_hash, DUPLICATE_ALTERNATE)
        client.set_kings.assert_not_called()

    def test_an_uncertain_verdict_writes_nothing(self):
        client, warning = self._run(DIFFERENT_PICTURE)
        self.assertIsNone(warning)
        client.set_file_relationship.assert_not_called()
        client.set_kings.assert_not_called()

    def test_a_mirrored_copy_writes_nothing(self):
        client, _ = self._run(MIRRORED)
        client.set_file_relationship.assert_not_called()

    def test_nothing_is_written_when_hydrus_is_not_holding_the_local_copy(self):
        """Hydrus answers 200 for a pairing against a hash it has never
        seen, so an unchecked call would look like it worked and leave the
        user nothing to find or undo."""
        client, warning = self._run(SAME_PICTURE_BIGGER, self._client(present=False))
        self.assertIsNone(warning)
        client.set_file_relationship.assert_not_called()

    def test_nothing_is_written_when_there_is_no_local_hash(self):
        client = self._client()
        self.assertIsNone(self.hi.record_duplicate_relationship(
            self._entry(), client, self._settings(True), None, SAME_PICTURE_BIGGER, "ff" * 32))
        client.set_file_relationship.assert_not_called()

    def test_nothing_is_written_when_hydrus_already_had_this_exact_file(self):
        """import_file is idempotent and reports the existing hash. One
        file cannot be a duplicate of itself."""
        client = self._client()
        self.assertIsNone(self.hi.record_duplicate_relationship(
            self._entry(), client, self._settings(True), self.local_hash,
            SAME_PICTURE_BIGGER, self.local_hash.upper()))
        client.set_file_relationship.assert_not_called()

    @patch("core.remote.download_bytes")
    def test_end_to_end_through_download_and_send(self, download):
        from core.hydrus_client import DUPLICATE_BETTER
        download.return_value = SAME_PICTURE_BIGGER
        entry, client = self._entry(), self._client()
        result = self.hi.download_and_send(entry, client, 30.0, self.tmp, self._settings(True))
        self.assertTrue(result.success)
        self.assertIsNone(result.warning)
        new_hash = hashlib.sha256(SAME_PICTURE_BIGGER).hexdigest()
        # Paired against the LOCAL file's hash, not the one the import
        # just wrote over entry.hydrus_hash with.
        client.set_file_relationship.assert_called_once_with(
            new_hash, self.local_hash, DUPLICATE_BETTER)
        self.assertEqual(entry.hydrus_hash, new_hash)


class TestDegradesGracefully(_Fixture):
    """A Hydrus that cannot do this must cost the user a sentence, not
    the import."""

    @patch("core.remote.download_bytes")
    def _import_with(self, client, download):
        download.return_value = SAME_PICTURE_BIGGER
        entry = self._entry()
        return entry, self.hi.download_and_send(entry, client, 30.0, self.tmp,
                                                self._settings(True))

    def test_a_key_without_the_permission_names_it_and_says_where(self):
        client = self._client(missing_permission=(
            'your Hydrus access key does not have the "Edit File Relationships" permission. '
            "Add it in Hydrus: services → review services → local → client api, "
            "then edit your access key and tick it"))
        entry, result = self._import_with(client)

        self.assertTrue(result.success, "the import itself must still succeed")
        self.assertTrue(entry.sent_to_hydrus)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("Edit File Relationships", result.warning)
        self.assertIn("review services", result.warning)
        self.assertNotIn("403", result.warning)
        client.set_file_relationship.assert_not_called()

    def test_an_old_hydrus_says_so_rather_than_reporting_a_404(self):
        from core.hydrus_client import HydrusClient, HydrusError
        client = self._client()
        # The probe cannot tell: an old client still answers
        # /verify_access_key, it just has no such endpoint to call.
        client.set_file_relationship.side_effect = (
            HydrusClient._relationship_failure(HydrusError("Hydrus returned HTTP 404: ...", 404)))
        entry, result = self._import_with(client)

        self.assertTrue(result.success)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("too old", result.warning)
        self.assertIn("/manage_file_relationships", result.warning)

    def test_any_other_hydrus_failure_still_leaves_the_import_standing(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        client.set_file_relationship.side_effect = HydrusError("the wheels came off", 500)
        entry, result = self._import_with(client)

        self.assertTrue(result.success)
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertIn("the wheels came off", result.warning)

    def test_a_failure_crowning_the_new_file_is_reported_not_raised(self):
        from core.hydrus_client import HydrusError
        client = self._client()
        client.set_kings.side_effect = HydrusError("no kings today", 500)
        entry, result = self._import_with(client)

        self.assertTrue(result.success)
        self.assertIn("no kings today", result.warning)


class TestClientWrappers(unittest.TestCase):
    """What actually goes on the wire, against Hydrus's documented shape."""

    def _client(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        return HydrusClient(HydrusSettings(access_key="k"))

    def test_the_relationship_request_is_the_documented_shape(self):
        from core.hydrus_client import DUPLICATE_BETTER
        client = self._client()
        with patch.object(client, "_post", return_value={}) as post:
            client.set_file_relationship("aa" * 32, "bb" * 32, DUPLICATE_BETTER)
        path, body = post.call_args[0]
        self.assertEqual(path, "/manage_file_relationships/set_file_relationships")
        self.assertEqual(body, {"relationships": [{
            "hash_a": "aa" * 32, "hash_b": "bb" * 32,
            "relationship": 4, "do_default_content_merge": True,
        }]})

    def test_deletion_is_never_requested_alongside_a_relationship(self):
        """Hydrus offers delete_a/delete_b on this same call. Being told
        which of two files is better is not consent to delete the other."""
        from core.hydrus_client import DUPLICATE_BETTER
        client = self._client()
        with patch.object(client, "_post", return_value={}) as post:
            client.set_file_relationship("aa" * 32, "bb" * 32, DUPLICATE_BETTER)
        pair = post.call_args[0][1]["relationships"][0]
        self.assertNotIn("delete_a", pair)
        self.assertNotIn("delete_b", pair)

    def test_the_enum_values_are_hydrus_own(self):
        from core.hydrus_client import DUPLICATE_ALTERNATE, DUPLICATE_BETTER
        self.assertEqual(DUPLICATE_BETTER, 4)      # "set A as better"
        self.assertEqual(DUPLICATE_ALTERNATE, 3)   # "set as alternates"

    def test_a_file_cannot_be_paired_with_itself(self):
        from core.hydrus_client import DUPLICATE_BETTER, HydrusError
        client = self._client()
        with patch.object(client, "_post") as post:
            with self.assertRaises(HydrusError):
                client.set_file_relationship("aa" * 32, "AA" * 32, DUPLICATE_BETTER)
        post.assert_not_called()

    def test_a_missing_hash_is_refused_before_the_request(self):
        from core.hydrus_client import DUPLICATE_BETTER, HydrusError
        client = self._client()
        with patch.object(client, "_post") as post:
            with self.assertRaises(HydrusError):
                client.set_file_relationship("aa" * 32, "", DUPLICATE_BETTER)
        post.assert_not_called()

    def test_set_kings_sends_hashes_and_no_ops_on_an_empty_list(self):
        client = self._client()
        with patch.object(client, "_post", return_value={}) as post:
            client.set_kings(["aa" * 32])
        self.assertEqual(post.call_args[0],
                         ("/manage_file_relationships/set_kings", {"hashes": ["aa" * 32]}))
        with patch.object(client, "_post") as post:
            client.set_kings([])
        post.assert_not_called()


class TestPermissionProbe(unittest.TestCase):
    def _client(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        return HydrusClient(HydrusSettings(access_key="k"))

    def test_the_permission_id_and_name_are_hydrus_own(self):
        from core.hydrus_client import HYDRUS_PERMISSIONS, PERMISSION_EDIT_FILE_RELATIONSHIPS
        self.assertEqual(PERMISSION_EDIT_FILE_RELATIONSHIPS, 8)
        self.assertEqual(HYDRUS_PERMISSIONS[8], "Edit File Relationships")

    def test_a_key_that_has_it_reports_nothing_missing(self):
        from core.hydrus_client import PERMISSION_EDIT_FILE_RELATIONSHIPS
        client = self._client()
        with patch.object(client, "_get", return_value={"basic_permissions": [0, 1, 3, 8]}):
            self.assertIsNone(client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS))

    def test_a_key_that_lacks_it_is_told_which_one_and_where(self):
        from core.hydrus_client import PERMISSION_EDIT_FILE_RELATIONSHIPS
        client = self._client()
        with patch.object(client, "_get", return_value={"basic_permissions": [0, 1, 3]}):
            message = client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS)
        self.assertIn("Edit File Relationships", message)
        self.assertIn("review services", message)

    def test_a_catch_all_key_covers_it(self):
        from core.hydrus_client import PERMISSION_EDIT_FILE_RELATIONSHIPS
        client = self._client()
        with patch.object(client, "_get",
                          return_value={"permits_everything": True, "basic_permissions": []}):
            self.assertIsNone(client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS))

    def test_inconclusive_is_not_treated_as_missing(self):
        """An unreachable Hydrus, or one that doesn't report permissions,
        must not silently disable a feature the user turned on - the call
        itself still fails with a named message if it really is missing.
        Same rule url_import_refusal() follows."""
        from core.hydrus_client import PERMISSION_EDIT_FILE_RELATIONSHIPS, HydrusError
        client = self._client()
        with patch.object(client, "_get", side_effect=HydrusError("unreachable")):
            self.assertIsNone(client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS))
        with patch.object(client, "_get", return_value={}):
            self.assertIsNone(client.missing_permission(PERMISSION_EDIT_FILE_RELATIONSHIPS))


class TestErrorStatusCodes(unittest.TestCase):
    """HydrusError now carries the HTTP status. Matching on message text
    instead would break the moment somebody rewords one."""

    def _response(self, status):
        response = MagicMock()
        response.status_code = status
        response.text = "nope"
        response.json.return_value = {}
        return response

    def test_the_status_is_carried_through(self):
        from core.hydrus_client import HydrusClient, HydrusError
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                with self.assertRaises(HydrusError) as caught:
                    HydrusClient._handle("/x", self._response(status))
                self.assertEqual(caught.exception.status_code, status)

    def test_a_locally_raised_error_has_no_status(self):
        from core.hydrus_client import HydrusError
        self.assertIsNone(HydrusError("nothing to do with HTTP").status_code)

    def test_403_and_404_become_the_two_messages_a_user_can_act_on(self):
        from core.hydrus_client import HydrusClient, HydrusError
        forbidden = HydrusClient._relationship_failure(HydrusError("403 text", 403))
        self.assertIn("Edit File Relationships", str(forbidden))
        self.assertIn("review services", str(forbidden))

        old = HydrusClient._relationship_failure(HydrusError("404 text", 404))
        self.assertIn("too old", str(old))
        self.assertIn("/manage_file_relationships", str(old))

        # Anything else is passed through rather than dressed up as a
        # diagnosis it isn't.
        other = HydrusError("disk on fire", 500)
        self.assertIs(HydrusClient._relationship_failure(other), other)


if __name__ == "__main__":
    unittest.main()
