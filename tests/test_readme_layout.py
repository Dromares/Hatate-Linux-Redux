"""README's "Project layout" section must describe the tree that exists
(DAN-49, from the DAN-41 slate's "Considered and not proposed").

Same blind-spot shape as tests/test_gui_harness.py:334-365, which
enumerates gui/ from the filesystem rather than from a hardcoded list
precisely so a new module cannot be invisible to the check: a guarantee
that only covers what somebody remembered to list reads as holding when
it is not.

The layout section had drifted to 3 of 23 `gui/` modules and 2 of 12
`workers/` modules. That is worse than having no layout section, because
a reader takes it as current - and the modules it happened to omit
included every one this issue found untested.

Deliberately NOT asserted:

  * `core/` completeness. The `core/` block is a curated reading order
    with nested re-export groupings (search_engine.py and the six modules
    it re-exports), and flattening it to one line per file to satisfy a
    test would make the section less useful to a reader than it is now.
    The two directories asserted here are flat lists already.
  * descriptions. Whether a one-line summary is accurate is not something
    a test can check, and an empty-string description would satisfy any
    rule that tried. The names are the part a missing module makes wrong.
"""
import pathlib
import re
import unittest

from . import _path  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

# The directories whose listing is flat enough to be checked exactly. See
# the module docstring for why core/ is not among them.
CHECKED_DIRS = ("gui", "workers")


def _layout_block() -> str:
    """The fenced code block immediately under "## Project layout"."""
    text = README.read_text(encoding="utf-8")
    heading = re.search(r"^## Project layout\s*$", text, re.MULTILINE)
    assert heading, 'README has no "## Project layout" heading'
    fence = re.search(r"^```\n(.*?)^```\s*$", text[heading.end():],
                      re.MULTILINE | re.DOTALL)
    assert fence, 'the "Project layout" section has no fenced code block'
    return fence.group(1)


def _listed(directory: str) -> set:
    """The module stems listed under `directory/` in the layout block.

    Entries are indented under a `directory/` line and end at the next
    line that is not indented (the next directory, or the end).
    """
    modules = set()
    inside = False
    for line in _layout_block().splitlines():
        if not line.strip():
            continue
        if re.match(rf"^{re.escape(directory)}/\s*$", line):
            inside = True
            continue
        if inside:
            if not line.startswith(("  ", "\t")):
                break                      # dedented - out of this directory
            name = line.split()[0]
            if name.endswith(".py"):
                modules.add(name[:-3])
    return modules


def _on_disk(directory: str) -> set:
    return {p.stem for p in (ROOT / directory).glob("*.py") if p.stem != "__init__"}


class TestReadmeLayout(unittest.TestCase):
    def test_the_layout_section_and_its_code_block_exist(self):
        """Every assertion below is vacuously true if the section was
        renamed or unfenced, so the parse itself is a test."""
        block = _layout_block()
        self.assertIn("main.py", block)
        for directory in CHECKED_DIRS:
            self.assertIn(f"{directory}/", block, f"no {directory}/ block in the layout")

    def test_every_gui_and_worker_module_is_named(self):
        """A module added without a line here fails HERE, rather than
        leaving the section quietly describing an older tree."""
        for directory in CHECKED_DIRS:
            with self.subTest(directory=directory):
                on_disk, listed = _on_disk(directory), _listed(directory)
                self.assertEqual(
                    on_disk - listed, set(),
                    f"{directory}/ modules missing from README's Project layout: "
                    f"{sorted(on_disk - listed)}")

    def test_nothing_is_listed_that_no_longer_exists(self):
        """The other direction, and the one a deletion breaks: a line for a
        module that was removed sends a reader looking for a file that
        isn't there."""
        for directory in CHECKED_DIRS:
            with self.subTest(directory=directory):
                on_disk, listed = _on_disk(directory), _listed(directory)
                self.assertEqual(
                    listed - on_disk, set(),
                    f"README's Project layout names {directory}/ modules that do not "
                    f"exist: {sorted(listed - on_disk)}")

    def test_the_parser_is_reading_the_right_block(self):
        """Guards the check itself: if _listed() silently returned nothing,
        the "missing" assertion above would pass for an empty README
        section. Both directories must come back non-trivially populated.
        """
        for directory in CHECKED_DIRS:
            with self.subTest(directory=directory):
                self.assertGreaterEqual(len(_listed(directory)), 5)
                self.assertGreaterEqual(len(_on_disk(directory)), 5)

    def test_a_directory_listing_stops_at_the_next_dedented_line(self):
        """gui/ and workers/ are adjacent blocks, so a parser that ran past
        the dedent would report each as containing the other's modules and
        every assertion above would pass for the wrong reason."""
        self.assertEqual(_listed("gui") & _listed("workers"), set())


if __name__ == "__main__":
    unittest.main()
