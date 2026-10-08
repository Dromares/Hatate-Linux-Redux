"""Session persistence.

The stored hash is the point of this feature: it's what lets a restart
skip re-hashing files the app has already seen, which for a large batch
on a network share is minutes of work.
"""
import importlib
import json
import os
import tempfile
import unittest

from . import _path  # noqa: F401


class TestSessionRoundTrip(unittest.TestCase):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-session-tests-")
        import core.paths, core.search_cache, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.search_cache)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        self.session = core.session
        self.paths = core.paths

    def _entry(self):
        from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
        e = ImageEntry(path="/mnt/smb/Photos/f1a/abc.png")
        e.hydrus_hash = "1a00af9d7f3bf4082923b402f671aa85"
        e.local_width, e.local_height = 2894, 4093
        e.status = MatchStatus.GOOD
        e.result_source = "fresh"
        e.candidates = [
            MatchCandidate(url="https://danbooru.donmai.us/posts/1", source_name="Danbooru",
                           similarity=95.0, engine="IQDB",
                           direct_file_url="https://cdn.donmai.us/o.jpg"),
            MatchCandidate(url="https://www.pixiv.net/artworks/2", source_name="Pixiv",
                           similarity=90.0, engine="SauceNAO"),
        ]
        e.select_candidate(1)
        e.add_tags([Tag("1girl", TagSource.BOORU),
                    Tag("someone", TagSource.BOORU, "creator"),
                    Tag("mine", TagSource.USER),
                    Tag("fromhydrus", TagSource.HYDRUS)])
        return e

    def test_no_saved_session_is_empty(self):
        self.assertEqual(self.session.load_session(), [])

    def test_the_reviewed_mark_survives_a_real_save_and_load(self):
        """A review pass over a large library spans days and several
        restarts. A mark forgotten on exit puts the row straight back
        into "needs review", which is the state it was added to fix."""
        e = self._entry()
        e.reviewed = True
        self.session.save_session([e])
        restored = self.session.load_session()[0]
        self.assertTrue(restored.reviewed)
        self.assertFalse(restored.needs_review)

    def test_an_unreviewed_entry_stays_unreviewed(self):
        self.session.save_session([self._entry()])
        self.assertFalse(self.session.load_session()[0].reviewed)

    def test_the_upscale_verdict_survives_a_real_save_and_load(self):
        """The check is opt-in and can take a while over a large batch, so
        the point of persisting it is not re-running it every session."""
        e = self._entry()
        e.upscale_verdict = "flagged"
        e.upscale_check_detail = "⚠ source is a different size"
        self.session.save_session([e])
        restored = self.session.load_session()[0]
        self.assertEqual(restored.upscale_verdict, "flagged")
        self.assertEqual(restored.upscale_check_detail, "⚠ source is a different size")

    def test_a_session_written_before_the_upscale_verdict_existed_loads_as_not_checked(self):
        """REGRESSION GUARD: a dict from a build that predates this field
        simply lacks the key, and that has to read as never-checked
        rather than raising or defaulting to some other verdict."""
        entry = self.session._entry_from_dict({"path": "/tmp/legacy.png"})
        self.assertIsNone(entry.upscale_verdict)
        self.assertIsNone(entry.upscale_check_detail)

    def test_hash_survives(self):
        """The whole point: a restored entry must not need re-hashing."""
        e = self._entry()
        self.session.save_session([e])
        self.assertEqual(self.session.load_session()[0].hydrus_hash, e.hydrus_hash)

    def test_selected_candidate_survives(self):
        """Restoring must not silently reset the user's chosen match."""
        self.session.save_session([self._entry()])
        r = self.session.load_session()[0]
        self.assertEqual(r.selected_candidate_index, 1)
        self.assertEqual(r.matched_url, "https://www.pixiv.net/artworks/2")
        self.assertEqual(r.booru_name, "Pixiv")
        self.assertEqual(r.similarity, 90.0)

    def test_all_tag_sources_survive(self):
        """REGRESSION: tags were restored BEFORE select_candidate(), which
        deliberately replaces BOORU and SEARCH_ENGINE tags - so every
        booru-sourced tag was silently discarded on load."""
        from core.models import TagSource
        self.session.save_session([self._entry()])
        r = self.session.load_session()[0]
        self.assertEqual(len(r.tags), 4)
        self.assertEqual({t.source for t in r.tags},
                         {TagSource.BOORU, TagSource.USER, TagSource.HYDRUS})

    def test_candidate_details_survive(self):
        self.session.save_session([self._entry()])
        r = self.session.load_session()[0]
        self.assertEqual(len(r.candidates), 2)
        self.assertEqual(r.candidates[0].direct_file_url, "https://cdn.donmai.us/o.jpg")

    def test_status_and_dimensions_survive(self):
        from core.models import MatchStatus
        self.session.save_session([self._entry()])
        r = self.session.load_session()[0]
        self.assertEqual(r.status, MatchStatus.GOOD)
        self.assertEqual((r.local_width, r.local_height), (2894, 4093))

    def test_corrupt_file_degrades_gracefully(self):
        """A bad session file must never prevent the app from starting."""
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.paths.SESSION_FILE.write_text("{ not valid json")
        self.assertEqual(self.session.load_session(), [])

    def test_unknown_format_version_ignored(self):
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.paths.SESSION_FILE.write_text(
            json.dumps({"version": 999, "entries": [{"path": "/x"}]}))
        self.assertEqual(self.session.load_session(), [])

    def test_clear_empties_the_saved_session(self):
        self.session.save_session([self._entry()])
        self.assertTrue(self.paths.SESSION_DB.exists())
        self.assertEqual(len(self.session.load_session()), 1)
        self.session.clear_session()
        self.assertEqual(self.session.load_session(), [])

    def test_clear_also_removes_a_legacy_json_session(self):
        """Otherwise it would be migrated straight back in on the next
        launch, which is the opposite of what clearing means."""
        self.session.save_session([self._entry()])
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.paths.SESSION_FILE.write_text(json.dumps({
            "version": self.session.SESSION_FORMAT_VERSION,
            "entries": [{"path": "/tmp/legacy.png"}],
        }))
        self.session.clear_session()
        self.assertFalse(self.paths.SESSION_FILE.exists())
        self.assertEqual(self.session.load_session(), [])

    def _legacy(self, age_days):
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.paths.SESSION_FILE.write_text(json.dumps({
            "version": self.session.SESSION_FORMAT_VERSION, "entries": []}))
        old = __import__("time").time() - age_days * 86400
        os.utime(self.paths.SESSION_FILE, (old, old))

    def test_an_old_migrated_backup_is_removed_once_the_db_loads(self):
        self.session.save_session([self._entry()])
        self._legacy(age_days=self.session.LEGACY_BACKUP_DAYS + 1)
        self.assertEqual(len(self.session.load_session()), 1)
        self.assertFalse(self.paths.SESSION_FILE.exists())

    def test_a_recent_migrated_backup_is_kept(self):
        self.session.save_session([self._entry()])
        self._legacy(age_days=2)
        self.session.load_session()
        self.assertTrue(self.paths.SESSION_FILE.exists())

    def test_the_backup_is_kept_while_the_db_is_empty(self):
        """No working session to fall back on - the backup is all there is."""
        self.session.save_session([self._entry()])
        self.session.clear_session()
        self.session.save_session([])
        self._legacy(age_days=self.session.LEGACY_BACKUP_DAYS + 1)
        self.session.load_session()
        self.assertTrue(self.paths.SESSION_FILE.exists())

    def test_revisions_of_a_loaded_list_make_the_next_save_incremental(self):
        """The first autosave after a launch used to rewrite every row:
        seeded from the loaded list, it rewrites only the rotating
        reconcile slice when nothing has changed."""
        import re
        import core.session_db as db
        from core.models import ImageEntry
        self.session.save_session([ImageEntry(path=f"/tmp/{i}.png") for i in range(40)])
        loaded = self.session.load_session()
        with self.assertLogs("hatate.session_db", level="INFO") as logs:
            ok, _ = db.save_entries(loaded, db.revisions_of(loaded))
        self.assertTrue(ok)
        rewritten = int(re.search(r"\((\d+) rewritten", logs.output[-1]).group(1))
        self.assertLessEqual(rewritten, -(-40 // db.RECONCILE_FRACTION))

    def test_entry_without_path_is_skipped(self):
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.paths.SESSION_FILE.write_text(json.dumps({
            "version": self.session.SESSION_FORMAT_VERSION,
            "entries": [{"path": ""}, {"path": "/tmp/ok.png"}],
        }))
        restored = self.session.load_session()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].path, "/tmp/ok.png")



