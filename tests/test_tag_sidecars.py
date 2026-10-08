"""Tag sidecar files - core/sidecar.py (DAN-76, slate #8).

This is the first thing the app writes into the user's own picture
folders, so most of what is asserted here is about restraint rather than
about output: that nothing is written until `write()` is called, that an
existing file of the user's is left byte-for-byte alone unless overwriting
was asked for, that a part-written file cannot replace a good one, and
that an image with no tags gets no file rather than an empty one claiming
it has none.

The other half is agreement. `tags_to_send` decides what leaves this app
(DAN-72); a sidecar that carried a different list than the same run's
Hydrus send would be worse than no sidecar, since both look authoritative
and only one can be right. So the filtering tests here assert the sidecar
matches that helper, not that it re-implements it.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

from core import sidecar
from core.config import Settings
from core.models import ImageEntry, Tag, TagSource, tags_to_send


def _entry(path: str, tags=None) -> ImageEntry:
    e = ImageEntry(path=path)
    e.tags = list(tags or [])
    return e


def _tag(name, source=TagSource.BOORU, namespace=None) -> Tag:
    return Tag(name=name, source=source, namespace=namespace)


class TestSidecarPath(unittest.TestCase):
    def test_append_style_keeps_the_image_extension(self):
        """The default. `cat.jpg.txt` is what Hydrus's sidecar importer
        looks for, and it cannot be mistaken for a note of the user's."""
        self.assertEqual(
            sidecar.sidecar_path("/pics/cat.jpg", sidecar.STYLE_APPEND),
            Path("/pics/cat.jpg.txt"))

    def test_replace_style_drops_it(self):
        """The stable-diffusion training convention."""
        self.assertEqual(
            sidecar.sidecar_path("/pics/cat.jpg", sidecar.STYLE_REPLACE),
            Path("/pics/cat.txt"))

    def test_two_images_differing_only_in_extension_collide_under_replace(self):
        """Documented, not a bug to be fixed here - it is the reason the
        append style is the default. A folder holding both cat.jpg and
        cat.png has one sidecar between them under `replace` and one each
        under `append`."""
        jpg = sidecar.sidecar_path("/pics/cat.jpg", sidecar.STYLE_REPLACE)
        png = sidecar.sidecar_path("/pics/cat.png", sidecar.STYLE_REPLACE)
        self.assertEqual(jpg, png)

        jpg = sidecar.sidecar_path("/pics/cat.jpg", sidecar.STYLE_APPEND)
        png = sidecar.sidecar_path("/pics/cat.png", sidecar.STYLE_APPEND)
        self.assertNotEqual(jpg, png)

    def test_a_name_with_dots_in_it_is_not_truncated(self):
        """with_suffix() only replaces the last one, which is what we
        want: `a.b.jpg` is an image called `a.b`, not `a`."""
        self.assertEqual(
            sidecar.sidecar_path("/pics/a.b.jpg", sidecar.STYLE_REPLACE),
            Path("/pics/a.b.txt"))

    def test_an_unrecognised_style_setting_falls_back_to_the_default(self):
        """The value is a string in a JSON file that nothing validates on
        load. A typo should cost the less-common convention, not the
        feature."""
        s = Settings()
        s.sidecar_filename_style = "nonsense"
        self.assertEqual(sidecar.style_of(s), sidecar.STYLE_APPEND)
        self.assertEqual(sidecar.style_of(None), sidecar.STYLE_APPEND)

    def test_an_unrecognised_overwrite_setting_falls_back_to_asking(self):
        s = Settings()
        s.sidecar_overwrite = "yes please"
        self.assertEqual(sidecar.overwrite_policy_of(s), sidecar.OVERWRITE_ASK)
        self.assertEqual(sidecar.overwrite_policy_of(None), sidecar.OVERWRITE_ASK)

    def test_the_shipped_defaults_are_the_safe_ones(self):
        """A fresh config must not be able to clobber anything without a
        prompt, and must not be on the colliding naming style."""
        s = Settings()
        # Asserted on the Settings fields themselves, not only through the
        # helpers: the helpers' getattr fallback would hide the fields
        # having been dropped from the config entirely, which is how a
        # user's choice would silently stop being remembered.
        self.assertEqual(s.sidecar_filename_style, sidecar.STYLE_APPEND)
        self.assertEqual(s.sidecar_overwrite, sidecar.OVERWRITE_ASK)
        self.assertEqual(sidecar.style_of(s), sidecar.STYLE_APPEND)
        self.assertEqual(sidecar.overwrite_policy_of(s), sidecar.OVERWRITE_ASK)


