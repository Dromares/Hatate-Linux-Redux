"""The tag-source filter, and what it is allowed to reach (DAN-72).

Before this, the tag-source checkboxes in Settings > General filtered
exactly one thing - the tag list widget - while every Hydrus send and
every export carried the full unfiltered set, and the README said
otherwise. Two separate things have to hold now, and both are
regressions against that build:

* with the new switch OFF (the default, and what every existing config
  migrates to), a send and an export still carry every tag. A build
  that "fixed" this by just honouring the existing checkboxes would
  silently change what the next send puts in someone's library, which
  is the thing DAN-47 says never to do.
* with it ON, all four outbound paths - the three sends and the export
  - drop the unticked sources. On the parent commit every one of these
  fails, because nothing but the widget ever looked at the setting.
"""
import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

from PIL import Image

from core.config import Settings
from core.models import ImageEntry, Tag, TagSource, tags_to_send


def _entry() -> ImageEntry:
    """One entry carrying a tag from every source, so a filter that drops
    the wrong one is visible rather than merely absent."""
    e = ImageEntry(path=os.path.join(tempfile.gettempdir(), "a.jpg"))
    e.matched_url = "https://danbooru.donmai.us/posts/1"
    e.tags = [
        Tag(name="mine", source=TagSource.USER),
        Tag(name="from_hydrus", source=TagSource.HYDRUS),
        Tag(name="from_engine", source=TagSource.SEARCH_ENGINE),
        Tag(name="from_booru", source=TagSource.BOORU),
        Tag(name="auto", source=TagSource.HATATE),
    ]
    return e


def _settings(filter_on: bool, sources=None) -> Settings:
    s = Settings()
    s.filter_sent_tags_by_source = filter_on
    if sources is not None:
        s.enabled_tag_sources = list(sources)
    return s


# A real decodable JPEG: download_and_send's validation (BA-02) rejects
# anything that doesn't open, so a placeholder byte string never reaches
# the add_tags call this test is about.
_jpeg = io.BytesIO()
Image.new("RGB", (100, 100), color="red").save(_jpeg, format="JPEG")
VALID_JPEG = _jpeg.getvalue()

ALL_TAGS = ["mine", "from_hydrus", "from_engine", "from_booru", "auto"]
# The stock enabled_tag_sources: everything but "Search engine".
DEFAULT_VISIBLE = ["mine", "from_hydrus", "from_booru", "auto"]


class TestTheHelper(unittest.TestCase):
    def test_no_settings_at_all_means_no_filtering(self):
        """Several callers have no Settings to hand. They must get the
        old behaviour rather than an empty tag list."""
        self.assertEqual([t.name for t in tags_to_send(_entry(), None)], ALL_TAGS)

    def test_off_sends_everything_even_when_sources_are_unticked(self):
        s = _settings(False, ["User"])
        self.assertEqual([t.name for t in tags_to_send(_entry(), s)], ALL_TAGS)

    def test_on_honours_the_ticked_sources(self):
        s = _settings(True)
        self.assertEqual([t.name for t in tags_to_send(_entry(), s)], DEFAULT_VISIBLE)

    def test_on_with_nothing_ticked_sends_nothing(self):
        """Unlike the tag list widget, which falls back to showing all
        tags when the list is empty. A send is deliberate and this
        combination has one honest reading - see the helper's docstring."""
        self.assertEqual(tags_to_send(_entry(), _settings(True, [])), [])

    def test_a_stale_source_name_is_ignored_not_fatal(self):
        """enabled_tag_sources is a list of strings on disk that nothing
        validates on load."""
        s = _settings(True, ["User", "Gelbooru-2009"])
        self.assertEqual([t.name for t in tags_to_send(_entry(), s)], ["mine"])

    def test_the_returned_list_is_not_the_entrys_own(self):
        """Callers build tag_names from it; nothing should be able to
        mutate the entry through the result."""
        e = _entry()
        result = tags_to_send(e, None)
        result.clear()
        self.assertEqual(len(e.tags), 5)