class TestMatchedUrlLogRotation(unittest.TestCase):
    def test_it_rotates_past_the_cap_and_keeps_a_bounded_number(self):
        from unittest.mock import patch
        from pathlib import Path
        from core import logger
        with tempfile.TemporaryDirectory() as tmp, patch.object(logger, "MAX_BYTES", 100):
            path = Path(tmp) / "matched_urls.log"
            for i in range(40):
                logger.log_matched_url(str(path), f"img{i}.png", "https://example.com/post/1")
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ["matched_urls.log", "matched_urls.log.1",
                                     "matched_urls.log.2", "matched_urls.log.3"])
            self.assertIn("img39.png", path.read_text())
            self.assertLess(path.stat().st_size, 100 + 80)

if __name__ == "__main__":
    unittest.main()


class TestSentToHydrusResetOnFreshSearch(unittest.TestCase):
    """REGRESSION: session persistence saves `sent_to_hydrus`, so an entry
    restored from a previous run came back already flagged as sent - and
    auto-import's first guard skipped it permanently, even after the user
    explicitly re-searched it and got a new match. Before persistence the
    flag reset on every launch, so this only appeared once sessions
    started being saved."""

    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-reimport-")
        import core.paths, core.search_cache, core.search_engine
        importlib.reload(core.paths)
        importlib.reload(core.search_cache)
        importlib.reload(core.search_engine)
        self.se = core.search_engine

    def test_fresh_search_clears_a_stale_sent_flag(self):
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.iqdb import IqdbMatch
        from core.models import ImageEntry

        path = os.path.join(tempfile.mkdtemp(), "x.png")
        with open(path, "wb") as fh:
            fh.write(b"x")

        s = Settings()
        s.retrieve_tags_from_booru = False
        s.secondary_engine_mode = "disabled"
        s.primary_engine = "iqdb"
        s.enable_ascii2d = False
        s.enable_tracemoe = False
        s.enable_iqdb3d = False

        entry = ImageEntry(path=path)
        entry.sent_to_hydrus = True          # as restored from a saved session

        hit = [IqdbMatch(url="https://danbooru.donmai.us/posts/1", thumb_url=None,
                         similarity=97.0, width=800, height=600, source_name="Danbooru",
                         is_best_match=True, unnamespaced_tags=[])]

        with patch("core.iqdb.search", return_value=hit), \
             patch.object(self.se, "_load_local_dimensions", lambda e: None), \
             patch.object(self.se, "download_bytes", return_value=b"x"), \
             patch("requests.Session.head", return_value=MagicMock(
                 status_code=200, url="x", headers={"Content-Type": "image/jpeg"})):
            self.se.search_image(entry, s, use_cache=False)

        self.assertFalse(entry.sent_to_hydrus,
                         "a fresh search must clear the stale flag so auto-import can run")

    def test_fresh_search_also_clears_the_confirmed_flag(self):
        """The confirmation flag drives the Sent column, so leaving it set
        after a re-search would show an image as already in Hydrus when
        its match - and therefore what would be sent - has changed."""
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.iqdb import IqdbMatch
        from core.models import ImageEntry

        path = os.path.join(tempfile.mkdtemp(), "y.png")
        with open(path, "wb") as fh:
            fh.write(b"y")

        s = Settings()
        s.retrieve_tags_from_booru = False
        s.secondary_engine_mode = "disabled"
        s.primary_engine = "iqdb"
        s.enable_ascii2d = False
        s.enable_tracemoe = False
        s.enable_iqdb3d = False

        entry = ImageEntry(path=path)
        entry.sent_to_hydrus = True
        entry.hydrus_import_confirmed = True

        hit = [IqdbMatch(url="https://danbooru.donmai.us/posts/2", thumb_url=None,
                         similarity=97.0, width=800, height=600, source_name="Danbooru",
                         is_best_match=True, unnamespaced_tags=[])]

        with patch("core.iqdb.search", return_value=hit), \
             patch.object(self.se, "_load_local_dimensions", lambda e: None), \
             patch.object(self.se, "download_bytes", return_value=b"x"), \
             patch("requests.Session.head", return_value=MagicMock(
                 status_code=200, url="x", headers={"Content-Type": "image/jpeg"})):
            self.se.search_image(entry, s, use_cache=False)

        self.assertFalse(entry.hydrus_import_confirmed)


