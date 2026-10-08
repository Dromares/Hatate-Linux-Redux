"""Add tags... dialog: what the user typed vs. the Tag objects it hands back.

gui/add_tags_dialog.py's AddTagsDialog.get_tags() had construction-only
coverage from the DAN-21 harness (it opens and closes fine), never a test
of what get_tags() actually returns for real input.
"""
import os
import tempfile
import unittest

from . import _path  # noqa: F401

# Must be set before PyQt6 is imported, or Qt tries to reach a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-add-tags-dialog-")

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


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TestGetTags(unittest.TestCase):
    def setUp(self):
        from gui.add_tags_dialog import AddTagsDialog
        self.dialog = AddTagsDialog("3 selected")
        self.addCleanup(self.dialog.deleteLater)

    def _tags(self, text):
        self.dialog.edit.setPlainText(text)
        return self.dialog.get_tags()

    def test_a_plain_line_has_no_namespace(self):
        tags = self._tags("1girl")
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].name, "1girl")
        self.assertIsNone(tags[0].namespace)

    def test_namespace_colon_name_splits_cleanly(self):
        tags = self._tags("character:reimu")
        self.assertEqual(tags[0].namespace, "character")
        self.assertEqual(tags[0].name, "reimu")

    def test_a_space_before_the_colon_must_not_end_up_in_the_namespace(self):
        """REGRESSION (DAN-81): typing "character : reimu" (a space
        either side of the colon reads naturally as the same tag as
        "character:reimu") produced namespace "character " - trailing
        space and all. Hydrus and the rest of this app treat "character"
        and "character " as different namespaces, so the tag silently
        landed under a namespace nothing else uses."""
        tags = self._tags("character : reimu")
        self.assertEqual(tags[0].namespace, "character")
        self.assertEqual(tags[0].name, "reimu")

    def test_a_trailing_space_before_the_colon_alone_is_also_stripped(self):
        tags = self._tags("character :reimu")
        self.assertEqual(tags[0].namespace, "character")

    def test_blank_lines_are_skipped(self):
        tags = self._tags("1girl\n\n   \ncreator:someone")
        self.assertEqual([t.name for t in tags], ["1girl", "someone"])

    def test_one_tag_per_line(self):
        tags = self._tags("1girl\ncharacter:reimu\ncreator:someone")
        self.assertEqual(len(tags), 3)


if __name__ == "__main__":
    unittest.main()