class TestTheThreeSends(unittest.TestCase):
    """core/hydrus_import.py's three outbound paths."""

    def _upload(self, settings):
        from core.hydrus_import import send_file_upload
        client = MagicMock()
        client.import_file.return_value = {"hash": "abc123", "status": 1}
        send_file_upload(_entry(), client, settings)
        self.assertTrue(client.add_tags.called, "no tags were sent at all")
        return list(client.add_tags.call_args[0][1])

    def _url_importer(self, settings):
        from core.hydrus_import import send_url_to_importer
        client = MagicMock()
        client.get_url_info.return_value = {"url_type": 0, "match_name": "danbooru"}
        client.import_url.return_value = {"human_result_text": "queued"}
        send_url_to_importer(_entry(), client, settings)
        self.assertTrue(client.import_url.called, "the URL was never sent")
        return list(client.import_url.call_args[1]["tags"])

    def _download_and_send(self, settings):
        """The download-then-upload path. Needs a genuinely decodable
        image (the DAN-17/BA-02 validation rejects anything else) and a
        client that reports the hash of what it was actually handed."""
        from unittest.mock import patch
        import core.hydrus_import as hi
        from core.hydrus_tag_lookup import hash_file
        from core.models import MatchCandidate

        e = _entry()
        e.candidates = [MatchCandidate(
            url=e.matched_url, source_name="danbooru", similarity=95.0,
            direct_file_url="https://example.invalid/a.jpg",
        )]
        e.select_candidate(0)
        # select_candidate swaps in the candidate's own engine/booru tags,
        # which for this bare candidate means clearing ours. Put them back.
        e.tags = _entry().tags

        client = MagicMock()
        client.import_file.side_effect = lambda path: {"hash": hash_file(path),
                                                       "status": "success"}
        tmp = tempfile.mkdtemp(prefix="hatate-dan72-send-")
        with patch("core.remote.download_bytes", return_value=VALID_JPEG):
            result = hi.download_and_send(e, client, 30.0, tmp, settings)
        self.assertTrue(result.success,
                        f"download_and_send failed: {result.error or result.skipped_reason}")
        self.assertTrue(client.add_tags.called, "no tags were sent at all")
        return list(client.add_tags.call_args[0][1])

    SENDS = ("_upload", "_url_importer", "_download_and_send")

    def test_off_by_default_every_send_still_carries_every_tag(self):
        for name in self.SENDS:
            with self.subTest(send=name):
                sent = getattr(self, name)(Settings())
                self.assertCountEqual(sent, ALL_TAGS)

    def test_off_ignores_unticked_sources(self):
        """The whole point of the default: someone who unticked "Search
        engine" years ago to tidy the panel must not find their next send
        suddenly missing those tags."""
        s = _settings(False, ["User"])
        for name in self.SENDS:
            with self.subTest(send=name):
                self.assertCountEqual(getattr(self, name)(s), ALL_TAGS)

    def test_on_every_send_drops_the_unticked_sources(self):
        s = _settings(True)
        for name in self.SENDS:
            with self.subTest(send=name):
                sent = getattr(self, name)(s)
                self.assertCountEqual(sent, DEFAULT_VISIBLE)
                self.assertNotIn("from_engine", sent)

    def test_no_settings_argument_still_sends_everything(self):
        """`settings` is optional on all three; omitting it is the old
        signature and must mean the old behaviour."""
        for name in self.SENDS:
            with self.subTest(send=name):
                self.assertCountEqual(getattr(self, name)(None), ALL_TAGS)


class TestTheExport(unittest.TestCase):
    def _csv_tags(self, settings):
        from core import export
        rows = export.to_csv([_entry()], settings).splitlines()
        header = rows[0].split(",")
        # The tags cell is quoted because it contains ", " separators.
        import csv
        row = next(csv.reader(rows[1:]))
        return row[header.index("tags")].split(export.CSV_TAG_SEPARATOR), row

    def test_off_by_default_the_export_carries_every_tag(self):
        from core import export
        tags, _row = self._csv_tags(Settings())
        self.assertCountEqual(tags, ALL_TAGS)
        record = json.loads(export.to_json([_entry()]))["images"][0]
        self.assertCountEqual([t["name"] for t in record["tags"]], ALL_TAGS)

    def test_on_the_export_drops_the_unticked_sources(self):
        from core import export
        tags, _row = self._csv_tags(_settings(True))
        self.assertCountEqual(tags, DEFAULT_VISIBLE)
        record = json.loads(
            export.to_json([_entry()], settings=_settings(True))
        )["images"][0]
        self.assertCountEqual([t["name"] for t in record["tags"]], DEFAULT_VISIBLE)
        self.assertNotIn("from_engine", [t["name"] for t in record["tags"]])

    def test_tag_count_matches_the_tags_actually_written(self):
        """A count that disagrees with the column next to it is worse
        than either number on its own."""
        from core import export
        for settings in (Settings(), _settings(True), _settings(True, [])):
            with self.subTest(filter_on=settings.filter_sent_tags_by_source,
                              sources=settings.enabled_tag_sources):
                record = json.loads(
                    export.to_json([_entry()], settings=settings))["images"][0]
                self.assertEqual(record["tag_count"], len(record["tags"]))
                tags, row = self._csv_tags(settings)
                header = export.HEADERS
                written = [t for t in tags if t]
                self.assertEqual(row[header.index("tag_count")], str(len(written)))

    def test_render_passes_the_settings_through(self):
        from core import export
        for fmt in ("csv", "json"):
            with self.subTest(fmt=fmt):
                out = export.render([_entry()], fmt, _settings(True))
                self.assertNotIn("from_engine", out)
                self.assertIn("from_booru", out)

    def test_the_column_order_is_unchanged(self):
        """_row_values replaced positional extraction with a dict keyed
        by field name; the CSV must still come out in HEADERS order."""
        from core import export
        rows = export.to_csv([_entry()]).splitlines()
        self.assertEqual(tuple(rows[0].split(",")), export.HEADERS)