class TestNamedSessionFiles(unittest.TestCase):
    """File > Save Session As / Open Session. The key property is that a
    session the user named is THEIRS - the app's own automatic session
    must never write over it, and vice versa."""

    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-named-")
        import core.paths, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        self.paths, self.session = core.paths, core.session
        self.dir = tempfile.mkdtemp()

    def _entries(self, count, sent=False):
        from core.models import ImageEntry, MatchStatus, Tag, TagSource
        out = []
        for i in range(count):
            entry = ImageEntry(path=os.path.join(self.dir, "f%d.jpg" % i))
            entry.hydrus_hash = "ab" * 32
            entry.status = MatchStatus.GOOD
            entry.sent_to_hydrus = sent
            entry.hydrus_import_confirmed = sent
            entry.add_tags([Tag("1girl", TagSource.BOORU)], replace_source=TagSource.BOORU)
            out.append(entry)
        return out

    def test_named_session_round_trips(self):
        path = os.path.join(self.dir, "mine.json")
        self.assertTrue(self.session.save_session(self._entries(3, sent=True), path=path))
        restored = self.session.load_session(path=path)
        self.assertEqual(len(restored), 3)
        self.assertTrue(restored[0].sent_to_hydrus)
        self.assertTrue(restored[0].hydrus_import_confirmed)
        self.assertEqual([t.display for t in restored[0].tags], ["1girl"])

    def test_saving_a_named_session_does_not_touch_the_automatic_one(self):
        path = os.path.join(self.dir, "mine.json")
        self.session.save_session(self._entries(2), path=path)
        self.assertFalse(self.paths.SESSION_FILE.exists())

    def test_automatic_save_does_not_touch_a_named_session(self):
        """The autosave timer writes the automatic session repeatedly; if
        it could reach a named file, a session saved deliberately would be
        silently overwritten minutes later."""
        path = os.path.join(self.dir, "mine.json")
        self.session.save_session(self._entries(3), path=path)
        self.session.save_session(self._entries(1))          # automatic
        self.assertEqual(self.session.session_entry_count(path), 3)

    def test_entry_count_without_loading(self):
        path = os.path.join(self.dir, "mine.json")
        self.session.save_session(self._entries(7), path=path)
        self.assertEqual(self.session.session_entry_count(path), 7)

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(self.session.load_session(path=os.path.join(self.dir, "nope.json")), [])

    def test_malformed_file_is_rejected_not_raised(self):
        path = os.path.join(self.dir, "junk.json")
        with open(path, "w") as fh:
            fh.write("this is not json")
        self.assertEqual(self.session.load_session(path=path), [])
        self.assertIsNone(self.session.session_entry_count(path))

    def test_valid_json_that_is_not_a_session_is_rejected(self):
        """Guards against picking the wrong .json in the file dialog and
        ending up with an empty list and no explanation."""
        path = os.path.join(self.dir, "other.json")
        with open(path, "w") as fh:
            fh.write('{"hello": "world"}')
        self.assertEqual(self.session.load_session(path=path), [])
        self.assertIsNone(self.session.session_entry_count(path))

    def test_temp_file_is_cleaned_up_by_the_atomic_replace(self):
        """The write goes via a .tmp then replaces, so an interrupted save
        can't leave a half-written session - but it also must not leave
        the .tmp lying next to the real file."""
        path = os.path.join(self.dir, "mine.json")
        self.session.save_session(self._entries(2), path=path)
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestRestoredMatchNeedsRefetch(unittest.TestCase):
    """A restored match showed no picture until you switched to another
    site in the dropdown and back - which quietly fetched it as a side
    effect. Thumbnail bytes can't go in a JSON session file, so every
    restored candidate starts with none, and only the dropdown ever
    triggered a fetch."""

    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-refetch-")
        import core.paths, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        self.session = core.session
        self.dir = tempfile.mkdtemp()

    def _entry_with_match(self):
        from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
        path = os.path.join(self.dir, "x.png")
        with open(path, "wb") as fh:
            fh.write(b"local")
        entry = ImageEntry(path=path)
        entry.status = MatchStatus.GOOD
        candidate = MatchCandidate(
            url="https://danbooru.donmai.us/posts/1", engine="SauceNAO", similarity=95.0)
        candidate.thumb_bytes = b"MATCHED-IMAGE-BYTES"
        candidate.booru_tags_fetched = True
        candidate.booru_tags = [Tag("1girl", TagSource.BOORU)]
        entry.candidates = [candidate]
        entry.select_candidate(0)
        return entry

    @staticmethod
    def _needs_fetch(candidate):
        """The guard _ensure_candidate_loaded uses."""
        return not (candidate.thumb_bytes is not None and candidate.booru_tags_fetched)

    def test_restored_candidate_has_no_image_bytes(self):
        """The root cause, pinned: bytes can't be serialised to JSON."""
        self.session.save_session([self._entry_with_match()])
        restored = self.session.load_session()[0]
        self.assertIsNone(restored.selected_candidate.thumb_bytes)

    def test_restored_candidate_is_flagged_for_refetch(self):
        """If the restored flags claimed the details were already
        fetched, nothing would ever load them and the picture would stay
        blank however many times you clicked."""
        self.session.save_session([self._entry_with_match()])
        restored = self.session.load_session()[0]
        self.assertTrue(self._needs_fetch(restored.selected_candidate))

    def test_no_refetch_once_loaded(self):
        """Selecting a row repeatedly must not re-download the match."""
        entry = self._entry_with_match()
        self.assertFalse(self._needs_fetch(entry.selected_candidate))

    def test_match_url_and_candidates_survive(self):
        """Only the bytes are lost - everything needed to fetch them
        again is still there."""
        self.session.save_session([self._entry_with_match()])
        restored = self.session.load_session()[0]
        self.assertEqual(restored.selected_candidate.url,
                         "https://danbooru.donmai.us/posts/1")
        self.assertEqual(len(restored.candidates), 1)


