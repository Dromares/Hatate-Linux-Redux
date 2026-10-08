"""core/image_prep.py - the upload size cap (DAN-74).

At 4c1acd9 this module sat at 42%: everything past the "already small
enough" early return was unexecuted, which is the entire reason the module
exists. Specifically uncovered were the stat-failure fallback (63-66), the
over-cap branch (84-103) and all of `_downscale_to_fit` (107-132) - the
JPEG quality ladder and the shrink loop.

That gap matters more than the percentage suggests, because of how this
module fails. It does not crash: `prepare_upload_bytes` catches everything
and hands back the original bytes. What a bug here produces is an HTTP 413
from IQDB or SauceNAO - for large files only, on a rate-limited service,
in the unattended half of a run. The symptom is a search that quietly
does not work for exactly the files that needed downscaling, and nothing
in the app says so.

So the ladder is driven with REAL generated images rather than a mocked
Pillow. Pillow is a hard dependency (requirements.txt), and the thing
under test is whether the loop actually converges under a byte cap - a
fake `save()` that returns whatever the test wants would assert the loop's
shape and prove nothing about its outcome. The images are random noise on
purpose: noise does not compress, so a 1200x1200 PNG is reliably over any
cap a test sets, and each rung of the quality ladder produces a
measurably different size.

Module-global discipline: `_last_prepared` is a module-level one-entry
cache, so it leaks between tests exactly the way the Gelbooru-family
`_last_api_post` memo does (Finding 2 from DAN-13). It is reset in both
setUp and tearDown, and TestPrepCache is the explicit proof the reset
happens.
"""
import io
import os
import random
import tempfile
import threading
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401

from PIL import Image

from core import image_prep


def _noise_image(size=(1200, 1200), mode="RGB"):
    """Random noise, which is the point: it does not compress, so a PNG of
    it is reliably larger than any cap these tests set, and each quality
    rung of the ladder lands at a genuinely different size."""
    rand = random.Random(20740)  # fixed seed - sizes must be reproducible
    width, height = size
    if mode == "L":
        data = bytes(rand.randrange(256) for _ in range(width * height))
    elif mode == "RGBA":
        data = bytes(rand.randrange(256) for _ in range(width * height * 4))
    else:
        data = bytes(rand.randrange(256) for _ in range(width * height * 3))
    return Image.frombytes(mode, size, data)