class TestTheMigration(unittest.TestCase):
    """The part DAN-72 calls the real work: an existing config must come
    out of this upgrade sending exactly what it sent yesterday."""

    def _load(self, stored: dict) -> Settings:
        import importlib
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-dan72-cfg-")
        import core.paths
        import core.config
        importlib.reload(core.paths)
        importlib.reload(core.config)
        core.config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        core.config.CONFIG_FILE.write_text(json.dumps(stored), encoding="utf-8")
        return core.config.Settings.load()

    def test_a_v12_config_with_sources_unticked_does_not_start_filtering(self):
        loaded = self._load({
            "schema_version": 12,
            "enabled_tag_sources": ["User"],
        })
        self.assertFalse(loaded.filter_sent_tags_by_source)
        self.assertEqual(loaded.enabled_tag_sources, ["User"],
                         "the migration must not rewrite what the user ticked")
        self.assertCountEqual([t.name for t in tags_to_send(_entry(), loaded)], ALL_TAGS)

    def test_a_pre_schema_config_is_treated_the_same(self):
        """No schema_version at all reads as version 1."""
        loaded = self._load({"enabled_tag_sources": ["User", "Booru"]})
        self.assertFalse(loaded.filter_sent_tags_by_source)

    def test_an_explicit_opt_in_survives_a_reload(self):
        """Someone who has already turned it on must not have it turned
        back off by the migration on the next launch."""
        import core.config
        loaded = self._load({
            "schema_version": core.config.CURRENT_SCHEMA_VERSION,
            "filter_sent_tags_by_source": True,
        })
        self.assertTrue(loaded.filter_sent_tags_by_source)

    def test_the_schema_version_was_bumped_for_this(self):
        import core.config
        self.assertGreaterEqual(core.config.CURRENT_SCHEMA_VERSION, 13)

    def test_a_fresh_install_defaults_to_off(self):
        self.assertFalse(Settings().filter_sent_tags_by_source)


class TestTheDialogCopy(unittest.TestCase):
    """DAN-47's shape: the dialog must say which of the two things the
    list does, and offer the other as a separate switch."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, settings):
        from gui.settings_dialog import SettingsDialog
        return SettingsDialog(settings)

    def test_the_two_meanings_are_worded_separately(self):
        from PyQt6.QtWidgets import QGroupBox, QLabel
        dialog = self._dialog(Settings())
        try:
            # DAN-521 turned this sentence into the General tab's "Tag
            # sources..." group box title rather than a standalone QLabel -
            # same wording, same distinct-from-the-switch meaning, just
            # read off QGroupBox.title() now instead of QLabel.text().
            names = [w.text() for w in dialog.findChildren(QLabel)]
            names += [w.title() for w in dialog.findChildren(QGroupBox)]
            self.assertIn("Tag sources shown in the tags list", names)
            send_copy = dialog.filter_sent_tags.text().lower()
            self.assertIn("sent to hydrus", send_copy)
            self.assertIn("export", send_copy)
        finally:
            dialog.deleteLater()

    def test_the_switch_round_trips_through_apply_to_settings(self):
        """A widget nobody reads back is the failure mode this repo's GUI
        harness exists for."""
        settings = _settings(True)
        dialog = self._dialog(settings)
        try:
            self.assertTrue(dialog.filter_sent_tags.isChecked())
            dialog.filter_sent_tags.setChecked(False)
            dialog.apply_to_settings()
            self.assertFalse(settings.filter_sent_tags_by_source)
            dialog.filter_sent_tags.setChecked(True)
            dialog.apply_to_settings()
            self.assertTrue(settings.filter_sent_tags_by_source)
        finally:
            dialog.deleteLater()

    def test_an_unchecked_switch_shows_as_unchecked(self):
        dialog = self._dialog(_settings(False))
        try:
            self.assertFalse(dialog.filter_sent_tags.isChecked())
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
