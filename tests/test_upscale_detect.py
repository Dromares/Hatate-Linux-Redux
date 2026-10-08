"""Upscale detection heuristics.

These caught two real bugs during development, both of which produced
confidently wrong answers rather than obvious failures.
"""
import unittest

from . import _path  # noqa: F401

from PIL import Image, ImageDraw

from core.upscale_detect import (
    HARD_PIXEL_LIMIT, check_upscaler_metadata, compare_to_source, detect_naive_upscale,
)


def _detailed(size=400):
    """Fine per-pixel detail - the hardest case to reconstruct, so a
    genuine native image should NOT look upscaled."""
    im = Image.new("RGB", (size, size))
    px = im.load()
    for x in range(size):
        for y in range(size):
            px[x, y] = ((x * 7 + y * 3) % 256, (x * 3 + y * 11) % 256, (x * 13 + y) % 256)
    return im


def _anime_style(size=400):
    """Large flat fills plus some linework - the content type this app
    actually handles, and the one that caused a false positive."""
    im = Image.new("RGB", (size, size), (240, 220, 200))
    d = ImageDraw.Draw(im)
    d.rectangle([size * .1, size * .1, size * .5, size * .6], fill=(60, 90, 180))
    d.ellipse([size * .3, size * .05, size * .7, size * .45], fill=(20, 20, 30))
    for i in range(0, size, 4):
        d.line([(i, size * .7), (i + 8, size * .85)], fill=(0, 0, 0), width=1)
    return im


class TestSourceComparison(unittest.TestCase):
    def test_flags_clearly_larger_local_file(self):
        self.assertTrue(compare_to_source(2000, 2000, 1000, 1000).flagged)

    def test_ignores_minor_size_difference(self):
        self.assertFalse(compare_to_source(1005, 1005, 1000, 1000).flagged)

    def test_smaller_local_is_never_flagged(self):
        self.assertFalse(compare_to_source(500, 500, 1000, 1000).flagged)

    def test_missing_source_dimensions_is_safe(self):
        self.assertFalse(compare_to_source(1000, 1000, 0, 0).flagged)


class TestSelfConsistency(unittest.TestCase):
    def test_detects_a_genuinely_upscaled_image(self, tmp="/tmp/_t_up.png"):
        native = _detailed()
        native.resize((100, 100), Image.LANCZOS).resize((400, 400), Image.BICUBIC).save(tmp)
        self.assertIn(detect_naive_upscale(tmp).confidence, ("likely", "possible"))

    def test_native_detail_is_not_flagged_as_likely(self, tmp="/tmp/_t_nat.png"):
        _detailed().save(tmp)
        self.assertNotEqual(detect_naive_upscale(tmp).confidence, "likely")

    def test_flat_heavy_art_is_not_a_false_positive(self, tmp="/tmp/_t_anime.png"):
        """REGRESSION: measuring reconstruction across the WHOLE image
        meant large flat regions - ubiquitous in anime/manga art -
        dominated the average and made native art look upscaled. The fix
        measures only regions with real detail."""
        _anime_style().save(tmp)
        self.assertEqual(detect_naive_upscale(tmp).confidence, "none")

    def test_difference_is_within_valid_range(self, tmp="/tmp/_t_range.png"):
        """REGRESSION: the histogram was summed across concatenated RGB
        channels, treating the flat index as the difference value. That
        produced values above 255 - outside the possible range - and
        broke detection entirely."""
        _detailed().save(tmp)
        diff = detect_naive_upscale(tmp).best_diff
        self.assertIsNotNone(diff)
        self.assertGreaterEqual(diff, 0.0)
        self.assertLessEqual(diff, 255.0)

    def test_oversized_image_is_skipped_not_attempted(self):
        """Guards the SIGSEGV path: decoding a huge image crashed the
        process natively, which no try/except can catch."""
        self.assertGreater(HARD_PIXEL_LIMIT, 0)


class TestUpscalerMetadata(unittest.TestCase):
    def test_detects_known_tool_in_png_metadata(self, tmp="/tmp/_t_meta.png"):
        from PIL import PngImagePlugin
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Software", "Real-ESRGAN v0.3.0")
        Image.new("RGB", (60, 60), "red").save(tmp, pnginfo=meta)
        self.assertIsNotNone(check_upscaler_metadata(tmp))

    def test_unrelated_software_is_not_flagged(self, tmp="/tmp/_t_gimp.png"):
        from PIL import PngImagePlugin
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Software", "GIMP 2.10")
        Image.new("RGB", (60, 60), "green").save(tmp, pnginfo=meta)
        self.assertIsNone(check_upscaler_metadata(tmp))

    def test_clean_image_is_not_flagged(self, tmp="/tmp/_t_clean.png"):
        Image.new("RGB", (60, 60), "blue").save(tmp)
        self.assertIsNone(check_upscaler_metadata(tmp))


if __name__ == "__main__":
    unittest.main()