class _PrepCase(unittest.TestCase):
    """Shared temp directory plus the module-global reset."""

    def setUp(self):
        image_prep._last_prepared = None
        self._dir = tempfile.TemporaryDirectory(prefix="hatate-dan74-")
        self.addCleanup(self._dir.cleanup)

    def tearDown(self):
        image_prep._last_prepared = None

    def _write(self, name, data):
        path = os.path.join(self._dir.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _write_png(self, name="big.png", size=(1200, 1200), mode="RGB"):
        buf = io.BytesIO()
        _noise_image(size, mode).save(buf, format="PNG")
        return self._write(name, buf.getvalue())


class TestSmallEnoughFile(_PrepCase):
    """Under the cap, the original bytes go up untouched."""

    def test_raw_bytes_and_original_filename_are_used_unchanged(self):
        raw = b"not really an image, and it does not need to be"
        path = self._write("small.png", raw)

        data, filename = image_prep.prepare_upload_bytes(path, max_bytes=1024)

        self.assertEqual(data, raw)
        self.assertEqual(filename, "small.png")

    def test_no_decode_happens_for_a_file_already_under_the_cap(self):
        # The whole point of the early return: a small file must not pay
        # for a Pillow decode.
        path = self._write("small.png", b"x" * 10)
        with patch.object(image_prep, "_downscale_to_fit") as downscale:
            image_prep.prepare_upload_bytes(path, max_bytes=1024)
        downscale.assert_not_called()

    def test_a_file_exactly_at_the_cap_is_not_downscaled(self):
        # The comparison is `<=`, so the boundary belongs to the cheap path.
        raw = b"y" * 512
        path = self._write("exact.png", raw)
        with patch.object(image_prep, "_downscale_to_fit") as downscale:
            data, filename = image_prep.prepare_upload_bytes(path, max_bytes=512)
        downscale.assert_not_called()
        self.assertEqual(data, raw)
        self.assertEqual(filename, "exact.png")


class TestStatFailure(_PrepCase):
    """63-66: the size cannot be read, so prep is skipped entirely.

    Sending the original is the right degradation - it is what this code
    replaced, and a 413 is recoverable where refusing to search is not.
    """

    def test_unreadable_size_falls_back_to_the_raw_bytes(self):
        raw = b"contents that still read fine"
        path = self._write("odd.png", raw)

        with patch.object(image_prep.os.path, "getsize",
                          side_effect=OSError("stat: no")):
            data, filename = image_prep.prepare_upload_bytes(path, max_bytes=1)

        self.assertEqual(data, raw)
        self.assertEqual(filename, "odd.png")

    def test_stat_failure_does_not_poison_the_cache(self):
        # The fallback returns before the cache is written. If it wrote,
        # the next caller would be served bytes keyed on a size nobody
        # managed to read.
        path = self._write("odd.png", b"abc")
        with patch.object(image_prep.os.path, "getsize",
                          side_effect=OSError("stat: no")):
            image_prep.prepare_upload_bytes(path)
        self.assertIsNone(image_prep._last_prepared)


class TestOverCapDownscale(_PrepCase):
    """84-103 and 107-132: the branch the module exists for."""

    def test_result_fits_under_the_cap_and_is_a_real_jpeg(self):
        # The caps in this class are picked from measured sizes of this
        # exact image, not guessed: 1200x1200 seeded noise is 4.3 MB as a
        # PNG and 1.19 MB / 914 KB / 757 KB / 640 KB / 552 KB / 467 KB as
        # a JPEG at qualities 90 down to 40. 1.2 MB is therefore the one
        # cap that quality 90 alone satisfies, so nothing shrinks.
        path = self._write_png()
        cap = 1_200_000
        self.assertGreater(os.path.getsize(path), cap)

        data, filename = image_prep.prepare_upload_bytes(path, max_bytes=cap)

        self.assertLessEqual(len(data), cap)
        self.assertEqual(filename, "big.jpg")
        with Image.open(io.BytesIO(data)) as out:
            # Not just "smaller bytes" - it has to be something the
            # services can actually decode.
            self.assertEqual(out.format, "JPEG")
            self.assertEqual(out.size, (1200, 1200))  # cap met on quality alone

    def test_max_dimension_is_applied_before_the_quality_ladder(self):
        path = self._write_png()

        data, _ = image_prep.prepare_upload_bytes(
            path, max_bytes=120 * 1024, max_dimension=300,
        )

        with Image.open(io.BytesIO(data)) as out:
            self.assertLessEqual(max(out.size), 300)

    def test_a_small_but_heavy_file_is_never_shrunk_below_the_floor(self):
        # 400x400 is inside max_dimension, so the thumbnail step is
        # skipped, and it is also already at/under the shrink loop's 500px
        # floor - so the dimensions come out untouched even though the
        # cap is unreachable. This is the deliberate floor, not a bug: a
        # sub-500px upload has too little detail left to match on.
        path = self._write_png(size=(400, 400))

        data, _ = image_prep.prepare_upload_bytes(
            path, max_bytes=8 * 1024, max_dimension=3000,
        )

        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(out.size, (400, 400))

    def test_quality_ladder_walks_down_when_quality_90_is_not_enough(self):
        # A cap that quality 90 cannot meet but a lower rung can, so the
        # `quality -= 10` step is the thing that succeeds. Recording the
        # sizes it actually produced is what makes the walk visible.
        path = self._write_png()
        qualities = []
        real_save = Image.Image.save

        def spy(self, fp, *args, **kwargs):
            if "quality" in kwargs:
                qualities.append(kwargs["quality"])
            return real_save(self, fp, *args, **kwargs)

        cap = 700_000  # between the measured q70 (757 KB) and q60 (640 KB)
        with patch.object(Image.Image, "save", spy):
            data, _ = image_prep.prepare_upload_bytes(path, max_bytes=cap)

        self.assertLessEqual(len(data), cap)
        # Descending in tens from 90, and it STOPPED at 60 rather than
        # running the ladder out - the `break` is what proves the cap was
        # met by quality alone, with no shrinking.
        self.assertEqual(qualities, [90, 80, 70, 60])
        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(out.size, (1200, 1200))

    def test_shrink_loop_runs_when_even_quality_40_is_too_big(self):
        # 300 KB is under the measured quality-40 size (467 KB), so the
        # ladder runs out and the second loop (thumbnail to 80%, re-save
        # at 70) has to run for this to fit at all.
        path = self._write_png()
        cap = 300_000

        data, _ = image_prep.prepare_upload_bytes(path, max_bytes=cap)

        self.assertLessEqual(len(data), cap)
        with Image.open(io.BytesIO(data)) as out:
            self.assertLess(max(out.size), 1200)

    def test_shrink_loop_stops_at_the_500px_floor_rather_than_forever(self):
        # An impossible cap. The loop's guard is `max(im.size) > 500`, so
        # it must terminate with an image around that floor and hand back
        # an over-cap result rather than spinning or raising.
        path = self._write_png()

        data, filename = image_prep.prepare_upload_bytes(path, max_bytes=1)

        self.assertEqual(filename, "big.jpg")
        self.assertGreater(len(data), 1)  # honestly still over the cap
        with Image.open(io.BytesIO(data)) as out:
            self.assertLessEqual(max(out.size), 500)

    def test_rgba_is_converted_since_jpeg_has_no_alpha_channel(self):
        # Saving an RGBA image as JPEG raises; the convert() is what stops
        # every transparent PNG from falling into the raw-bytes fallback.
        path = self._write_png("alpha.png", size=(900, 900), mode="RGBA")

        data, filename = image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024)

        self.assertEqual(filename, "alpha.jpg")
        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(out.format, "JPEG")
            self.assertEqual(out.mode, "RGB")

    def test_greyscale_is_kept_as_L_rather_than_widened_to_rgb(self):
        # "L" is in the pass-through set: JPEG encodes it natively, and
        # converting would triple the data for no visual gain.
        path = self._write_png("grey.png", size=(900, 900), mode="L")

        data, _ = image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024)

        with Image.open(io.BytesIO(data)) as out:
            self.assertEqual(out.mode, "L")

    def test_extensionless_name_still_gets_a_jpg_suffix(self):
        buf = io.BytesIO()
        _noise_image((900, 900)).save(buf, format="PNG")
        path = self._write("noextension", buf.getvalue())

        _, filename = image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024)

        self.assertEqual(filename, "noextension.jpg")