class TestRender(unittest.TestCase):
    def test_one_tag_per_line_newline_terminated(self):
        body = sidecar.render(_entry("/pics/cat.jpg", [_tag("blue_sky"), _tag("cloud")]))
        self.assertEqual(body, "blue_sky\ncloud\n")

    def test_namespaces_are_written_the_way_they_are_displayed(self):
        """`character:hatsune_miku`, not the bare name - the namespace is
        half of what a booru tag means, and every reader of these files
        understands the prefixed form."""
        body = sidecar.render(_entry(
            "/pics/cat.jpg", [_tag("hatsune_miku", namespace="character")]))
        self.assertEqual(body, "character:hatsune_miku\n")

    def test_duplicates_are_dropped_keeping_the_first(self):
        """The same tag really does arrive twice - from the booru page and
        from the copy already on the file in Hydrus."""
        body = sidecar.render(_entry("/pics/cat.jpg", [
            _tag("cloud", source=TagSource.BOORU),
            _tag("blue_sky", source=TagSource.BOORU),
            _tag("cloud", source=TagSource.HYDRUS),
        ]))
        self.assertEqual(body, "cloud\nblue_sky\n")

    def test_order_is_the_entrys_own(self):
        """Not sorted. The entry's order groups tags by source, which is
        what the tag list shows and what a send uses."""
        body = sidecar.render(_entry("/pics/cat.jpg",
                                     [_tag("zebra"), _tag("aardvark")]))
        self.assertEqual(body, "zebra\naardvark\n")

    def test_no_tags_renders_nothing_at_all(self):
        """Not "\\n", not "". An empty body is the signal plan() uses to
        skip the file rather than write one that claims the image has no
        tags."""
        self.assertEqual(sidecar.render(_entry("/pics/cat.jpg", [])), "")

    def test_the_written_list_is_exactly_what_a_hydrus_send_would_carry(self):
        """The agreement this feature lives or dies on. Asserted against
        tags_to_send itself so the two cannot drift: if a later change
        moves what a send carries, this follows it."""
        entry = _entry("/pics/cat.jpg", [
            _tag("mine", source=TagSource.USER),
            _tag("from_engine", source=TagSource.SEARCH_ENGINE),
            _tag("from_booru", source=TagSource.BOORU),
        ])
        s = Settings()
        s.filter_sent_tags_by_source = True
        s.enabled_tag_sources = ["User", "Booru"]

        expected = [t.display for t in tags_to_send(entry, s)]
        self.assertEqual(sidecar.render(entry, s).splitlines(), expected)
        # ... and that this case is actually filtering something, or the
        # assertion above would hold for a render() that ignored settings.
        self.assertNotIn("from_engine", expected)

    def test_no_settings_means_every_tag(self):
        """Matching tags_to_send's own contract for a caller with no
        Settings to hand."""
        entry = _entry("/pics/cat.jpg", [
            _tag("mine", source=TagSource.USER),
            _tag("from_engine", source=TagSource.SEARCH_ENGINE),
        ])
        self.assertEqual(sidecar.render(entry).splitlines(),
                         ["mine", "from_engine"])


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def _image(self, name: str) -> str:
        path = self.root / name
        path.write_bytes(b"not really a jpeg")
        return str(path)

    def test_planning_writes_nothing(self):
        """The whole reason plan() and write() are separate."""
        entry = _entry(self._image("cat.jpg"), [_tag("cloud")])
        result = sidecar.plan([entry])
        self.assertEqual(len(result.writes), 1)
        self.assertFalse(result.writes[0].path.exists())

    def test_an_entry_with_no_tags_is_reported_not_written_empty(self):
        entry = _entry(self._image("cat.jpg"), [])
        result = sidecar.plan([entry])
        self.assertEqual(result.writes, [])
        self.assertEqual(result.without_tags, [entry])

    def test_a_missing_file_gets_no_sidecar(self):
        """Beside nothing, the file is litter in a folder the user may
        have finished with."""
        entry = _entry(str(self.root / "gone.jpg"), [_tag("cloud")])
        entry.file_missing = True
        result = sidecar.plan([entry])
        self.assertEqual(result.writes, [])
        self.assertEqual(result.missing_files, [entry])

    def test_an_existing_target_is_flagged_but_still_planned(self):
        """Flagged so the prompt can say how many would be clobbered;
        still planned because the user may answer "overwrite"."""
        path = self._image("cat.jpg")
        (self.root / "cat.jpg.txt").write_text("theirs\n", encoding="utf-8")
        result = sidecar.plan([_entry(path, [_tag("cloud")])])
        self.assertEqual(len(result.writes), 1)
        self.assertTrue(result.writes[0].exists)
        self.assertEqual(len(result.existing), 1)

    def test_directories_lists_each_folder_once(self):
        """What the confirmation prompt names, so the user can see where
        this is about to write before it does."""
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "c.jpg").write_bytes(b"x")
        entries = [
            _entry(self._image("a.jpg"), [_tag("t")]),
            _entry(self._image("b.jpg"), [_tag("t")]),
            _entry(str(sub / "c.jpg"), [_tag("t")]),
        ]
        self.assertEqual(sidecar.plan(entries).directories, [self.root, sub])

    def test_the_plan_honours_the_naming_style_setting(self):
        s = Settings()
        s.sidecar_filename_style = sidecar.STYLE_REPLACE
        result = sidecar.plan([_entry(self._image("cat.jpg"), [_tag("t")])], s)
        self.assertEqual(result.writes[0].path, self.root / "cat.txt")


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def _planned(self, name="cat.jpg", tags=("cloud", "blue_sky")):
        path = self.root / name
        path.write_bytes(b"not really a jpeg")
        entry = _entry(str(path), [_tag(t) for t in tags])
        return sidecar.plan([entry])

    def test_writes_the_file_beside_the_image(self):
        plan = self._planned()
        result = sidecar.write(plan.writes)
        target = self.root / "cat.jpg.txt"
        self.assertEqual(result.written, [target])
        self.assertEqual(target.read_text(encoding="utf-8"), "cloud\nblue_sky\n")

    def test_line_endings_are_lf_on_every_platform(self):
        """Read back as bytes. These files are consumed by scripts that
        split on \\n, and Python would otherwise translate on Windows."""
        plan = self._planned()
        sidecar.write(plan.writes)
        self.assertEqual((self.root / "cat.jpg.txt").read_bytes(),
                         b"cloud\nblue_sky\n")

    def test_an_existing_file_is_left_exactly_alone_by_default(self):
        """The promise the feature is built around: overwrite defaults to
        False, and a file already there is reported, not replaced."""
        target = self.root / "cat.jpg.txt"
        target.write_text("something of the user's\n", encoding="utf-8")
        plan = self._planned()

        result = sidecar.write(plan.writes)

        self.assertEqual(result.written, [])
        self.assertEqual(result.skipped_existing, [target])
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "something of the user's\n")

    def test_overwrite_replaces_it(self):
        target = self.root / "cat.jpg.txt"
        target.write_text("stale\n", encoding="utf-8")
        plan = self._planned()

        result = sidecar.write(plan.writes, overwrite=True)

        self.assertEqual(result.written, [target])
        self.assertEqual(target.read_text(encoding="utf-8"), "cloud\nblue_sky\n")

    def test_an_existing_file_appearing_between_plan_and_write_is_not_clobbered(self):
        """plan()'s `exists` flag is for telling the user in advance; it
        is not what enforces the promise. A check-then-write
        implementation passes every other test here and loses the user's
        file in this one."""
        plan = self._planned()
        self.assertFalse(plan.writes[0].exists)  # as far as the plan knew

        target = self.root / "cat.jpg.txt"
        target.write_text("arrived late\n", encoding="utf-8")

        result = sidecar.write(plan.writes)

        self.assertEqual(result.skipped_existing, [target])
        self.assertEqual(target.read_text(encoding="utf-8"), "arrived late\n")

    def test_overwriting_leaves_no_temporary_files_behind(self):
        """The atomic replace writes a temp file in the same folder. If
        one of those survived, every run would litter the user's picture
        folder."""
        (self.root / "cat.jpg.txt").write_text("stale\n", encoding="utf-8")
        plan = self._planned()
        sidecar.write(plan.writes, overwrite=True)
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["cat.jpg", "cat.jpg.txt"])

    def test_an_overwrite_that_fails_part_way_leaves_the_old_file_intact(self):
        """Why the overwrite path renames a finished temp file over the
        target instead of opening the target for writing: a truncated
        sidecar is indistinguishable from a real one to whatever reads it
        next, and the good copy is gone."""
        target = self.root / "cat.jpg.txt"
        target.write_text("the previous, complete sidecar\n", encoding="utf-8")
        plan = self._planned()

        def exploding_replace(src, dst):
            raise OSError(28, "No space left on device")

        with patch("core.sidecar.os.replace", exploding_replace):
            result = sidecar.write(plan.writes, overwrite=True)

        self.assertEqual(result.written, [])
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "the previous, complete sidecar\n")
        # And the temp file it had written is gone, not left in the folder.
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["cat.jpg", "cat.jpg.txt"])

    def test_the_written_file_is_not_world_readable_by_accident(self):
        """Both paths must agree on the mode. NamedTemporaryFile creates
        0600, so without the explicit chmod an overwritten sidecar would
        end up more restricted than a freshly written one - same feature,
        two different permissions depending on whether the file happened
        to exist."""
        plan = self._planned()
        sidecar.write(plan.writes)
        fresh = (self.root / "cat.jpg.txt").stat().st_mode & 0o777

        sidecar.write(plan.writes, overwrite=True)
        replaced = (self.root / "cat.jpg.txt").stat().st_mode & 0o777

        self.assertEqual(fresh, replaced)

    def test_one_unwritable_target_does_not_abandon_the_rest(self):
        """A single read-only folder in a batch of thousands would
        otherwise lose every sidecar after it."""
        good = self.root / "good"
        good.mkdir()
        (good / "a.jpg").write_bytes(b"x")
        entries = [
            _entry(str(self.root / "nonexistent-folder" / "b.jpg"), [_tag("t")]),
            _entry(str(good / "a.jpg"), [_tag("t")]),
        ]
        plan = sidecar.plan(entries)

        result = sidecar.write(plan.writes)

        self.assertEqual(result.written, [good / "a.jpg.txt"])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("b.jpg", str(result.failures[0][0]))


if __name__ == "__main__":
    unittest.main()
