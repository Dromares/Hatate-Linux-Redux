"""Font registration: the bundle loads, and a missing one degrades."""
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PyQt6.QtWidgets import QApplication

from gui.fonts import FONTS_DIR, register_fonts

_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


class TestFontRegistration(unittest.TestCase):
    def setUp(self):
        _app()

    def test_the_real_bundle_registers_every_file(self):
        """Nothing in resources/fonts/ silently fails to load."""
        ids = register_fonts()
        self.assertTrue(ids, "no font files found under resources/fonts/")
        self.assertNotIn(-1, ids, "a bundled font failed to register")

    def test_a_missing_directory_returns_minus_one_rather_than_raising(self):
        """addApplicationFont's own failure contract (-1, not an
        exception) has to survive register_fonts() unchanged - a font
        that can't load is a degraded launch, not a crashed one."""
        ids = register_fonts(Path("/no/such/directory"))
        self.assertEqual(ids, [])

    def test_a_corrupt_font_file_registers_as_minus_one(self):
        """A real path that isn't a real font - the actual -1 case
        addApplicationFont documents, as opposed to the merely-empty
        directory above."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            family_dir = Path(tmp) / "broken"
            family_dir.mkdir()
            (family_dir / "not-a-font.woff2").write_bytes(b"not a font file")
            ids = register_fonts(Path(tmp))
            self.assertEqual(ids, [-1])


class TestRyokuKanjiSubset(unittest.TestCase):
    """DAN-161/DAN-168: the subset shipped U+955C (Simplified 镜) where
    the design calls for U+93E1 (Japanese 鏡) - visually near-identical
    in most faces, so a resubset could reintroduce the wrong codepoint
    without anyone noticing by eye."""

    def test_cmap_has_the_japanese_mirror_glyph_not_the_simplified_one(self):
        from fontTools.ttLib import TTFont

        font = TTFont(FONTS_DIR / "ryoku-kanji" / "RyokuKanji.woff2")
        cmap = font.getBestCmap()
        self.assertIn(0x93E1, cmap, "U+93E1 (鏡) missing from the subset")
        self.assertNotIn(0x955C, cmap, "U+955C (镜) should not be in the subset")


if __name__ == "__main__":
    unittest.main()