class TestDownscaleFailureDegrades(_PrepCase):
    """100-103: a bug in here must not be able to block a search."""

    def test_raw_bytes_are_sent_when_downscaling_raises(self):
        raw = b"z" * 4096
        path = self._write("broken.png", raw)

        with patch.object(image_prep, "_downscale_to_fit",
                          side_effect=ValueError("pretend Pillow gave up")):
            data, filename = image_prep.prepare_upload_bytes(path, max_bytes=16)

        self.assertEqual(data, raw)
        self.assertEqual(filename, "broken.png")  # original name, original format

    def test_a_truncated_file_is_not_fatal(self):
        # Not a mocked failure: real bytes Pillow genuinely cannot open.
        raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
        path = self._write("truncated.png", raw)

        data, filename = image_prep.prepare_upload_bytes(path, max_bytes=64)

        self.assertEqual(data, raw)
        self.assertEqual(filename, "truncated.png")

    def test_a_failed_downscale_does_not_cache_the_raw_bytes(self):
        # Caching the fallback would make one transient failure stick for
        # every later engine asking about the same file.
        path = self._write("broken.png", b"z" * 4096)
        with patch.object(image_prep, "_downscale_to_fit",
                          side_effect=ValueError("nope")):
            image_prep.prepare_upload_bytes(path, max_bytes=16)
        self.assertIsNone(image_prep._last_prepared)