class TestMissingFileCheck(unittest.TestCase):
    """A saved session stores paths, and the usual reason one goes stale
    is the file being deleted from Hydrus. The row still restores looking
    entirely normal, so without this check the loss only surfaces later,
    one failure at a time."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _sharded(self, name, create=True):
        """Mimics Hydrus's store layout, which shards files across many
        directories - the reason the check lists directories rather than
        stat'ing files."""
        shard = os.path.join(self.dir, "f%02x" % (abs(hash(name)) % 4))
        os.makedirs(shard, exist_ok=True)
        path = os.path.join(shard, name)
        if create:
            with open(path, "wb") as fh:
                fh.write(b"x")
        return path

    def test_reports_only_the_deleted_files(self):
        from core.missing_files import find_missing_paths
        present = [self._sharded("a.jpg"), self._sharded("b.jpg")]
        deleted = [self._sharded("gone1.jpg", create=False),
                   self._sharded("gone2.jpg", create=False)]
        self.assertEqual(find_missing_paths(present + deleted), set(deleted))

    def test_present_files_are_never_reported(self):
        from core.missing_files import find_missing_paths
        present = [self._sharded("a.jpg"), self._sharded("b.jpg")]
        self.assertEqual(find_missing_paths(present), set())

    def test_unreadable_directory_means_all_its_files(self):
        """An unmounted share or removed folder - every file under it is
        unreachable, which is what the caller needs to know."""
        import shutil
        from core.missing_files import find_missing_paths
        sub = os.path.join(self.dir, "vanishing")
        os.makedirs(sub)
        paths = [os.path.join(sub, "g%d.jpg" % i) for i in range(3)]
        for path in paths:
            with open(path, "wb") as fh:
                fh.write(b"x")
        shutil.rmtree(sub)
        self.assertEqual(find_missing_paths(paths), set(paths))

    def test_groups_by_directory_to_cut_round_trips(self):
        """The check lists each directory once instead of stat'ing every
        file. Locally that's irrelevant; over SMB, where each request
        pays network latency, it's the whole cost of the feature."""
        from core.missing_files import group_by_directory
        paths = [self._sharded("f%d.jpg" % i) for i in range(20)]
        grouped = group_by_directory(paths)
        self.assertLess(len(grouped), len(paths))
        self.assertEqual(sum(len(g) for g in grouped.values()), len(paths))

    def test_empty_input(self):
        from core.missing_files import find_missing_paths
        self.assertEqual(find_missing_paths([]), set())

    def test_stop_check_abandons_the_scan(self):
        from core.missing_files import find_missing_paths
        paths = [self._sharded("f%d.jpg" % i, create=False) for i in range(20)]
        calls = []

        def stop():
            calls.append(1)
            return True

        find_missing_paths(paths, stop_check=stop)
        self.assertEqual(len(calls), 1, "must bail on the first check, not scan everything first")

    def test_flag_is_not_persisted(self):
        """REGRESSION GUARD: file_missing describes the disk right now.
        Persisting it would leave a file restored from Hydrus's trash
        marked missing forever, since nothing would re-derive it."""
        import importlib
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-missing-")
        import core.paths, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        from core.models import ImageEntry

        entry = ImageEntry(path=os.path.join(self.dir, "x.jpg"))
        entry.file_missing = True
        core.session.save_session([entry])

        # Read the stored row back out of the database rather than a JSON
        # file: the automatic session is SQLite now, though each row still
        # holds the same per-entry document it always did.
        import sqlite3
        conn = sqlite3.connect(core.paths.SESSION_DB)
        try:
            (blob,) = conn.execute("SELECT data FROM entries").fetchone()
        finally:
            conn.close()
        self.assertNotIn("file_missing", json.loads(blob))
        self.assertFalse(core.session.load_session()[0].file_missing)


