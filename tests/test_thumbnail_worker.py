"""workers/thumbnail_worker.py - row thumbnail decoding (DAN-74).

At 4c1acd9 this sat at 56% and it is on the hot path: it is what runs for
every row of every batch the user adds. Unexecuted were the whole Hydrus
side (`_known_to_hydrus`, `_decode_from_bytes`), the scaled local decode,
the `source == "off"` short circuit, and every stop-request branch.

`run()` is called DIRECTLY on the test's own thread rather than through
`start()`, for the reasons tests/test_workers_background.py sets out at
length: the signal emissions are then delivered synchronously at emit
time, so order and payload can be asserted without pumping a Qt event
loop, and `run()` is the whole behaviour under test.

The images are real - tiny PNGs generated with Pillow, decoded by real
QImageReader/QImage. Stubbing the decoder would leave the one thing worth
checking (that a scaled decode produces an image of the requested size,
and that an undecodable file yields None rather than a null QImage the
GUI would then try to paint) asserted against the stub.

No network: HydrusClient is a MagicMock everywhere it appears.
"""
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

try:
    from PyQt6.QtCore import QThread  # noqa: F401 - import probe only
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


def _png_bytes(size=(200, 200), color=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _settings(source="local", access_key=""):
    from core.config import Settings
    settings = Settings()
    settings.thumbnail_source = source
    settings.hydrus.access_key = access_key
    return settings


def _entry(path, hydrus_hash=None):
    from core.models import ImageEntry
    entry = ImageEntry(path=path)
    entry.hydrus_hash = hydrus_hash
    return entry


def _worker(entries, size=64, settings=None):
    from workers.thumbnail_worker import ThumbnailWorker
    return ThumbnailWorker(entries, size, settings)


class _Recorder:
    """Collects every signal the worker emits, in order.

    Connected before run() and read after, so a test asserts on what the
    GUI thread would actually have received - including that
    `finished_all` arrives exactly once even on the early-return paths.
    """

    def __init__(self, worker):
        self.thumbnails = []
        self.progress = []
        self.finished = 0
        worker.thumbnail_ready.connect(
            lambda entry, image: self.thumbnails.append((entry, image)))
        worker.progress.connect(lambda done, total: self.progress.append((done, total)))
        worker.finished_all.connect(self._finish)

    def _finish(self):
        self.finished += 1


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class _ThumbnailCase(unittest.TestCase):
    """Base for every case here. The Qt skip lives on this one class and is
    inherited, so a machine without PyQt6 skips the module wholesale."""

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory(prefix="hatate-dan74-thumb-")
        self.addCleanup(self._dir.cleanup)

    def _image_file(self, name="row.png", size=(200, 200)):
        path = os.path.join(self._dir.name, name)
        with open(path, "wb") as fh:
            fh.write(_png_bytes(size))
        return path


class TestThumbnailSource(_ThumbnailCase):
    def test_source_defaults_to_local_when_there_are_no_settings(self):
        worker = _worker([])
        self.assertEqual(worker._thumbnail_source(), "local")

    def test_source_is_read_from_settings(self):
        worker = _worker([], settings=_settings(source="hydrus"))
        self.assertEqual(worker._thumbnail_source(), "hydrus")

    def test_off_reads_no_files_at_all_but_still_finishes(self):
        # The GUI waits on finished_all to clear its progress state. An
        # early return that skipped it would leave the window showing a
        # thumbnail pass that never ends.
        path = self._image_file()
        worker = _worker([_entry(path)], settings=_settings(source="off"))
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual(recorder.thumbnails, [])
        self.assertEqual(recorder.progress, [])
        self.assertEqual(recorder.finished, 1)

    def test_hydrus_source_without_an_access_key_falls_back_to_local(self):
        # Selecting "hydrus" but never pasting a key is an easy state to
        # end up in; it must degrade to reading the file, not to no
        # thumbnails at all.
        path = self._image_file()
        worker = _worker([_entry(path, hydrus_hash="a" * 64)],
                         settings=_settings(source="hydrus", access_key=""))
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual(len(recorder.thumbnails), 1)
        self.assertEqual(recorder.finished, 1)


class TestLocalDecode(_ThumbnailCase):
    def test_a_large_image_is_decoded_down_to_the_requested_size(self):
        # The scaled decode is the reason this class exists - asking the
        # reader for a small image instead of loading full resolution and
        # shrinking afterwards.
        worker = _worker([], size=64)

        image = worker._decode(self._image_file(size=(800, 600)))

        self.assertIsNotNone(image)
        self.assertLessEqual(max(image.width(), image.height()), 64)

    def test_an_image_already_smaller_than_the_target_is_left_alone(self):
        worker = _worker([], size=256)

        image = worker._decode(self._image_file(size=(32, 24)))

        self.assertIsNotNone(image)
        self.assertEqual((image.width(), image.height()), (32, 24))

    def test_an_undecodable_file_yields_None_rather_than_a_null_image(self):
        # A null QImage is not falsy in a way the caller checks for, so
        # returning one would put an empty pixmap in the row.
        path = os.path.join(self._dir.name, "notanimage.png")
        with open(path, "wb") as fh:
            fh.write(b"this is not a PNG")
        worker = _worker([])

        self.assertIsNone(worker._decode(path))

    def test_a_missing_file_yields_None(self):
        worker = _worker([])
        self.assertIsNone(worker._decode(os.path.join(self._dir.name, "gone.png")))

    def test_a_raising_decoder_yields_None_rather_than_killing_the_thread(self):
        # QImageReader does not raise for the ordinary bad inputs above, so
        # the blanket except is only reachable when Qt itself throws - and
        # it has to hold, because an exception escaping run() on a
        # background thread silently ends the whole thumbnail pass and
        # leaves the GUI waiting on finished_all forever.
        import workers.thumbnail_worker as module
        worker = _worker([])
        with patch.object(module, "QImageReader",
                          side_effect=RuntimeError("Qt image plugin blew up")):
            self.assertIsNone(worker._decode(self._image_file()))

    def test_every_decoded_row_is_emitted_with_its_own_entry(self):
        entries = [_entry(self._image_file(f"row{i}.png")) for i in range(3)]
        worker = _worker(entries, settings=_settings(source="local"))
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual([entry for entry, _ in recorder.thumbnails], entries)
        self.assertEqual(recorder.progress, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(recorder.finished, 1)

    def test_a_row_that_cannot_be_decoded_still_reports_progress(self):
        # Progress is per row, not per success. Skipping it for a failed
        # decode would make the bar stall short of the total forever.
        bad = os.path.join(self._dir.name, "bad.png")
        with open(bad, "wb") as fh:
            fh.write(b"nope")
        worker = _worker([_entry(bad), _entry(self._image_file())],
                         settings=_settings(source="local"))
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual(len(recorder.thumbnails), 1)
        self.assertEqual(recorder.progress, [(1, 2), (2, 2)])


class TestDecodeFromBytes(_ThumbnailCase):
    def test_bytes_are_scaled_down_to_the_requested_size(self):
        worker = _worker([], size=48)

        image = worker._decode_from_bytes(_png_bytes((300, 150)))

        self.assertIsNotNone(image)
        self.assertLessEqual(max(image.width(), image.height()), 48)

    def test_bytes_already_small_enough_are_not_rescaled(self):
        worker = _worker([], size=256)

        image = worker._decode_from_bytes(_png_bytes((20, 20)))

        self.assertEqual((image.width(), image.height()), (20, 20))

    def test_unrecognisable_bytes_yield_None(self):
        # Hydrus's /get_files/thumbnail never 404s, so garbage here is a
        # real shape - it is how an unknown file comes back.
        worker = _worker([], size=64)
        self.assertIsNone(worker._decode_from_bytes(b"\x00\x01\x02"))


class TestKnownToHydrus(_ThumbnailCase):
    def test_no_hashes_means_no_call_is_made_at_all(self):
        client = MagicMock()
        worker = _worker([_entry("/tmp/x.png"), _entry("/tmp/y.png")])

        self.assertEqual(worker._known_to_hydrus(client), set())
        client.filter_known_hashes.assert_not_called()

    def test_hashes_are_asked_for_in_batches(self):
        # Batched deliberately: one call per file would be a round trip
        # per row of a thousand-image batch.
        from workers.thumbnail_worker import ThumbnailWorker
        count = ThumbnailWorker.HASH_CHECK_BATCH + 5
        entries = [_entry(f"/tmp/{i}.png", hydrus_hash=f"{i:064x}") for i in range(count)]
        client = MagicMock()
        client.filter_known_hashes.side_effect = lambda batch: set(batch)
        worker = _worker(entries)

        known = worker._known_to_hydrus(client)

        self.assertEqual(len(known), count)
        self.assertEqual(client.filter_known_hashes.call_count, 2)
        first, second = client.filter_known_hashes.call_args_list
        self.assertEqual(len(first.args[0]), ThumbnailWorker.HASH_CHECK_BATCH)
        self.assertEqual(len(second.args[0]), 5)

    def test_files_with_no_hash_are_not_asked_about(self):
        entries = [_entry("/tmp/a.png", hydrus_hash="a" * 64), _entry("/tmp/b.png")]
        client = MagicMock()
        client.filter_known_hashes.side_effect = lambda batch: set(batch)
        worker = _worker(entries)

        worker._known_to_hydrus(client)

        self.assertEqual(client.filter_known_hashes.call_args.args[0], ["a" * 64])

    def test_a_hydrus_error_stops_asking_and_keeps_what_was_answered(self):
        # Hydrus being down must not mean no thumbnails - the local decode
        # is still there. Whatever came back before the failure is kept.
        from core.hydrus_client import HydrusError
        from workers.thumbnail_worker import ThumbnailWorker
        count = ThumbnailWorker.HASH_CHECK_BATCH + 5
        entries = [_entry(f"/tmp/{i}.png", hydrus_hash=f"{i:064x}") for i in range(count)]
        answers = [set(), HydrusError("connection refused")]

        def flaky(batch):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return set(batch)

        client = MagicMock()
        client.filter_known_hashes.side_effect = flaky
        worker = _worker(entries)

        known = worker._known_to_hydrus(client)

        self.assertEqual(len(known), ThumbnailWorker.HASH_CHECK_BATCH)
        self.assertEqual(client.filter_known_hashes.call_count, 2)

    def test_a_stop_request_abandons_the_remaining_batches(self):
        from workers.thumbnail_worker import ThumbnailWorker
        count = ThumbnailWorker.HASH_CHECK_BATCH * 2
        entries = [_entry(f"/tmp/{i}.png", hydrus_hash=f"{i:064x}") for i in range(count)]
        worker = _worker(entries)

        def stop_after_first(batch):
            worker.stop()
            return set(batch)

        client = MagicMock()
        client.filter_known_hashes.side_effect = stop_after_first

        worker._known_to_hydrus(client)

        self.assertEqual(client.filter_known_hashes.call_count, 1)


class TestHydrusThumbnailPath(_ThumbnailCase):
    """source == "hydrus": the file on disk is not read at all when
    Hydrus can supply the thumbnail, which is the entire saving."""

    def setUp(self):
        super().setUp()
        import workers.thumbnail_worker as module
        self.module = module
        self.client = MagicMock()
        self._real_client_class = module.HydrusClient
        module.HydrusClient = MagicMock(return_value=self.client)
        self.addCleanup(setattr, module, "HydrusClient", self._real_client_class)

    def _hydrus_worker(self, entries, size=64):
        return _worker(entries, size=size,
                       settings=_settings(source="hydrus", access_key="key"))

    def test_a_known_hash_is_served_from_hydrus_without_reading_the_file(self):
        # The path deliberately does not exist: if the worker fell back to
        # disk, the decode would fail and nothing would be emitted.
        entry = _entry(os.path.join(self._dir.name, "never-written.png"),
                       hydrus_hash="a" * 64)
        self.client.filter_known_hashes.side_effect = lambda batch: set(batch)
        self.client.get_thumbnail.return_value = _png_bytes((120, 120))
        worker = self._hydrus_worker([entry])
        recorder = _Recorder(worker)

        worker.run()

        self.client.get_thumbnail.assert_called_once_with("a" * 64)
        self.assertEqual(len(recorder.thumbnails), 1)
        self.assertEqual(recorder.finished, 1)

    def test_a_hash_hydrus_does_not_know_is_decoded_from_disk(self):
        entry = _entry(self._image_file(), hydrus_hash="b" * 64)
        self.client.filter_known_hashes.return_value = set()
        worker = self._hydrus_worker([entry])
        recorder = _Recorder(worker)

        worker.run()

        self.client.get_thumbnail.assert_not_called()
        self.assertEqual(len(recorder.thumbnails), 1)

    def test_an_empty_hydrus_thumbnail_falls_back_to_the_file(self):
        entry = _entry(self._image_file(), hydrus_hash="c" * 64)
        self.client.filter_known_hashes.side_effect = lambda batch: set(batch)
        self.client.get_thumbnail.return_value = b""
        worker = self._hydrus_worker([entry])
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual(len(recorder.thumbnails), 1)

    def test_an_undecodable_hydrus_thumbnail_falls_back_to_the_file(self):
        # Hydrus's generic "unknown file" icon comes back with HTTP 200,
        # so "it returned bytes" is not the same as "it returned a
        # thumbnail of this picture".
        entry = _entry(self._image_file(), hydrus_hash="d" * 64)
        self.client.filter_known_hashes.side_effect = lambda batch: set(batch)
        self.client.get_thumbnail.return_value = b"not an image"
        worker = self._hydrus_worker([entry])
        recorder = _Recorder(worker)

        worker.run()

        self.assertEqual(len(recorder.thumbnails), 1)


class TestStopRequest(_ThumbnailCase):
    def test_stop_sets_the_flag(self):
        worker = _worker([])
        self.assertFalse(worker._stop_requested)
        worker.stop()
        self.assertTrue(worker._stop_requested)

    def test_a_worker_stopped_before_it_starts_emits_nothing_but_finishes(self):
        entries = [_entry(self._image_file(f"row{i}.png")) for i in range(3)]
        worker = _worker(entries, settings=_settings(source="local"))
        recorder = _Recorder(worker)

        worker.stop()
        worker.run()

        self.assertEqual(recorder.thumbnails, [])
        self.assertEqual(recorder.progress, [])
        self.assertEqual(recorder.finished, 1)

    def test_stopping_part_way_through_leaves_the_earlier_rows_emitted(self):
        entries = [_entry(self._image_file(f"row{i}.png")) for i in range(4)]
        worker = _worker(entries, settings=_settings(source="local"))
        recorder = _Recorder(worker)
        worker.progress.connect(
            lambda done, total: worker.stop() if done == 2 else None)

        worker.run()

        self.assertEqual(len(recorder.thumbnails), 2)
        self.assertEqual(recorder.progress, [(1, 4), (2, 4)])
        self.assertEqual(recorder.finished, 1)


if __name__ == "__main__":
    unittest.main()