class TestPrepCache(_PrepCase):
    """The one-entry memo. IQDB and SauceNAO each call this for the same
    file within one search, so the second call must not re-decode."""

    def test_the_second_caller_gets_the_first_callers_work(self):
        path = self._write_png(size=(900, 900))
        cap = 60 * 1024

        first, first_name = image_prep.prepare_upload_bytes(path, max_bytes=cap)
        with patch.object(image_prep, "_downscale_to_fit") as downscale:
            second, second_name = image_prep.prepare_upload_bytes(path, max_bytes=cap)

        downscale.assert_not_called()
        self.assertIs(second, first)
        self.assertEqual(second_name, first_name)

    def test_a_different_cap_is_a_different_key(self):
        # Same file, different limit - reusing the memo here would hand
        # SauceNAO bytes prepared for IQDB's (different) cap.
        path = self._write_png(size=(900, 900))
        image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024)

        with patch.object(image_prep, "_downscale_to_fit",
                          return_value=(b"fresh", "x.jpg")) as downscale:
            data, _ = image_prep.prepare_upload_bytes(path, max_bytes=30 * 1024)

        downscale.assert_called_once()
        self.assertEqual(data, b"fresh")

    def test_a_different_max_dimension_is_a_different_key(self):
        path = self._write_png(size=(900, 900))
        image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024, max_dimension=3000)

        with patch.object(image_prep, "_downscale_to_fit",
                          return_value=(b"fresh", "x.jpg")) as downscale:
            image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024, max_dimension=400)

        downscale.assert_called_once()

    def test_a_file_that_changed_size_is_re_prepared(self):
        # file_size is in the key as a cheap correctness check. Without it,
        # a file edited between two engines' calls would upload the old
        # picture under the new one's name.
        path = self._write("small.png", b"a" * 100)
        first, _ = image_prep.prepare_upload_bytes(path, max_bytes=1024)
        self.assertEqual(first, b"a" * 100)

        with open(path, "wb") as fh:
            fh.write(b"b" * 200)
        second, _ = image_prep.prepare_upload_bytes(path, max_bytes=1024)

        self.assertEqual(second, b"b" * 200)

    def test_a_different_path_is_a_different_key(self):
        one = self._write("one.png", b"1" * 50)
        two = self._write("two.png", b"2" * 50)

        image_prep.prepare_upload_bytes(one, max_bytes=1024)
        data, filename = image_prep.prepare_upload_bytes(two, max_bytes=1024)

        self.assertEqual(data, b"2" * 50)
        self.assertEqual(filename, "two.png")

    def test_only_the_most_recent_result_is_kept(self):
        # Deliberately one entry, not an LRU: the access pattern is two
        # calls for the same file, seconds apart.
        one = self._write("one.png", b"1" * 50)
        two = self._write("two.png", b"2" * 50)
        image_prep.prepare_upload_bytes(one, max_bytes=1024)
        image_prep.prepare_upload_bytes(two, max_bytes=1024)

        self.assertEqual(image_prep._last_prepared[0], two)

    def test_concurrent_callers_decode_once(self):
        # The engines can run in parallel, which is the case the lock is
        # for. Serialised by _prep_lock, the first thread through fills
        # the memo and the rest hit it.
        path = self._write_png(size=(900, 900))
        calls = []
        real = image_prep._downscale_to_fit

        def counting(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        results = []
        with patch.object(image_prep, "_downscale_to_fit", counting):
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        image_prep.prepare_upload_bytes(path, max_bytes=60 * 1024)))
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(calls), 1, "the same file was decoded more than once")
        self.assertEqual(len(results), 4)
        self.assertEqual({data for data, _ in results}, {results[0][0]})

    def test_the_module_global_is_reset_between_tests(self):
        # The explicit proof that setUp/tearDown clear the memo - without
        # it, a later test would silently be served an earlier test's
        # bytes and pass for the wrong reason.
        self.assertIsNone(image_prep._last_prepared)


class TestDefaults(unittest.TestCase):
    def test_the_default_cap_leaves_headroom_under_the_cited_8_mib_limit(self):
        # The module's docstring is explicit that this is deliberately
        # under the commonly-cited limit rather than at it.
        self.assertLess(image_prep.DEFAULT_MAX_BYTES, 8 * 1024 * 1024)
        self.assertEqual(image_prep.DEFAULT_MAX_DIMENSION, 3000)


if __name__ == "__main__":
    unittest.main()