class TestAutosaveSettings(unittest.TestCase):
    def test_defaults(self):
        from core.config import Settings
        s = Settings()
        self.assertTrue(s.autosave_session)
        self.assertEqual(s.autosave_interval_seconds, 120)

    def test_round_trips(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-autosave-")
        import core.paths, core.config
        importlib.reload(core.paths)
        importlib.reload(core.config)
        s = core.config.Settings()
        s.autosave_session = False
        s.autosave_interval_seconds = 45
        s.save()
        loaded = core.config.Settings.load()
        self.assertFalse(loaded.autosave_session)
        self.assertEqual(loaded.autosave_interval_seconds, 45)


class TestHydrusSentState(unittest.TestCase):
    """The Sent column distinguishes three states, and the difference
    matters: the URL-importer path only means Hydrus ACCEPTED the URL for
    its downloader, not that the file arrived."""

    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-sent-")
        import core.paths, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        self.session = core.session

    def _entry(self, name):
        from core.models import ImageEntry
        return ImageEntry(path=os.path.join(tempfile.mkdtemp(), name))

    def test_upload_path_is_confirmed(self):
        from unittest.mock import MagicMock
        from core.hydrus_import import send_file_upload
        client = MagicMock()
        client.import_file.return_value = {"hash": "abc123", "status": 1}
        e = self._entry("a.jpg")
        e.matched_url = "https://danbooru.donmai.us/posts/1"
        send_file_upload(e, client)
        self.assertTrue(e.sent_to_hydrus)
        self.assertTrue(e.hydrus_import_confirmed)

    def test_url_importer_is_sent_but_not_confirmed(self):
        from unittest.mock import MagicMock
        from core.hydrus_import import send_url_to_importer
        client = MagicMock()
        client.import_url.return_value = {"human_result_text": "queued"}
        e = self._entry("b.jpg")
        e.matched_url = "https://danbooru.donmai.us/posts/2"
        send_url_to_importer(e, client)
        self.assertTrue(e.sent_to_hydrus)
        self.assertFalse(e.hydrus_import_confirmed,
                         "Hydrus only accepted the URL - the download happens asynchronously")

    def test_having_a_hash_does_not_imply_sent(self):
        """REGRESSION GUARD: hydrus_hash is just the file's SHA256, which
        the app computes for EVERY added file during duplicate detection.
        Deriving "sent" from it would mark the entire list as sent."""
        e = self._entry("c.jpg")
        e.hydrus_hash = "deadbeef" * 8
        self.assertFalse(e.sent_to_hydrus)
        self.assertFalse(e.hydrus_import_confirmed)

    def test_sent_state_survives_a_restart(self):
        confirmed = self._entry("d.jpg")
        confirmed.sent_to_hydrus = True
        confirmed.hydrus_import_confirmed = True
        queued = self._entry("e.jpg")
        queued.sent_to_hydrus = True
        unsent = self._entry("f.jpg")

        self.session.save_session([confirmed, queued, unsent])
        restored = self.session.load_session()
        self.assertEqual(
            [(e.sent_to_hydrus, e.hydrus_import_confirmed) for e in restored],
            [(True, True), (True, False), (False, False)])


class TestSessionWriteDurability(unittest.TestCase):
    """The session file is the crash-recovery record, so the write has to
    survive being interrupted - and has to cope with two writers, since a
    background autosave can still be running when quitting forces a
    synchronous save."""

    def setUp(self):
        from core import session
        self.session = session
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, "session.json")

    def _payload(self, n=5):
        from core.models import ImageEntry
        return self.session.build_session_payload(
            [ImageEntry(path=f"/x/{i}.png") for i in range(n)])

    def test_write_is_atomic_and_leaves_no_scratch_files(self):
        self.assertTrue(self.session.write_session_payload(self._payload(), self.target))
        with open(self.target, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["entries"]), 5)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_concurrent_writers_do_not_share_a_scratch_path(self):
        """REGRESSION: a fixed ".tmp" name meant a background autosave and
        a synchronous save could write over each other's scratch file and
        rename the wrong bytes into place."""
        import threading
        results = []
        payloads = [self._payload(3), self._payload(9)]

        def write(p):
            results.append(self.session.write_session_payload(p, self.target))

        threads = [threading.Thread(target=write, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(results), "both writes should succeed")
        # Whichever landed last, the file must be ONE of them intact -
        # never a mixture, and never truncated.
        with open(self.target, encoding="utf-8") as fh:
            self.assertIn(len(json.load(fh)["entries"]), (3, 9))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_a_failed_write_cleans_up_after_itself(self):
        from unittest.mock import patch
        with patch("core.session.json.dumps", side_effect=ValueError("nope")):
            self.assertFalse(self.session.write_session_payload(self._payload(), self.target))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])

    def test_serializing_while_another_thread_mutates_does_not_raise(self):
        """An autosave runs while search workers are still filling entries
        in. It may capture a moment mid-update - that is inherent to a
        periodic snapshot, and a short candidate list is clamped on load -
        but it must never throw, because an exception escaping the Qt slot
        that triggered it would abort the process."""
        import threading
        from core.models import ImageEntry, MatchCandidate
        entries = []
        for i in range(100):
            entry = ImageEntry(path=f"/x/{i}.png")
            entry.candidates = [MatchCandidate(url=f"u{j}", similarity=9.0, engine="iqdb")
                                for j in range(10)]
            entries.append(entry)

        stop = threading.Event()

        def churn():
            while not stop.is_set():
                for entry in entries:
                    entry.candidates = []           # what search_image does
                    for j in range(10):
                        entry.candidates.append(
                            MatchCandidate(url=f"u{j}", similarity=9.0, engine="iqdb"))

        worker = threading.Thread(target=churn, daemon=True)
        worker.start()
        try:
            for _ in range(30):
                self.session.build_session_payload(entries)   # must not raise
        finally:
            stop.set()
            worker.join(timeout=5)

    def test_a_short_candidate_list_is_clamped_on_load(self):
        """The safety net for a snapshot taken mid-update: the stored
        selected index must never point past the stored candidates."""
        from core.models import ImageEntry, MatchCandidate
        entry = ImageEntry(path="/x/a.png")
        entry.candidates = [MatchCandidate(url="u0", similarity=9.0, engine="iqdb")]
        entry.selected_candidate_index = 0
        payload = self.session.build_session_payload([entry])
        payload["entries"][0]["selected_candidate_index"] = 7   # as if truncated
        self.session.write_session_payload(payload, self.target)
        restored = self.session.load_session(self.target)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].selected_candidate_index, 0)


