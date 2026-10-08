"""Visual comparison: size relationships and perceptual matching.

The design decision worth pinning here is why this uses a perceptual
hash instead of a pixel difference overlay. A local file and its match
are almost always different resolutions - that's the reason to compare
them - so any pixel diff must resample one onto the other, and the
resampling alone perturbs every edge. Boorus also re-encode. A diff
would therefore report "different" for a perfect match, which is worse
than reporting nothing at all.
"""
import io
import unittest

from PIL import Image, ImageDraw

from . import _path  # noqa: F401
from core.image_compare import (
    compact_size_delta, compare_file_sizes, compare_sizes, describe_perceptual_match,
    dhash, hamming_distance, size_delta_sort_key,
)


def artwork(size):
    """Structured content rather than noise - a perceptual hash on random
    pixels tells you nothing about how it behaves on real images."""
    im = Image.new("RGB", size, (238, 220, 205))
    draw = ImageDraw.Draw(im)
    w, h = size
    draw.ellipse([w * .25, h * .08, w * .75, h * .55], fill=(30, 32, 48))
    draw.rectangle([w * .15, h * .55, w * .85, h * .95], fill=(58, 92, 176))
    for i in range(12):
        y = h * .6 + i * h * .025
        draw.line([(w * .2, y), (w * .8, y)], fill=(230, 230, 240), width=max(1, h // 300))
    draw.ellipse([w * .38, h * .22, w * .44, h * .28], fill=(250, 250, 250))
    return im


class TestSizeComparison(unittest.TestCase):
    def test_larger_match(self):
        c = compare_sizes(1000, 1500, 2000, 3000)
        self.assertIn("larger", c.verdict)
        self.assertTrue(c.remote_is_bigger)
        self.assertEqual(round(c.megapixel_ratio), 4,
                         "twice the long edge is four times the pixels")

    def test_smaller_match_is_phrased_as_smaller(self):
        """"0.5x larger" is unreadable; people say "half the size"."""
        c = compare_sizes(2000, 3000, 1000, 1500)
        self.assertIn("smaller", c.verdict)
        self.assertFalse(c.remote_is_bigger)

    def test_identical(self):
        self.assertEqual(compare_sizes(1200, 1600, 1200, 1600).verdict, "same size")

    def test_a_pixel_or_two_is_not_an_upgrade(self):
        c = compare_sizes(1200, 1600, 1201, 1601)
        self.assertEqual(c.verdict, "same size")
        self.assertFalse(c.remote_is_bigger)

    def test_different_aspect_is_called_out(self):
        """A bigger match that's also a different shape may be cropped -
        it isn't straightforwardly an upgrade, and saying only "larger"
        would imply it was."""
        c = compare_sizes(1000, 1500, 2000, 2000)
        self.assertTrue(c.aspect_differs)
        self.assertIn("different shape", c.verdict)

    def test_invalid_dimensions_yield_no_verdict(self):
        for args in [(0, 0, 100, 100), (100, 100, 0, 0), (-5, 10, 10, 10), (None, 1, 1, 1)]:
            with self.subTest(args=args):
                self.assertEqual(compare_sizes(*args).verdict, "")

    def test_file_size_comparison(self):
        self.assertIn("bigger", compare_file_sizes(1_000_000, 2_000_000))
        self.assertIn("smaller", compare_file_sizes(2_000_000, 1_000_000))
        self.assertIn("about the same", compare_file_sizes(1_000_000, 1_020_000))
        self.assertEqual(compare_file_sizes(1_000_000, None), "")
        self.assertEqual(compare_file_sizes(0, 100), "")

    def test_a_large_ratio_is_never_in_exponent_form(self):
        """Seen on the banner: "1.5e+03× bigger file"."""
        self.assertEqual(compare_file_sizes(1_000, 1_500_000), "1,500\u00d7 bigger file")
        self.assertEqual(compare_file_sizes(1_000, 150_000), "150\u00d7 bigger file")
        self.assertEqual(compare_file_sizes(150_000, 1_000), "150\u00d7 smaller file")
        self.assertEqual(compare_file_sizes(1_000, 2_500), "2.5\u00d7 bigger file")
        self.assertEqual(compare_file_sizes(1_000, 12_400), "12\u00d7 bigger file")

    def test_the_size_column_is_never_in_exponent_form_either(self):
        self.assertEqual(compact_size_delta(10, 10, 12_000, 12_000), "+ 1,200x")
        self.assertEqual(compact_size_delta(100, 100, 250, 250), "+ 2.5x")
        self.assertEqual(compact_size_delta(12_000, 12_000, 10, 10), "- 1,200x")


class TestCompactSizeDelta(unittest.TestCase):
    """The Size Difference column. Always relative to the LOCAL file, so
    the sign means the same thing on every row: + is an upgrade, - is
    smaller than what you already have."""

    def test_bigger_match(self):
        self.assertEqual(compact_size_delta(1000, 1500, 4000, 6000), "+ 4x")

    def test_smaller_match_counts_how_many_times_smaller(self):
        """"- 4x" is immediately readable; "- 0.25x" needs arithmetic."""
        self.assertEqual(compact_size_delta(4000, 6000, 1000, 1500), "- 4x")

    def test_equal(self):
        self.assertEqual(compact_size_delta(1200, 1600, 1200, 1600), "=")

    def test_a_pixel_or_two_still_counts_as_equal(self):
        self.assertEqual(compact_size_delta(1200, 1600, 1201, 1601), "=")

    def test_fractional_ratio(self):
        self.assertEqual(compact_size_delta(1000, 1500, 1500, 2250), "+ 1.5x")

    def test_unknown_dimensions_give_no_label(self):
        for args in ((1000, 1500, 0, 0), (0, 0, 1000, 1500), (None, 1, 1, 1)):
            with self.subTest(args=args):
                self.assertEqual(compact_size_delta(*args), "")

    def test_agrees_with_the_preview_panel(self):
        """The column and the comparison line describe the same pair, so
        they must never disagree about which is bigger."""
        delta = compact_size_delta(1000, 1500, 2000, 3000)
        comparison = compare_sizes(1000, 1500, 2000, 3000)
        self.assertTrue(delta.startswith("+"))
        self.assertTrue(comparison.remote_is_bigger)

    def test_sorts_by_ratio_not_by_text(self):
        """REGRESSION GUARD: sorting the labels as strings puts "+ 10x"
        before "+ 2x" and groups every "-" together, which is not what
        clicking that header asks for."""
        pairs = [
            ((1000, 1500), (4000, 6000)),    # + 4x
            ((4000, 6000), (1000, 1500)),    # - 4x
            ((1200, 1600), (1200, 1600)),    # =
            ((1000, 1500), (1500, 2250)),    # + 1.5x
        ]
        ordered = sorted(pairs, key=lambda p: size_delta_sort_key(*p[0], *p[1]))
        labels = [compact_size_delta(*p[0], *p[1]) for p in ordered]
        self.assertEqual(labels, ["- 4x", "=", "+ 1.5x", "+ 4x"])

    def test_unknown_sorts_to_one_end_not_into_parity(self):
        """An unknown size must not land next to "=" as though the two
        images were the same."""
        self.assertLess(size_delta_sort_key(1000, 1500, 0, 0),
                        size_delta_sort_key(1200, 1600, 1200, 1600))


class TestPerceptualMatching(unittest.TestCase):
    def setUp(self):
        self.original = artwork((1000, 1400))
        self.hash = dhash(self.original)

    def test_same_image_at_double_resolution(self):
        """THE case a pixel diff gets wrong."""
        bigger = self.original.resize((2000, 2800), Image.LANCZOS)
        self.assertLessEqual(hamming_distance(self.hash, dhash(bigger)), 5)

    def test_survives_heavy_jpeg_re_encoding(self):
        buf = io.BytesIO()
        self.original.save(buf, "JPEG", quality=40)
        self.assertLessEqual(hamming_distance(self.hash, dhash(buf.getvalue())), 5)

    def test_survives_downscale_and_re_encode_together(self):
        """The realistic case: a booru's sample image."""
        sample = self.original.resize((850, 1190), Image.LANCZOS)
        buf = io.BytesIO()
        sample.save(buf, "JPEG", quality=70)
        self.assertLessEqual(hamming_distance(self.hash, dhash(buf.getvalue())), 5)

    def test_different_images_are_reported_as_different(self):
        other = artwork((1000, 1400))
        draw = ImageDraw.Draw(other)
        draw.rectangle([0, 0, 1000, 700], fill=(200, 40, 40))
        draw.ellipse([100, 800, 900, 1300], fill=(20, 200, 90))
        self.assertGreater(hamming_distance(self.hash, dhash(other)), 12)

    def test_accepts_bytes_and_paths_and_images(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "a.png")
        self.original.save(path)
        buf = io.BytesIO()
        self.original.save(buf, "PNG")
        self.assertEqual(dhash(path), dhash(buf.getvalue()))
        self.assertEqual(dhash(path), dhash(self.original))

    def test_unreadable_source_returns_none_rather_than_raising(self):
        self.assertIsNone(dhash(b"not an image"))
        self.assertIsNone(dhash("/nonexistent/path.jpg"))
        self.assertIsNone(hamming_distance(None, 123))

    def test_descriptions_are_hedged_not_absolute(self):
        """64 bits isn't proof, and claiming certainty would overstate
        what a perceptual hash can tell you."""
        self.assertIn("Looks like", describe_perceptual_match(0))
        self.assertIn("Similar", describe_perceptual_match(8))
        self.assertIn("different", describe_perceptual_match(30))
        self.assertIn("Couldn't", describe_perceptual_match(None))


if __name__ == "__main__":
    unittest.main()


def _picture(seed=0, size=(600, 800)):
    """An image with real structure - bands, blocks and a diagonal - so a
    hash has something to read. Different seeds give different pictures."""
    import random
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    im = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw = ImageDraw.Draw(im)
    for _ in range(14):
        x0, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        draw.rectangle((x0, y0, x0 + rnd.randrange(60, 260), y0 + rnd.randrange(60, 260)),
                       fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    draw.line((0, 0, size[0], size[1]), fill=(255, 255, 255), width=25)
    return im


def _jpeg_thumb(im, longest=250):
    import io
    im = im.copy()
    im.thumbnail((longest, longest))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=70)
    return out.getvalue()


class TestAlignedComparison(unittest.TestCase):
    """compare_to_local tolerates what engine thumbnails do to a picture.
    Measured on 418 real library images before this existed: a 4% crop
    reached 90% for only 18% of them, letterboxing 25%, a mirror 0.5%."""

    def setUp(self):
        from core.image_compare import local_prints
        self.local = _picture(1)
        self.prints = local_prints(self.local)

    def _score(self, im):
        from core.image_compare import aligned_similarity
        return aligned_similarity(self.prints, _jpeg_thumb(im))

    def test_a_plain_thumbnail_is_the_same_picture(self):
        self.assertGreaterEqual(self._score(self.local)[0], 90)

    def test_a_cropped_copy_is_the_same_picture(self):
        w, h = self.local.size
        for box in ((24, 32, w - 24, h - 32), (0, 0, w, int(h * 0.9))):
            with self.subTest(box=box):
                self.assertGreaterEqual(self._score(self.local.crop(box))[0], 90)

    def test_a_letterboxed_copy_is_the_same_picture(self):
        from PIL import ImageOps
        padded = ImageOps.expand(self.local, border=(0, 64), fill=(0, 0, 0))
        self.assertGreaterEqual(self._score(padded)[0], 90)

    def test_a_mirrored_copy_is_recognised_but_kept_below_auto_import(self):
        from PIL import ImageOps
        from core.image_compare import MIRRORED_MAX_SIMILARITY
        score, mirrored = self._score(ImageOps.mirror(self.local))
        self.assertTrue(mirrored)
        self.assertLessEqual(score, MIRRORED_MAX_SIMILARITY)
        self.assertGreaterEqual(score, 75)

    def test_a_different_picture_is_not(self):
        for seed in (2, 3, 4, 5):
            with self.subTest(seed=seed):
                self.assertLess(self._score(_picture(seed))[0], 75)

    def test_a_border_is_trimmed_and_a_picture_without_one_is_left_alone(self):
        from PIL import ImageOps
        from core.image_compare import trim_border
        padded = ImageOps.expand(self.local, border=(40, 0), fill=(255, 255, 255))
        self.assertEqual(trim_border(padded).size, self.local.size)
        self.assertIs(trim_border(self.local), self.local)

    def test_nothing_to_compare_gives_no_score(self):
        from core.image_compare import aligned_similarity, local_prints
        self.assertEqual(aligned_similarity(None, _jpeg_thumb(self.local)), (None, False))
        self.assertIsNone(local_prints(b"not an image"))
        self.assertEqual(aligned_similarity(self.prints, b"not an image"), (None, False))

    def test_a_90_the_fine_hash_does_not_confirm_is_held_for_review(self):
        """An edited variant looks identical at 64 bits. Measured against
        IQDB: variants sat 23-61 apart at 256 bits, genuine matches within
        16, same-picture thumbnails within 20."""
        from unittest.mock import patch
        from core.image_compare import (
            FINE_CONFIRM_MAX, UNCONFIRMED_MAX_SIMILARITY, Comparison, aligned_similarity)
        cases = ((Comparison(1, False, FINE_CONFIRM_MAX + 10), UNCONFIRMED_MAX_SIMILARITY),
                 (Comparison(1, False, None), UNCONFIRMED_MAX_SIMILARITY),
                 (Comparison(1, False, FINE_CONFIRM_MAX), 98.0),
                 (Comparison(10, False, 60), None))            # below 90 anyway: untouched
        for comparison, expected in cases:
            with self.subTest(comparison=comparison), \
                 patch("core.image_compare.compare_to_local", return_value=comparison):
                score, _ = aligned_similarity(self.prints, b"x")
                if expected is None:
                    self.assertLess(score, 90)
                else:
                    self.assertEqual(score, expected)

    def test_an_oversized_image_fails_quietly(self):
        """Seen on a real 256-megapixel file where Pillow's size limit
        was still in force."""
        from unittest.mock import patch
        from PIL import Image
        from core.image_compare import local_prints
        with patch("core.image_compare.Image.open",
                   side_effect=Image.DecompressionBombError("too big")):
            self.assertIsNone(local_prints("/tmp/huge.png"))