class TestSessionDatabase(unittest.TestCase):
    """The automatic session is SQLite so a save can rewrite only what
    changed. At 29,000 entries a full write re-encodes ~50MB to record one
    new match, and that grows with how much of the library is searched."""

    def setUp(self):
        from core import session_db
        self.db_module = session_db
        self.db = os.path.join(tempfile.mkdtemp(prefix="hatate-db-"), "session.db")

    def _entries(self, n=6):
        from core.models import ImageEntry
        return [ImageEntry(path=f"/x/{i:03d}.png") for i in range(n)]

    def _stored(self, path):
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute("SELECT data FROM entries WHERE path = ?", (path,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def _tamper(self, path, marker="TAMPERED"):
        """Writes a recognisable value straight into a row, so a later save
        reveals whether it rewrote that row or left it alone."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute("UPDATE entries SET data = ? WHERE path = ?", (marker, path))
        finally:
            conn.close()

    def test_round_trip_preserves_entries_and_order(self):
        entries = self._entries()
        ok, revisions = self.db_module.save_entries(entries, None, self.db)
        self.assertTrue(ok)
        self.assertEqual(len(revisions), len(entries))
        restored, skipped = self.db_module.load_entries(self.db)
        self.assertEqual(skipped, 0)
        self.assertEqual([e.path for e in restored], [e.path for e in entries])

    def test_only_changed_rows_are_rewritten(self):
        """The whole point. Proven by tampering with a stored row: if the
        save rewrites it anyway, the tampering disappears."""
        from core.models import MatchStatus
        entries = self._entries()
        ok, revisions = self.db_module.save_entries(entries, None, self.db)

        untouched, changed = entries[1], entries[2]
        self._tamper(untouched.path)
        self._tamper(changed.path)
        changed.status = MatchStatus.GOOD          # bumps its revision

        # pass_number chosen so the reconcile slice covers neither row.
        ok, revisions = self.db_module.save_entries(entries, revisions, self.db, pass_number=5)
        self.assertTrue(ok)
        self.assertEqual(self._stored(untouched.path), "TAMPERED",
                         "an unchanged entry should not have been re-encoded")
        self.assertNotEqual(self._stored(changed.path), "TAMPERED",
                            "a changed entry must be rewritten")

    def test_the_rotating_reconcile_repairs_what_the_counter_missed(self):
        """Mutating a list INSIDE an entry doesn't bump its revision, so a
        save can miss it. Every pass refreshes a slice of the list, which
        bounds how long such a change can go unwritten."""
        entries = self._entries(12)
        ok, revisions = self.db_module.save_entries(entries, None, self.db)
        target = entries[3]
        self._tamper(target.path)

        for pass_number in range(self.db_module.RECONCILE_FRACTION + 1):
            ok, revisions = self.db_module.save_entries(
                entries, revisions, self.db, pass_number=pass_number)
        self.assertNotEqual(self._stored(target.path), "TAMPERED",
                            "a full sweep should have refreshed every row by now")

    def test_removed_entries_are_deleted(self):
        entries = self._entries()
        ok, revisions = self.db_module.save_entries(entries, None, self.db)
        dropped = entries.pop(2)
        ok, revisions = self.db_module.save_entries(entries, revisions, self.db)
        self.assertIsNone(self._stored(dropped.path))
        restored, skipped = self.db_module.load_entries(self.db)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(restored), 5)
        self.assertNotIn(dropped.path, revisions)

    def test_reordering_is_persisted(self):
        entries = self._entries()
        ok, revisions = self.db_module.save_entries(entries, None, self.db)
        entries.reverse()
        ok, revisions = self.db_module.save_entries(entries, revisions, self.db)
        restored, skipped = self.db_module.load_entries(self.db)
        self.assertEqual(skipped, 0)
        self.assertEqual([e.path for e in restored], [e.path for e in entries])

    def test_a_failed_save_does_not_advance_the_revision_map(self):
        """Otherwise the next save would believe rows it never wrote were
        already current, and skip them forever."""
        from unittest.mock import patch
        entries = self._entries()
        ok, revisions = self.db_module.save_entries(entries, None, self.db)
        entries[0].status = entries[0].status      # bump a revision
        with patch.object(self.db_module.json, "dumps", side_effect=ValueError("nope")):
            ok, after = self.db_module.save_entries(entries, revisions, self.db)
        self.assertFalse(ok)
        self.assertEqual(after, revisions)

    def test_an_empty_database_loads_as_an_empty_list(self):
        restored, skipped = self.db_module.load_entries(self.db)
        self.assertEqual(skipped, 0)
        self.assertEqual(restored, [])

    def test_a_corrupt_row_is_skipped_not_fatal(self):
        entries = self._entries(3)
        self.db_module.save_entries(entries, None, self.db)
        self._tamper(entries[1].path, marker="{not valid json")
        restored, skipped = self.db_module.load_entries(self.db)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(restored), 2)


class TestLegacySessionMigration(unittest.TestCase):
    """A session.json written before the switch has to come across once,
    and never be read again afterwards."""

    def setUp(self):
        import importlib
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-migrate-")
        import core.paths, core.session_db, core.session
        importlib.reload(core.paths)
        importlib.reload(core.session_db)
        importlib.reload(core.session)
        self.session = core.session
        self.paths = core.paths

    def _write_legacy(self, count=4):
        from core.models import ImageEntry
        entries = [ImageEntry(path=f"/legacy/{i}.png") for i in range(count)]
        payload = self.session.build_session_payload(entries)
        self.paths.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.session.write_session_payload(payload, self.paths.SESSION_FILE)
        return entries

    def test_a_legacy_file_is_migrated_on_first_load(self):
        self._write_legacy()
        restored = self.session.load_session()
        self.assertEqual(len(restored), 4)
        self.assertTrue(self.paths.SESSION_DB.exists())

    def test_the_old_file_is_kept_as_a_backup_but_not_reread(self):
        self._write_legacy()
        self.session.load_session()
        self.assertTrue(self.paths.SESSION_FILE.exists(), "the backup should survive")
        # Empty the database; the stale JSON must NOT be migrated back in.
        self.session.clear_session()
        self.assertEqual(self.session.load_session(), [])

    def test_the_database_wins_when_both_exist(self):
        from core.models import ImageEntry
        self._write_legacy(4)
        self.session.save_session([ImageEntry(path="/current/only.png")])
        restored = self.session.load_session()
        self.assertEqual([e.path for e in restored], ["/current/only.png"])

    def test_named_sessions_are_still_json(self):
        """They are portable, user-facing files - nothing about them needs
        incremental writes."""
        from core.models import ImageEntry
        named = os.path.join(tempfile.mkdtemp(), "mine.json")
        self.session.save_session([ImageEntry(path="/n/1.png")], named)
        self.assertTrue(os.path.exists(named))
        with open(named, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["entries"]), 1)
        self.assertEqual(len(self.session.load_session(named)), 1)


class TestEntryRevisionTracking(unittest.TestCase):
    """What makes an incremental save possible: an entry knows when it has
    changed, without anything having to re-encode it to find out."""

    def _entry(self):
        from core.models import ImageEntry
        return ImageEntry(path="/x/a.png")

    def test_a_field_write_marks_the_entry_changed(self):
        from core.models import MatchStatus
        entry = self._entry()
        before = entry.revision
        entry.status = MatchStatus.GOOD
        self.assertGreater(entry.revision, before)

    def test_thumbnail_bytes_do_not_mark_it_changed(self):
        """A background worker sets these for every row it decodes. Counting
        them would mark the whole visible list dirty on every scroll, and
        they are not saved anyway."""
        entry = self._entry()
        before = entry.revision
        entry.matched_thumb_bytes = b"\x89PNG"
        self.assertEqual(entry.revision, before)

    def test_the_missing_flag_does_not_mark_it_changed(self):
        """It describes the disk right now and is deliberately not saved."""
        entry = self._entry()
        before = entry.revision
        entry.file_missing = True
        self.assertEqual(entry.revision, before)

    def test_touch_covers_changes_inside_the_entry(self):
        """entry.tags.append() mutates a list the entry points at, which
        __setattr__ cannot see."""
        from core.models import Tag, TagSource
        entry = self._entry()
        before = entry.revision
        entry.tags.append(Tag("1girl", TagSource.BOORU))
        self.assertEqual(entry.revision, before, "the list mutation is genuinely invisible")
        entry.touch()
        self.assertGreater(entry.revision, before)

    def test_revision_is_not_persisted(self):
        """It describes this run - a restored entry starting at some
        arbitrary saved number would be meaningless."""
        from core import session
        entry = self._entry()
        entry.touch()
        payload = session.build_session_payload([entry])
        self.assertNotIn("revision", payload["entries"][0])


class TestRemovedEntriesStayRemoved(unittest.TestCase):
    """REGRESSION: entries removed from the list came back on the next
    start.

    save_entries decided what to delete with
    `[p for p in previous if p not in current]`, and `previous` is {}
    whenever last_revisions is None. A full save passes None - and a full
    save is what runs on quit - so the one write meant to be
    authoritative could only add and update, never delete.

    With remove_after_import on, every auto-imported file was dropped
    from the list and then resurrected on the next launch, at its old
    ordinal, interleaved with the rows that had legitimately stayed. The
    restored list looked like an older one.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="hatate-session-db-"))
        self.db = self.tmp / "session.db"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entries(self, n):
        from core.models import ImageEntry, MatchStatus
        out = []
        for i in range(n):
            e = ImageEntry(path=f"/tmp/sess-{i}.png")
            e.status = MatchStatus.GOOD
            out.append(e)
        return out

    def _saved_paths(self):
        from core import session_db
        entries, _ = session_db.load_entries(self.db)
        return [e.path for e in entries]

    def test_a_full_save_deletes_what_is_no_longer_in_the_list(self):
        """The quit path. This is the one that was broken."""
        from core import session_db
        entries = self._entries(5)
        session_db.save_entries(entries, None, path=self.db)
        kept = [entries[0], entries[3], entries[4]]
        session_db.save_entries(kept, None, path=self.db)
        self.assertEqual(self._saved_paths(),
                         ["/tmp/sess-0.png", "/tmp/sess-3.png", "/tmp/sess-4.png"])

    def test_the_restored_order_has_no_gaps_left_by_removals(self):
        """The resurrected rows kept their old ordinals, so they came back
        interleaved - which is what made the list look shuffled as well as
        wrong."""
        from core import session_db
        entries = self._entries(5)
        session_db.save_entries(entries, None, path=self.db)
        session_db.save_entries([entries[0], entries[3]], None, path=self.db)
        self.assertEqual(self._saved_paths(), ["/tmp/sess-0.png", "/tmp/sess-3.png"])

    def test_an_incremental_save_still_deletes(self):
        """The periodic autosave path, which did work - kept covered so a
        fix to one cannot break the other."""
        from core import session_db
        entries = self._entries(5)
        _, revisions = session_db.save_entries(entries, None, path=self.db)
        session_db.save_entries(entries[:3], revisions, path=self.db)
        self.assertEqual(len(self._saved_paths()), 3)

    def test_a_save_that_removes_nothing_deletes_nothing(self):
        """The common case by far. Deleting here would be far worse than
        the bug being fixed."""
        from core import session_db
        entries = self._entries(5)
        _, revisions = session_db.save_entries(entries, None, path=self.db)
        session_db.save_entries(entries, revisions, path=self.db)
        self.assertEqual(len(self._saved_paths()), 5)
        session_db.save_entries(entries, None, path=self.db)
        self.assertEqual(len(self._saved_paths()), 5)

    def test_removals_survive_a_save_that_knows_nothing_of_them(self):
        """A fresh run has no revision map at all, so the file has to be
        the authority on what is in it."""
        from core import session_db
        entries = self._entries(4)
        session_db.save_entries(entries, None, path=self.db)
        # A later, entirely separate save - no revision map carried over.
        session_db.save_entries(entries[:2], None, path=self.db)
        self.assertEqual(len(self._saved_paths()), 2)

    def test_saving_an_empty_list_empties_the_stored_session(self):
        from core import session_db
        session_db.save_entries(self._entries(3), None, path=self.db)
        session_db.save_entries([], None, path=self.db)
        self.assertEqual(self._saved_paths(), [])

    def test_the_saved_state_of_a_kept_entry_is_preserved(self):
        """Guards against fixing the deletion by rewriting everything and
        losing what the rows held."""
        from core import session_db
        from core.models import MatchCandidate, MatchStatus, Tag, TagSource
        entries = self._entries(3)
        # booru_name/similarity are not stored in their own right - they
        # are derived from the selected candidate when the entry is read
        # back, so the match itself is what has to survive.
        entries[0].candidates = [MatchCandidate(
            url="https://danbooru.donmai.us/posts/1", similarity=93.0,
            source_name="Danbooru", engine="IQDB")]
        entries[0].select_candidate(0)
        entries[0].tags = [Tag("kept", TagSource.USER)]
        session_db.save_entries(entries, None, path=self.db)
        session_db.save_entries(entries[:1], None, path=self.db)
        restored, skipped = session_db.load_entries(self.db)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].status, MatchStatus.GOOD)
        self.assertEqual(len(restored[0].candidates), 1)
        self.assertEqual(restored[0].booru_name, "Danbooru")
        self.assertEqual(restored[0].similarity, 93.0)
        self.assertEqual([t.name for t in restored[0].tags], ["kept"])

    def test_a_removed_entry_that_returns_later_is_stored_once(self):
        """Re-adding a file after removing it must not leave two rows."""
        from core import session_db
        entries = self._entries(3)
        session_db.save_entries(entries, None, path=self.db)
        session_db.save_entries(entries[:2], None, path=self.db)
        session_db.save_entries(entries, None, path=self.db)
        paths = self._saved_paths()
        self.assertEqual(len(paths), 3)
        self.assertEqual(len(set(paths)), 3)
