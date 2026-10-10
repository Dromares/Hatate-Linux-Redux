"""Small self-contained utilities.

Every case here is a bug that actually occurred during development, not a
hypothetical - the comments say which, so a future failure is easy to
interpret.
"""
import time
import sys
import unittest

from . import _path  # noqa: F401  (sys.path side effect)

from core.lru_cache import LRUCache
from core.hard_timeout import HardTimeoutError, run_with_hard_timeout
from core.saucenao import _safe_int


class TestLRUCache(unittest.TestCase):
    def test_subscript_read_works(self):
        """REGRESSION: LRUCache originally implemented __setitem__ but not
        __getitem__, so the thumbnail handler crashed with
        'LRUCache object is not subscriptable' the moment a thumbnail
        finished decoding."""
        c = LRUCache(3)
        c["a"] = 1
        self.assertEqual(c["a"], 1)

    def test_missing_key_raises_keyerror(self):
        c = LRUCache(3)
        with self.assertRaises(KeyError):
            c["nope"]

    def test_evicts_least_recently_used_not_oldest_inserted(self):
        """Must be true LRU, not FIFO - an entry that was read recently
        has to survive eviction."""
        c = LRUCache(3)
        c["a"], c["b"], c["c"] = 1, 2, 3
        c.get("a")          # touch 'a' -> now most recently used
        c["d"] = 4          # should evict 'b', the genuinely least-recent
        self.assertIn("a", c)
        self.assertNotIn("b", c)

    def test_subscript_read_also_refreshes_recency(self):
        c = LRUCache(3)
        c["a"], c["b"], c["c"] = 1, 2, 3
        _ = c["a"]          # touch via subscript rather than .get()
        c["d"] = 4
        self.assertIn("a", c)

    def test_overwrite_does_not_grow_cache(self):
        c = LRUCache(3)
        c["a"], c["b"] = 1, 2
        before = len(c)
        c["a"] = 99
        self.assertEqual(len(c), before)
        self.assertEqual(c.get("a"), 99)

    def test_stays_within_cap(self):
        """The point of the cache: a 1,144-image batch previously held
        every decoded preview at once (~2 GB)."""
        c = LRUCache(40)
        for i in range(1144):
            c[i] = f"pixmap{i}"
        self.assertLessEqual(len(c), 40)
        self.assertIsNotNone(c.get(1143))  # most recent survives


class TestHardTimeout(unittest.TestCase):
    def test_returns_value_when_fast(self):
        self.assertEqual(run_with_hard_timeout(lambda: 42, timeout=2.0), 42)

    def test_propagates_exception(self):
        def boom():
            raise ValueError("boom")
        with self.assertRaises(ValueError):
            run_with_hard_timeout(boom, timeout=2.0)

    def test_enforces_deadline_on_a_hanging_call(self):
        """REGRESSION: requests' own `timeout` only limits time between
        bytes, so a server trickling data kept a 'timed out' search
        running for minutes. This enforces real wall-clock time."""
        start = time.monotonic()
        with self.assertRaises(HardTimeoutError):
            run_with_hard_timeout(lambda: time.sleep(30), timeout=0.4)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_uses_daemon_thread_so_a_hung_call_cannot_block_exit(self):
        """REGRESSION: an earlier ThreadPoolExecutor version registered an
        atexit hook that waited for abandoned work, so a genuinely hung
        request froze application shutdown - the opposite of the fix."""
        import threading
        before = {t.name for t in threading.enumerate()}
        try:
            run_with_hard_timeout(lambda: time.sleep(5), timeout=0.2)
        except HardTimeoutError:
            pass
        new = [t for t in threading.enumerate() if t.name not in before]
        self.assertTrue(all(t.daemon for t in new),
                        "abandoned worker threads must be daemons")


class TestSauceNaoSafeInt(unittest.TestCase):
    def test_coerces_numeric_strings(self):
        """REGRESSION: SauceNAO returned long_limit as the string '5000'
        while long_remaining was the int 4854; subtracting them raised
        TypeError and took down the whole app via a Qt signal handler."""
        self.assertEqual(_safe_int("5000"), 5000)
        self.assertEqual(_safe_int(4854), 4854)

    def test_unparseable_becomes_none_rather_than_raising(self):
        self.assertIsNone(_safe_int("not_a_number"))
        self.assertIsNone(_safe_int(""))
        self.assertIsNone(_safe_int(None))


class TestSauceNaoQuotaExhaustion(unittest.TestCase):
    """Drives the "pause when the daily quota runs out" toggle
    (Settings > SauceNAO). The distinction that matters is daily vs the
    ~30-second burst window - confusing them would halt a whole batch
    over a limit that clears by itself in seconds."""

    def setUp(self):
        import os, tempfile
        from PIL import Image
        from core import saucenao
        self.saucenao = saucenao
        self.path = os.path.join(tempfile.mkdtemp(), "x.jpg")
        Image.new("RGB", (32, 32), "red").save(self.path)
        self._reset()

    def tearDown(self):
        self._reset()

    def _reset(self):
        self.saucenao.reset_daily_limit_flag()
        self.saucenao._last_quota = None

    def _respond(self, header):
        from unittest.mock import MagicMock, patch
        from core.config import SauceNaoSettings
        self._reset()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"header": header, "results": []}
        settings = SauceNaoSettings(api_key="k", use_json_api=True)
        with patch("requests.Session.post", return_value=resp):
            try:
                self.saucenao.search(self.path, settings)
            except self.saucenao.SauceNaoError:
                pass

    def test_quota_remaining_is_not_exhaustion(self):
        self._respond({"status": 0, "short_remaining": 5, "short_limit": 6,
                       "long_remaining": 82, "long_limit": 100})
        self.assertFalse(self.saucenao.is_daily_quota_exhausted())

    def test_short_burst_window_must_not_count_as_exhausted(self):
        """THE case this feature turns on: short_remaining hitting zero is
        the ~30s burst limit, which the app already waits out. Treating it
        as "out of quota" would stop a batch for a few seconds' delay."""
        self._respond({"status": 0, "short_remaining": 0, "short_limit": 6,
                       "long_remaining": 40, "long_limit": 100})
        self.assertFalse(self.saucenao.is_daily_quota_exhausted())

    def test_daily_window_empty_is_exhaustion(self):
        self._respond({"status": 0, "short_remaining": 4, "short_limit": 6,
                       "long_remaining": 0, "long_limit": 100})
        self.assertTrue(self.saucenao.is_daily_quota_exhausted())

    def test_error_message_alone_is_enough(self):
        """An over-quota response doesn't always carry usable numbers."""
        self._respond({"status": -1, "message": "Daily Search Limit Exceeded."})
        self.assertTrue(self.saucenao.is_daily_quota_exhausted())

    def test_unrelated_error_is_not_exhaustion(self):
        """A bad key or a transient failure must not halt the batch."""
        self._respond({"status": -1, "message": "Invalid API key."})
        self.assertFalse(self.saucenao.is_daily_quota_exhausted())

    def test_reset_clears_the_flag(self):
        self._respond({"status": -1, "message": "Daily Search Limit Exceeded."})
        self.assertTrue(self.saucenao.is_daily_quota_exhausted())
        self.saucenao.reset_daily_limit_flag()
        self.assertFalse(self.saucenao.is_daily_quota_exhausted())


class TestQuotaResetEta(unittest.TestCase):
    """DAN-486 gap 2: nothing computed WHEN the daily quota comes back -
    only whether it currently has. SauceNAO resets at 00:00 UTC."""

    def setUp(self):
        from core import saucenao
        self.saucenao = saucenao

    def test_resets_at_the_next_utc_midnight(self):
        import datetime
        # 2026-01-15 13:00 UTC
        now = datetime.datetime(2026, 1, 15, 13, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        reset_at = self.saucenao.daily_quota_reset_at(now)
        self.assertEqual(
            reset_at, datetime.datetime(2026, 1, 16, 0, 0, 0, tzinfo=datetime.timezone.utc))

    def test_seconds_until_reset_matches_the_absolute_time(self):
        import datetime
        now = datetime.datetime(2026, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(
            self.saucenao.seconds_until_daily_quota_resets(now), 3600.0, places=3)

    def test_exactly_at_midnight_is_a_full_day_to_the_next_one(self):
        """Not zero: hitting the boundary exactly means the NEXT reset is
        a full day away, not that a reset is somehow already overdue."""
        import datetime
        now = datetime.datetime(2026, 1, 15, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
        self.assertAlmostEqual(
            self.saucenao.seconds_until_daily_quota_resets(now), 86400.0, places=3)


class TestQuotaPausePersistence(unittest.TestCase):
    """DAN-486 gap 1: a quota pause was a one-time signal that reached
    nobody if they weren't watching the status bar at that exact moment -
    the whole point of an unattended overnight run. These drive the
    persisted state directly, against a throwaway file."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core import saucenao
        self.saucenao = saucenao
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        pause_file = Path(self._tmpdir.name) / "saucenao_quota_pause.json"
        self._patch = patch.object(saucenao, "QUOTA_PAUSE_FILE", pause_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_no_pause_recorded_reads_as_none(self):
        self.assertIsNone(self.saucenao.get_quota_pause_state())

    def test_round_trips_what_was_recorded(self):
        self.saucenao.record_quota_pause(42, 58)
        state = self.saucenao.get_quota_pause_state()
        self.assertEqual((state.searched, state.remaining), (42, 58))

    def test_clearing_removes_it(self):
        self.saucenao.record_quota_pause(1, 1)
        self.saucenao.clear_quota_pause()
        self.assertIsNone(self.saucenao.get_quota_pause_state())

    def test_clearing_when_nothing_was_recorded_does_not_raise(self):
        self.saucenao.clear_quota_pause()

    def test_a_corrupt_file_reads_as_no_pause_rather_than_raising(self):
        """Same defensive shape as session.py's corrupt-file handling -
        a bad file must never block the app from starting."""
        self.saucenao.QUOTA_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.saucenao.QUOTA_PAUSE_FILE.write_text("{ not valid json", encoding="utf-8")
        self.assertIsNone(self.saucenao.get_quota_pause_state())

    def test_recorded_reset_time_survives_the_round_trip(self):
        """describe_quota_pause() and the GUI both parse reset_at back out
        with fromisoformat() - it must still be that shape after a write
        and a read, not just at the moment it was computed."""
        import datetime
        self.saucenao.record_quota_pause(1, 1)
        state = self.saucenao.get_quota_pause_state()
        parsed = datetime.datetime.fromisoformat(state.reset_at)
        self.assertIsNotNone(parsed.tzinfo)


class TestDescribeQuotaPause(unittest.TestCase):
    """The message shown for a persisted pause - on the status bar at
    startup, and in the pause dialog itself."""

    def setUp(self):
        import datetime
        from core.saucenao import QuotaPauseState
        self.QuotaPauseState = QuotaPauseState
        self.now = datetime.datetime(
            2026, 1, 15, 22, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    def test_reports_the_real_known_reset_time_not_a_countdown_alone(self):
        from core.saucenao import describe_quota_pause
        state = self.QuotaPauseState(
            paused_at=self.now, searched=10, remaining=5,
            reset_at="2026-01-16T00:00:00+00:00")
        text = describe_quota_pause(state, now=self.now)
        self.assertIn("00:00 UTC", text)
        self.assertIn("2h00m", text)
        self.assertIn("10 searched", text)
        self.assertIn("5 left unsearched", text)

    def test_an_unparseable_reset_time_still_reports_something_useful(self):
        from core.saucenao import describe_quota_pause
        state = self.QuotaPauseState(
            paused_at=self.now, searched=3, remaining=7, reset_at="not-a-date")
        text = describe_quota_pause(state, now=self.now)
        self.assertIn("3 searched", text)
        self.assertIn("7 left unsearched", text)


class TestHeaderLayoutProvenance(unittest.TestCase):
    """A saved column layout is only safe to apply if it describes the
    same set of columns. Qt applies a mismatched one partially, which
    leaves header labels sitting over the wrong data and columns
    collapsed to zero width - looking like the titles have vanished."""

    @property
    def COLUMN_COUNT(self):
        """However many columns this build actually has.

        Hardcoding 10 here meant the test agreed with itself rather than
        with the table: adding a column would leave it asserting against a
        count nothing else used.
        """
        from gui.image_table_model import COLUMNS
        return len(COLUMNS)

    def _should_restore(self, saved_state, saved_columns):
        """Calls the REAL decision, not a copy of it.

        This used to reimplement the rule under the comment "mirrors
        _restore_table_header_state's decision", so it could pass while
        that decision changed underneath it.
        """
        from gui.image_table_model import header_layout_is_usable
        return header_layout_is_usable(saved_state, saved_columns)

    def test_layout_from_this_build_is_applied(self):
        self.assertTrue(self._should_restore("state", self.COLUMN_COUNT))

    def test_layout_with_an_unrecorded_count_is_discarded(self):
        """REGRESSION: 0 means "saved before the count was recorded",
        which is exactly the untrustworthy case - those layouts came from
        a build with a different set of columns. Treating 0 as an
        exemption is what let a nine-column layout load into a
        ten-column table."""
        self.assertFalse(self._should_restore("state", 0))

    def test_layout_from_a_different_column_count_is_discarded(self):
        """Offsets from the real count rather than fixed numbers, which
        went stale the moment a column was added: 11 was a "wrong" count
        here right up until the Reviewed column made it the right one,
        and the test failed for having been outrun by the table."""
        for delta in (-2, -1, +1, +2):
            count = self.COLUMN_COUNT + delta
            with self.subTest(count=count):
                self.assertFalse(self._should_restore("state", count))

    def test_no_saved_layout_is_not_an_error(self):
        self.assertFalse(self._should_restore("", self.COLUMN_COUNT))

    def test_column_count_setting_round_trips(self):
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-header-")
        import core.paths, core.config
        importlib.reload(core.paths)
        importlib.reload(core.config)
        settings = core.config.Settings()
        self.assertEqual(settings.table_header_columns, 0)
        settings.table_header_columns = 10
        settings.table_header_state = "abc"
        settings.save()
        loaded = core.config.Settings.load()
        self.assertEqual(loaded.table_header_columns, 10)
        self.assertEqual(loaded.table_header_state, "abc")


class TestHeaderRepair(unittest.TestCase):
    """Even an accepted layout can arrive damaged. A section with no
    width shows no title - it only appears when the divider is hovered,
    which reads as a rendering bug rather than a stale setting."""

    MIN_WIDTH = 24
    DEFAULT_WIDTH = 90

    def _repair(self, sizes, hidden=()):
        sizes = list(sizes)
        hidden = set(hidden)
        for index in range(len(sizes)):
            if index in hidden:
                hidden.discard(index)
            if sizes[index] < self.MIN_WIDTH:
                sizes[index] = self.DEFAULT_WIDTH
        return sizes, hidden

    def test_zero_width_columns_are_given_a_usable_width(self):
        sizes, _ = self._repair([60, 0, 0, 90])
        self.assertTrue(all(width >= self.MIN_WIDTH for width in sizes))

    def test_hidden_columns_are_shown_again(self):
        _, hidden = self._repair([60, 90, 90], hidden={1})
        self.assertEqual(hidden, set())

    def test_a_healthy_layout_is_left_alone(self):
        original = [60, 200, 90, 80]
        sizes, hidden = self._repair(original)
        self.assertEqual(sizes, original)
        self.assertEqual(hidden, set())


class TestEngineOverrideSelection(unittest.TestCase):
    """Right-click > Search with a specific engine. The opposite-engine
    action used to refuse outright when a row had no match yet - but
    wanting to run IQDB over some unsearched images is an ordinary thing
    to want, especially once SauceNAO's daily quota is spent."""

    def _engines_for_opposite(self, entries, settings):
        """Calls the REAL implementation, not a copy of it.

        This used to reimplement the rule with the comment "mirrors
        _research_rows_with_opposite_engine's choice of engine" - which
        meant the test could go on passing while the code it described
        changed underneath it. The logic now lives in core/engines.py
        precisely so both callers can use the one copy.
        """
        from core.engines import effective_primary, opposite_engine
        primary = effective_primary(settings.primary_engine)
        chosen = {}
        for entry in entries:
            candidate = entry.selected_candidate
            chosen[entry.path] = opposite_engine(
                candidate.engine if candidate else None, primary)
        return chosen

    def _entry(self, name, engine=None):
        from core.models import ImageEntry, MatchCandidate, MatchStatus
        entry = ImageEntry(path="/tmp/%s" % name)
        if engine:
            entry.candidates = [MatchCandidate(url="https://x/1", engine=engine, similarity=90.0)]
            entry.select_candidate(0)
            entry.status = MatchStatus.GOOD
        return entry

    def test_unsearched_rows_use_the_engine_not_yet_tried(self):
        """REGRESSION: these were refused with "None of the selected
        images have a found match to flip"."""
        from core.config import Settings
        settings = Settings()
        settings.primary_engine = "saucenao"
        chosen = self._engines_for_opposite([self._entry("a.png")], settings)
        self.assertEqual(set(chosen.values()), {"iqdb"})

    def test_fallback_follows_the_configured_primary(self):
        from core.config import Settings
        settings = Settings()
        settings.primary_engine = "iqdb"
        chosen = self._engines_for_opposite([self._entry("a.png")], settings)
        self.assertEqual(set(chosen.values()), {"saucenao"})

    def test_matched_rows_still_flip_per_row(self):
        from core.config import Settings
        settings = Settings()
        settings.primary_engine = "saucenao"
        entries = [self._entry("b.png", engine="SauceNAO"),
                   self._entry("c.png", engine="IQDB")]
        chosen = self._engines_for_opposite(entries, settings)
        self.assertEqual(chosen["/tmp/b.png"], "iqdb")
        self.assertEqual(chosen["/tmp/c.png"], "saucenao")

    def test_mixed_selection_is_handled_row_by_row(self):
        """A selection mixing searched and unsearched rows must work as a
        whole rather than being rejected because of the unsearched ones."""
        from core.config import Settings
        settings = Settings()
        settings.primary_engine = "saucenao"
        entries = [self._entry("a.png"),
                   self._entry("b.png", engine="SauceNAO"),
                   self._entry("c.png", engine="IQDB")]
        chosen = self._engines_for_opposite(entries, settings)
        self.assertEqual(len(chosen), 3)
        self.assertEqual(chosen["/tmp/a.png"], "iqdb")

    def test_the_engine_label_is_matched_however_it_is_written(self):
        """MatchCandidate.engine holds a LABEL ("SauceNAO"), not an id,
        and has been seen with stray whitespace. Comparing it naively
        would flip the wrong way and silently re-run the same engine."""
        from core.engines import IQDB, SAUCENAO, opposite_engine
        for written in ("IQDB", "iqdb", "  IQDB  ", "IqDb"):
            with self.subTest(written=written):
                self.assertEqual(opposite_engine(written, IQDB), SAUCENAO)

    def test_an_unrecognised_primary_does_not_break_the_action(self):
        """A hand-edited or older config must not leave this refusing to
        do anything."""
        from core.engines import IQDB, SAUCENAO, effective_primary, opposite_engine
        self.assertEqual(effective_primary("nonsense"), IQDB)
        self.assertEqual(opposite_engine(None, "nonsense"), SAUCENAO)

    def test_an_unknown_engine_label_flips_to_iqdb(self):
        """A match from ascii2d or trace.moe has no opposite in the
        primary pair, so it goes to IQDB - the one with no daily quota."""
        from core.engines import IQDB, opposite_engine
        self.assertEqual(opposite_engine("ascii2d", IQDB), IQDB)


class TestQuotaPauseGating(unittest.TestCase):
    """The worker only acts on exhaustion when SauceNAO is actually in the
    engine order - extracted from SearchWorker so it can be exercised
    without Qt."""

    def setUp(self):
        import ast
        from core import saucenao
        self.saucenao = saucenao
        src = open("workers/search_worker.py").read()
        cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))
        # Both the decision and the helper it leans on, so the isolated
        # copy behaves like the real method rather than half of it.
        wanted = ("_saucenao_quota_should_pause", "_saucenao_in_use", "_skip_saucenao_now")
        fns = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        ns = {"is_daily_quota_exhausted": saucenao.is_daily_quota_exhausted}
        exec(compile(ast.Module(body=fns, type_ignores=[]), "<x>", "exec"), ns)
        self.should_pause = ns["_saucenao_quota_should_pause"]
        self.should_skip = ns["_skip_saucenao_now"]
        self._helpers = ns

    def _check(self, primary, secondary, toggle, exhausted):
        from core.config import Settings
        s = Settings()
        s.primary_engine = primary
        s.secondary_engine_mode = secondary
        s.saucenao.pause_search_on_quota_exhausted = toggle
        self.saucenao.reset_daily_limit_flag()
        self.saucenao._last_quota = None
        if exhausted:
            self.saucenao._daily_limit_reported = True
        self._worker = type("W", (), {
            "settings": s,
            "_saucenao_in_use": self._helpers["_saucenao_in_use"],
            "_force_continue_without_saucenao": False,
        })()
        try:
            return self.should_pause(self._worker)
        finally:
            self.saucenao.reset_daily_limit_flag()

    def _check_skip(self, primary, secondary, carry_on, exhausted):
        from core.config import Settings
        s = Settings()
        s.primary_engine = primary
        s.secondary_engine_mode = secondary
        s.continue_without_saucenao_on_quota = carry_on
        self.saucenao.reset_daily_limit_flag()
        self.saucenao._last_quota = None
        if exhausted:
            self.saucenao._daily_limit_reported = True
        worker = type("W", (), {
            "settings": s,
            "_saucenao_in_use": self._helpers["_saucenao_in_use"],
            "_force_continue_without_saucenao": False,
        })()
        try:
            return self.should_skip(worker)
        finally:
            self.saucenao.reset_daily_limit_flag()

    def test_carrying_on_is_off_unless_asked_for(self):
        """Stopping stays the default: a result found without SauceNAO is
        weaker, and that has not changed."""
        self.assertFalse(self._check_skip("saucenao", "fallback", False, True))

    def test_carrying_on_skips_saucenao_once_the_day_is_spent(self):
        self.assertTrue(self._check_skip("saucenao", "fallback", True, True))

    def test_carrying_on_changes_nothing_while_quota_remains(self):
        self.assertFalse(self._check_skip("saucenao", "fallback", True, False))

    def test_carrying_on_takes_precedence_over_pausing(self):
        """Both can be ticked. The one that says what to DO wins over the
        one that says to stop."""
        from core.config import Settings
        s = Settings()
        s.primary_engine = "saucenao"
        s.secondary_engine_mode = "fallback"
        s.saucenao.pause_search_on_quota_exhausted = True
        s.continue_without_saucenao_on_quota = True
        self.saucenao.reset_daily_limit_flag()
        self.saucenao._daily_limit_reported = True
        worker = type("W", (), {
            "settings": s,
            "_saucenao_in_use": self._helpers["_saucenao_in_use"],
            "_force_continue_without_saucenao": False,
        })()
        try:
            self.assertFalse(self.should_pause(worker), "it paused instead of carrying on")
            self.assertTrue(self.should_skip(worker))
        finally:
            self.saucenao.reset_daily_limit_flag()

    def test_carrying_on_is_irrelevant_when_saucenao_is_unused(self):
        """Nothing to skip, so nothing is marked provisional."""
        self.assertFalse(self._check_skip("iqdb", "disabled", True, True))

    def test_pauses_when_saucenao_is_primary(self):
        self.assertTrue(self._check("saucenao", "disabled", True, True))

    def test_pauses_when_saucenao_is_secondary(self):
        for mode in ("always", "fallback"):
            with self.subTest(mode=mode):
                self.assertTrue(self._check("iqdb", mode, True, True))

    def test_does_not_pause_when_saucenao_is_unused(self):
        """With SauceNAO out of the engine order its quota is irrelevant,
        and stopping an IQDB-only batch over it would be nonsense."""
        self.assertFalse(self._check("iqdb", "disabled", True, True))

    def test_toggle_off_disables_the_pause(self):
        self.assertFalse(self._check("saucenao", "disabled", False, True))

    def test_no_pause_while_quota_remains(self):
        self.assertFalse(self._check("saucenao", "disabled", True, False))

    def _worker_with_force(self, force: bool):
        from core.config import Settings
        s = Settings()
        s.primary_engine = "saucenao"
        s.secondary_engine_mode = "disabled"
        s.saucenao.pause_search_on_quota_exhausted = True
        s.continue_without_saucenao_on_quota = False  # left off deliberately
        self.saucenao.reset_daily_limit_flag()
        self.saucenao._daily_limit_reported = True
        return type("W", (), {
            "settings": s,
            "_saucenao_in_use": self._helpers["_saucenao_in_use"],
            "_force_continue_without_saucenao": force,
        })()

    def test_the_one_shot_override_stops_the_pause_despite_the_saved_setting(self):
        """DAN-486 gap 3: "Continue with other engines" must behave like
        the saved setting without requiring it to be on."""
        worker = self._worker_with_force(True)
        try:
            self.assertFalse(self.should_pause(worker))
            self.assertTrue(self.should_skip(worker))
        finally:
            self.saucenao.reset_daily_limit_flag()

    def test_without_the_override_the_saved_setting_alone_still_governs(self):
        worker = self._worker_with_force(False)
        try:
            self.assertTrue(self.should_pause(worker))
            self.assertFalse(self.should_skip(worker))
        finally:
            self.saucenao.reset_daily_limit_flag()


class TestFilenameHashes(unittest.TestCase):
    """Hydrus names files in its store after their own SHA256, so a batch
    added from there already carries every hash in its paths - reading
    tens of GB to recompute them is pure waste. The risk is trusting a
    name that lies, so these pin both halves."""

    def setUp(self):
        import hashlib, os, tempfile
        self.d = tempfile.mkdtemp()
        self.hashlib, self.os = hashlib, os

    def _write(self, data, name=None):
        h = self.hashlib.sha256(data).hexdigest()
        path = self.os.path.join(self.d, (name or h) + ".jpg")
        with open(path, "wb") as fh:
            fh.write(data)
        return path, h

    def test_extracts_hash_from_a_hydrus_style_name(self):
        from core.hydrus_tag_lookup import hash_from_filename
        path, h = self._write(b"content")
        self.assertEqual(hash_from_filename(path), h)

    def test_ignores_ordinary_filenames(self):
        from core.hydrus_tag_lookup import hash_from_filename
        for name in ("my_picture", "abc123", "IMG_0042"):
            with self.subTest(name=name):
                path, _ = self._write(b"x", name=name)
                self.assertEqual(hash_from_filename(path), "")

    def test_uppercase_hex_is_not_accepted(self):
        """Hydrus writes lowercase. Accepting uppercase would mean
        emitting a hash that never matches Hydrus's own."""
        from core.hydrus_tag_lookup import hash_from_filename
        path, h = self._write(b"content", name=self.hashlib.sha256(b"content").hexdigest().upper())
        self.assertEqual(hash_from_filename(path), "")

    def test_derived_hash_equals_the_real_one(self):
        from core.hydrus_tag_lookup import hash_file, hash_from_filename
        path, _ = self._write(b"some real bytes here")
        self.assertEqual(hash_from_filename(path), hash_file(path))

    def test_verification_passes_on_a_genuine_store(self):
        from core.hydrus_tag_lookup import verify_filename_hashes
        paths = [self._write(b"file %d" % i)[0] for i in range(10)]
        self.assertTrue(verify_filename_hashes(paths))

    def test_verification_rejects_names_that_lie(self):
        """REGRESSION GUARD: without this check, a directory of files that
        merely LOOK hash-named would have every hash silently wrong - and
        with it every duplicate check and Hydrus tag lookup."""
        from core.hydrus_tag_lookup import verify_filename_hashes
        paths = []
        for i in range(10):
            fake = self.hashlib.sha256(b"name %d" % i).hexdigest()
            path = self.os.path.join(self.d, fake + ".jpg")
            with open(path, "wb") as fh:
                fh.write(b"entirely different content %d" % i)
            paths.append(path)
        self.assertFalse(verify_filename_hashes(paths))

    def test_verification_declines_when_there_is_nothing_to_verify(self):
        from core.hydrus_tag_lookup import verify_filename_hashes
        paths = [self._write(b"x", name="ordinary%d" % i)[0] for i in range(3)]
        self.assertFalse(verify_filename_hashes(paths))


class TestHashSourceSetting(unittest.TestCase):
    """hash_source replaced the older use_filename_hashes bool. The
    migration matters because the wrong mapping would silently flip how
    every future batch is hashed."""

    def setUp(self):
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-hashsrc-")
        import core.paths, core.config
        importlib.reload(core.paths)
        importlib.reload(core.config)
        self.config = core.config

    def _load_v4_config(self, use_filename_hashes):
        import json
        s = self.config.Settings()
        s.save()
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        raw["schema_version"] = 4
        raw.pop("hash_source", None)
        raw["use_filename_hashes"] = use_filename_hashes
        self.config.CONFIG_FILE.write_text(json.dumps(raw))
        return self.config.Settings.load()

    def test_defaults_to_hydrus_hashes(self):
        self.assertEqual(self.config.Settings().hash_source, "hydrus")

    def test_migrates_true_to_hydrus(self):
        self.assertEqual(self._load_v4_config(True).hash_source, "hydrus")

    def test_migrates_false_to_local(self):
        """Someone who deliberately turned filename hashing OFF must not
        have it silently turned back on by the rename."""
        self.assertEqual(self._load_v4_config(False).hash_source, "local")

    def test_migration_is_persisted_not_reapplied(self):
        import json
        self._load_v4_config(False)
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        self.assertEqual(raw["schema_version"], self.config.CURRENT_SCHEMA_VERSION)
        self.assertEqual(raw["hash_source"], "local")

    def test_config_predating_the_setting_entirely(self):
        import json
        s = self.config.Settings()
        s.save()
        raw = json.loads(self.config.CONFIG_FILE.read_text())
        raw["schema_version"] = 3
        raw.pop("hash_source", None)
        raw.pop("use_filename_hashes", None)
        self.config.CONFIG_FILE.write_text(json.dumps(raw))
        self.assertEqual(self.config.Settings.load().hash_source, "hydrus")


class TestHashSourceSplit(unittest.TestCase):
    """The two modes must genuinely differ: "hydrus" skips reading only
    files whose names really are their hashes, "local" reads everything."""

    def setUp(self):
        import ast, hashlib, os, tempfile
        from core.applog import get_logger
        from core.hydrus_tag_lookup import hash_from_filename, verify_filename_hashes
        src = open("workers/file_hash_worker.py").read()
        cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))
        fn = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_split_by_filename_hash")
        ns = {"hash_from_filename": hash_from_filename,
              "verify_filename_hashes": verify_filename_hashes, "log": get_logger("test")}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
        self.split = ns["_split_by_filename_hash"]

        self.d = tempfile.mkdtemp()
        self.paths = []
        for i in range(8):
            data = b"content %d" % i
            h = hashlib.sha256(data).hexdigest()
            path = os.path.join(self.d, h + ".jpg")
            with open(path, "wb") as fh:
                fh.write(data)
            self.paths.append(path)
        for i in range(3):
            path = os.path.join(self.d, "photo%d.jpg" % i)
            with open(path, "wb") as fh:
                fh.write(b"ordinary %d" % i)
            self.paths.append(path)

    def _worker(self, mode):
        import os
        paths = self.paths
        sizes = {p: os.path.getsize(p) for p in paths}
        return type("W", (), {
            "hash_source": mode, "paths": paths,
            "_file_size": lambda self, p: sizes.get(p, 0),
        })()

    def test_hydrus_mode_skips_only_hash_named_files(self):
        from core.hydrus_tag_lookup import hash_file
        from_name, to_read = self.split(self._worker("hydrus"))
        self.assertEqual(len(from_name), 8)
        self.assertEqual(len(to_read), 3)
        for path, derived in from_name.items():
            self.assertEqual(derived, hash_file(path))

    def test_local_mode_reads_everything(self):
        from_name, to_read = self.split(self._worker("local"))
        self.assertEqual(from_name, {})
        self.assertEqual(len(to_read), 11)

    def test_unrecognised_mode_falls_back_to_reading(self):
        """An unexpected value must take the never-wrong path."""
        from_name, to_read = self.split(self._worker("something_else"))
        self.assertEqual(from_name, {})
        self.assertEqual(len(to_read), 11)


class TestViewportRange(unittest.TestCase):
    """Lazy thumbnail loading decides what work to do from this range, so
    a wrong answer here means either blank rows forever or the eager
    behaviour it exists to replace."""

    def _range(self, first, last, count, buf=15):
        from core.viewport import visible_range_with_buffer
        return visible_range_with_buffer(first, last, count, buf)

    def test_expands_by_the_buffer(self):
        self.assertEqual(self._range(100, 119, 1000), (85, 134))

    def test_clamps_at_the_top(self):
        self.assertEqual(self._range(0, 19, 1000), (0, 34))

    def test_clamps_at_the_bottom(self):
        self.assertEqual(self._range(980, 999, 1000), (965, 999))

    def test_negative_last_means_past_the_final_row(self):
        """Qt's rowAt() returns -1 when the y-coordinate falls past the
        last row - routine when the list is shorter than the window or
        scrolled to the bottom. Treating it as an error would leave those
        rows permanently blank."""
        self.assertEqual(self._range(80, -1, 100), (65, 99))

    def test_negative_first_means_top_of_list(self):
        self.assertEqual(self._range(-1, 19, 100), (0, 34))

    def test_both_negative_covers_the_whole_short_list(self):
        self.assertEqual(self._range(-1, -1, 5), (0, 4))

    def test_list_shorter_than_the_viewport(self):
        self.assertEqual(self._range(0, 4, 5), (0, 4))

    def test_single_row(self):
        self.assertEqual(self._range(0, 0, 1), (0, 0))

    def test_empty_list_returns_none(self):
        self.assertIsNone(self._range(0, 0, 0))

    def test_swapped_pair_does_not_produce_an_empty_slice(self):
        """A reversed pair sliced directly would yield nothing, which
        reads as "no thumbnails needed" rather than as the bug it is."""
        self.assertEqual(self._range(50, 30, 1000), (15, 65))

    def test_range_stays_tiny_against_a_huge_list(self):
        start, end = self._range(14000, 14019, 28500)
        self.assertLess(end - start + 1, 60,
                        "a viewport pass must stay proportional to the screen, not the list")


class TestDuplicateFiltering(unittest.TestCase):
    """Which files actually enter the list. Getting this wrong means
    either the same image twice or a file silently refused, and it had no
    test at all while it lived on MainWindow."""

    def _entry(self, path, file_hash=None):
        from core.models import ImageEntry
        e = ImageEntry(path=path)
        e.hydrus_hash = file_hash
        return e

    def _filter(self, paths, hashes=None, existing=()):
        from core.file_intake import filter_duplicate_paths
        return filter_duplicate_paths(paths, hashes or {}, existing)

    def test_a_brand_new_path_is_kept(self):
        unique, dupes = self._filter(["/tmp/a.png"])
        self.assertEqual((unique, dupes), (["/tmp/a.png"], 0))

    def test_the_same_path_already_in_the_list_is_dropped(self):
        unique, dupes = self._filter(["/tmp/a.png"], existing=[self._entry("/tmp/a.png")])
        self.assertEqual((unique, dupes), ([], 1))

    def test_the_path_check_ignores_how_the_path_was_written(self):
        """/tmp/./a.png and /tmp/a.png are the same file."""
        unique, dupes = self._filter(["/tmp/./a.png"], existing=[self._entry("/tmp/a.png")])
        self.assertEqual((unique, dupes), ([], 1))

    def test_the_same_content_under_a_different_name_is_dropped(self):
        """A copy or a re-download - the case that actually happens when
        someone adds a directory tree twice."""
        unique, dupes = self._filter(
            ["/tmp/copy.png"], {"/tmp/copy.png": "abc"},
            existing=[self._entry("/tmp/original.png", "abc")])
        self.assertEqual((unique, dupes), ([], 1))

    def test_different_content_under_a_different_name_is_kept(self):
        unique, _ = self._filter(
            ["/tmp/b.png"], {"/tmp/b.png": "zzz"},
            existing=[self._entry("/tmp/a.png", "abc")])
        self.assertEqual(unique, ["/tmp/b.png"])

    def test_duplicates_inside_one_batch_are_caught(self):
        """A folder holding a file and its own copy adds one, not both."""
        unique, dupes = self._filter(
            ["/tmp/a.png", "/tmp/a-copy.png"],
            {"/tmp/a.png": "same", "/tmp/a-copy.png": "same"})
        self.assertEqual((unique, dupes), (["/tmp/a.png"], 1))

    def test_an_unhashed_path_still_gets_the_path_check(self):
        """A path with no hash must not be refused for want of one, but
        must still be caught if it is literally already there."""
        unique, dupes = self._filter(
            ["/tmp/a.png", "/tmp/b.png"], {}, existing=[self._entry("/tmp/a.png")])
        self.assertEqual((unique, dupes), (["/tmp/b.png"], 1))

    def test_entries_with_no_hash_do_not_block_everything(self):
        """An existing entry whose hash is None must not make every
        unhashed incoming file look like a duplicate of it."""
        unique, dupes = self._filter(
            ["/tmp/new.png"], {}, existing=[self._entry("/tmp/old.png", None)])
        self.assertEqual((unique, dupes), (["/tmp/new.png"], 0))

    def test_nothing_in_is_nothing_out(self):
        self.assertEqual(self._filter([]), ([], 0))


class TestFolderScan(unittest.TestCase):
    def test_it_finds_images_recursively_and_skips_other_files(self):
        import os
        import tempfile
        from core.file_intake import scan_folder
        root = tempfile.mkdtemp(prefix="hatate-scan-")
        os.makedirs(os.path.join(root, "sub"))
        for rel in ("a.png", "sub/b.JPG", "notes.txt", "sub/archive.zip"):
            open(os.path.join(root, rel), "w").close()
        found = sorted(os.path.relpath(p, root) for p in scan_folder(root))
        self.assertEqual(found, ["a.png", os.path.join("sub", "b.JPG")],
                         "extensions are matched case-insensitively, non-images skipped")

    def test_an_empty_folder_yields_nothing(self):
        import tempfile
        from core.file_intake import scan_folder
        self.assertEqual(scan_folder(tempfile.mkdtemp(prefix="hatate-empty-")), [])


class TestThumbnailWorkSelection(unittest.TestCase):
    """Which rows in view still need a thumbnail. The two states that must
    not be confused are "already has one" and "one is being made right
    now" - treating the second as work to do queues a duplicate worker for
    the same entry, once per debounce tick while scrolling."""

    def _entries(self, n):
        from core.models import ImageEntry
        return [ImageEntry(path=f"/tmp/e{i}.png") for i in range(n)]

    def _split(self, entries, cached=(), in_flight=()):
        from core.viewport import entries_needing_thumbnails
        cached_ids = {id(entries[i]) for i in cached}
        flight_ids = {id(entries[i]) for i in in_flight}
        return entries_needing_thumbnails(entries, lambda eid: eid in cached_ids, flight_ids)

    def test_everything_is_needed_when_nothing_is_known(self):
        entries = self._entries(4)
        work = self._split(entries)
        self.assertEqual(work.needed, entries)
        self.assertEqual((work.already_cached, work.in_flight), (0, 0))

    def test_a_cached_row_is_not_regenerated(self):
        entries = self._entries(3)
        work = self._split(entries, cached=[1])
        self.assertNotIn(entries[1], work.needed)
        self.assertEqual(work.already_cached, 1)

    def test_a_row_being_generated_is_not_queued_again(self):
        """The duplicate-worker bug this exists to prevent."""
        entries = self._entries(3)
        work = self._split(entries, in_flight=[2])
        self.assertNotIn(entries[2], work.needed)
        self.assertEqual(work.in_flight, 1)

    def test_cached_wins_over_in_flight(self):
        """A finished generation can leave a stale in-flight marker; the
        thumbnail existing is the more authoritative fact."""
        entries = self._entries(2)
        work = self._split(entries, cached=[0], in_flight=[0])
        self.assertEqual(work.already_cached, 1)
        self.assertEqual(work.in_flight, 0)

    def test_every_row_is_accounted_for_exactly_once(self):
        entries = self._entries(6)
        work = self._split(entries, cached=[0, 1], in_flight=[2])
        self.assertEqual(work.examined, 6)
        self.assertEqual(len(work.needed), 3)

    def test_two_rows_sharing_a_path_are_still_separate_work(self):
        """The same file added twice is two rows, and each needs its own
        thumbnail - which is why this keys on identity, not path."""
        from core.models import ImageEntry
        a, b = ImageEntry(path="/tmp/same.png"), ImageEntry(path="/tmp/same.png")
        work = self._split([a, b], cached=[0])
        self.assertEqual(work.needed, [b])

    def test_no_rows_in_view_is_no_work(self):
        from core.viewport import entries_needing_thumbnails
        work = entries_needing_thumbnails([], lambda eid: False, set())
        self.assertEqual(work.needed, [])
        self.assertEqual(work.examined, 0)


class TestHydrusThumbnails(unittest.TestCase):
    """Row thumbnails used to read every file, which for a large batch on
    a network share means pulling the whole library across the wire just
    to draw previews. Hydrus already has thumbnails for files it holds."""

    def setUp(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        self.client = HydrusClient(HydrusSettings(api_url="http://x", access_key="k"))

    def test_existence_check_requests_identifiers_only(self):
        """Existence is all that's needed here; pulling full metadata for
        tens of thousands of hashes would transfer a large payload to
        answer a yes/no question."""
        from unittest.mock import MagicMock, patch
        captured = {}

        def fake_get(url, **kwargs):
            captured["params"] = kwargs.get("params")
            resp = MagicMock()
            resp.status_code = 200
            # What Hydrus really sends, verified against a live client:
            # it answers for EVERY hash asked about, and marks the ones it
            # doesn't hold with a null file_id rather than omitting them.
            resp.json.return_value = {"metadata": [
                {"hash": "aa" * 32, "file_id": 1234},
                {"hash": "bb" * 32, "file_id": None},
            ]}
            return resp

        with patch("requests.get", side_effect=fake_get):
            known = self.client.filter_known_hashes(["aa" * 32, "bb" * 32])
        self.assertEqual(known, {"aa" * 32})
        self.assertEqual(captured["params"].get("only_return_identifiers"), "true")

    def test_a_hash_hydrus_does_not_hold_is_not_called_known(self):
        """REGRESSION: this checked only that a row came back, and Hydrus
        returns a row for every hash asked about - so an unknown hash was
        reported as known. The null file_id is the actual signal."""
        from unittest.mock import MagicMock, patch
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"metadata": [{"hash": "dd" * 32, "file_id": None}]}
        with patch("requests.get", return_value=resp):
            self.assertEqual(self.client.filter_known_hashes(["dd" * 32]), set())

    def test_unknown_hashes_are_not_reported_as_known(self):
        """REGRESSION GUARD: /get_files/thumbnail is documented never to
        404 - an unknown file returns Hydrus's generic icon instead. If
        unknown hashes leaked through here, every non-Hydrus file would
        silently get a Hydrus logo for a thumbnail."""
        from unittest.mock import MagicMock, patch
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"metadata": []}
        with patch("requests.get", return_value=resp):
            self.assertEqual(self.client.filter_known_hashes(["cc" * 32]), set())

    def test_thumbnail_returns_bytes(self):
        from unittest.mock import MagicMock, patch
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"thumb-bytes"
        with patch("requests.get", return_value=resp):
            self.assertEqual(self.client.get_thumbnail("aa" * 32), b"thumb-bytes")

    def test_failed_thumbnail_request_returns_none(self):
        from unittest.mock import MagicMock, patch
        resp = MagicMock()
        resp.status_code = 403
        resp.content = b"denied"
        with patch("requests.get", return_value=resp):
            self.assertIsNone(self.client.get_thumbnail("aa" * 32))

    def test_empty_hash_list_makes_no_request(self):
        from unittest.mock import patch
        with patch("requests.get", side_effect=AssertionError("should not call out")):
            self.assertEqual(self.client.filter_known_hashes([]), set())


class TestThumbnailSourceSetting(unittest.TestCase):
    def setUp(self):
        import ast
        src = open("workers/thumbnail_worker.py").read()
        cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef))
        fn = next(n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_thumbnail_source")
        ns = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<x>", "exec"), ns)
        self.thumbnail_source = ns["_thumbnail_source"]

    def _worker(self, settings):
        return type("W", (), {"settings": settings})()

    def test_defaults_to_hydrus(self):
        from core.config import Settings
        self.assertEqual(Settings().thumbnail_source, "hydrus")

    def test_reads_each_mode(self):
        from core.config import Settings
        for mode in ("hydrus", "local", "off"):
            with self.subTest(mode=mode):
                settings = Settings()
                settings.thumbnail_source = mode
                self.assertEqual(self.thumbnail_source(self._worker(settings)), mode)

    def test_without_settings_falls_back_to_local(self):
        """Never assume Hydrus is available when there's nothing to say
        so - decoding locally always works."""
        self.assertEqual(self.thumbnail_source(self._worker(None)), "local")


class TestHydrusSizeCrossCheck(unittest.TestCase):
    """Sampling can't prove every filename in a batch is honest, so the
    one place a wrong hash would do real damage - importing another
    file's tags - is guarded independently, using the size Hydrus already
    returns in the same response."""

    def setUp(self):
        import os, tempfile
        self.path = os.path.join(tempfile.mkdtemp(), "a.jpg")
        with open(self.path, "wb") as fh:
            fh.write(b"x" * 1000)

    def _run(self, hydrus_size):
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.models import ImageEntry
        from core import hydrus_tag_lookup as htl

        settings = Settings()
        settings.hydrus.access_key = "k"
        entry = ImageEntry(path=self.path)
        entry.hydrus_hash = "a" * 64

        client = MagicMock()
        client.get_tags_and_sizes_for_hashes.return_value = {"a" * 64: (["1girl"], hydrus_size)}
        with patch.object(htl, "HydrusClient", return_value=client):
            result = htl.apply_existing_hydrus_tags([entry], settings)
        return result.tagged_count, [t.display for t in entry.tags]

    def test_matching_size_imports_normally(self):
        self.assertEqual(self._run(1000), (1, ["1girl"]))

    def test_mismatched_size_blocks_the_import(self):
        self.assertEqual(self._run(9999), (0, []))

    def test_missing_size_does_not_block(self):
        """Older Hydrus builds may not report a size; refusing to import
        over a missing field would be worse than the risk."""
        self.assertEqual(self._run(None), (1, ["1girl"]))


class TestHydrusTagLookupFailureIsDistinguishableFromZeroTags(unittest.TestCase):
    """DAN-95: a lookup that FAILS (Hydrus unreachable, access key
    rejected) must not come back looking identical to a lookup that
    SUCCEEDED and simply found nothing - the overwhelmingly common case
    right after import. Both used to collapse to tagged_count=0."""

    def setUp(self):
        import os, tempfile
        self.path = os.path.join(tempfile.mkdtemp(), "a.jpg")
        with open(self.path, "wb") as fh:
            fh.write(b"x" * 10)

    def test_a_lookup_failure_is_distinguishable_from_zero_tags(self):
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.hydrus_client import HydrusError
        from core.models import ImageEntry
        from core import hydrus_tag_lookup as htl

        settings = Settings()
        settings.hydrus.access_key = "k"
        entry = ImageEntry(path=self.path)
        entry.hydrus_hash = "a" * 64

        client = MagicMock()
        client.get_tags_and_sizes_for_hashes.side_effect = HydrusError(
            "Could not reach Hydrus at http://127.0.0.1:1: Connection refused"
        )
        with patch.object(htl, "HydrusClient", return_value=client):
            result = htl.apply_existing_hydrus_tags([entry], settings)

        self.assertEqual(result.tagged_count, 0)
        self.assertIsNotNone(result.error)
        self.assertIn("Connection refused", result.error)

    def test_a_genuine_zero_tags_result_carries_no_error(self):
        from unittest.mock import MagicMock, patch
        from core.config import Settings
        from core.models import ImageEntry
        from core import hydrus_tag_lookup as htl

        settings = Settings()
        settings.hydrus.access_key = "k"
        entry = ImageEntry(path=self.path)
        entry.hydrus_hash = "a" * 64

        client = MagicMock()
        client.get_tags_and_sizes_for_hashes.return_value = {}
        with patch.object(htl, "HydrusClient", return_value=client):
            result = htl.apply_existing_hydrus_tags([entry], settings)

        self.assertEqual(result.tagged_count, 0)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()


class TestHydrusDelete(unittest.TestCase):
    """Deleting through Hydrus rather than off disk.

    A file in Hydrus's store IS Hydrus's copy - removing it behind
    Hydrus's back leaves the record in place and Hydrus expecting a file
    that isn't there.
    """

    def setUp(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        self.client = HydrusClient(HydrusSettings(api_url="http://x", access_key="k"))

    def _metadata(self, entries):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"metadata": entries}
        return resp

    def _held(self, h):
        return {"hash": h, "file_id": 1, "is_local": True,
                "is_trashed": False, "is_deleted": False}

    def test_a_held_file_is_deleted_with_a_reason(self):
        from unittest.mock import patch
        held = "aa" * 32
        with patch("requests.get", return_value=self._metadata([self._held(held)])), \
             patch("requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {}
            asked = self.client.delete_files([held], reason="because")
        self.assertEqual(asked, [held])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["hashes"], [held])
        self.assertEqual(body["reason"], "because")
        self.assertIn("/add_files/delete_files", post.call_args.args[0])

    def test_a_hash_hydrus_never_had_is_never_sent(self):
        """THE important guard. Deleting an unknown hash does not no-op:
        Hydrus writes a deletion record for it, and will then refuse that
        file if it is ever imported later. Confirmed against a live client
        - a made-up hash came back is_deleted afterwards, and the record
        could not be cleared again."""
        from unittest.mock import patch
        unknown = "bb" * 32
        with patch("requests.get",
                   return_value=self._metadata([{"hash": unknown, "file_id": None}])), \
             patch("requests.post") as post:
            asked = self.client.delete_files([unknown])
        self.assertEqual(asked, [])
        post.assert_not_called()

    def test_an_already_deleted_file_is_not_sent_again(self):
        """It keeps its file_id, so 'has a file_id' is not the test."""
        from unittest.mock import patch
        gone = "cc" * 32
        already = {"hash": gone, "file_id": 7, "is_local": False,
                   "is_trashed": False, "is_deleted": True}
        with patch("requests.get", return_value=self._metadata([already])), \
             patch("requests.post") as post:
            self.assertEqual(self.client.delete_files([gone]), [])
        post.assert_not_called()

    def test_only_the_held_files_of_a_mixed_selection_are_sent(self):
        from unittest.mock import patch
        held, unknown = "aa" * 32, "bb" * 32
        rows = [self._held(held), {"hash": unknown, "file_id": None}]
        with patch("requests.get", return_value=self._metadata(rows)), \
             patch("requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {}
            asked = self.client.delete_files([held, unknown])
        self.assertEqual(asked, [held])
        self.assertEqual(post.call_args.kwargs["json"]["hashes"], [held])

    def test_an_empty_selection_makes_no_request(self):
        from unittest.mock import patch
        with patch("requests.get") as get, patch("requests.post") as post:
            self.assertEqual(self.client.delete_files([]), [])
        get.assert_not_called()
        post.assert_not_called()

    def test_deletion_states_distinguishes_every_case(self):
        from unittest.mock import patch
        rows = [
            self._held("aa" * 32),
            {"hash": "bb" * 32, "file_id": 2, "is_local": False,
             "is_trashed": True, "is_deleted": False},
            {"hash": "cc" * 32, "file_id": 3, "is_local": False,
             "is_trashed": False, "is_deleted": True},
            {"hash": "dd" * 32, "file_id": None},
        ]
        with patch("requests.get", return_value=self._metadata(rows)):
            states = self.client.deletion_states(
                ["aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32])
        self.assertEqual(states, {
            "aa" * 32: "present", "bb" * 32: "trashed",
            "cc" * 32: "deleted", "dd" * 32: "unknown",
        })

    def test_undelete_posts_the_hashes(self):
        from unittest.mock import patch
        with patch("requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {}
            self.client.undelete_files(["aa" * 32])
        self.assertIn("/add_files/undelete_files", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["json"]["hashes"], ["aa" * 32])


class TestCrashDiagnostics(unittest.TestCase):
    """Three crashes were diagnosed during development and NONE left
    anything in app.log - it just stopped mid-sentence. Each needed a core
    dump. The common cause was stderr: Qt's qFatal message, PyQt's
    traceback for an exception in a slot, and a hard fault all report
    there, and a GUI has nowhere for stderr to go."""

    def setUp(self):
        """Install the hooks in isolation and put the originals back.

        install() guards itself so it only ever runs once for real; these
        tests exercise the pieces directly rather than defeating that
        guard, which would chain each test's handler onto the last.
        """
        import threading
        from core import crashlog
        self.crashlog = crashlog
        original_sys, original_thread = sys.excepthook, threading.excepthook
        self.addCleanup(setattr, sys, "excepthook", original_sys)
        self.addCleanup(setattr, threading, "excepthook", original_thread)
        crashlog._install_python_hooks()

    def test_install_is_idempotent(self):
        """It must not chain a second handler onto the first."""
        from core import crashlog
        crashlog.install()
        first = sys.excepthook
        crashlog.install()
        self.assertIs(sys.excepthook, first)

    def test_python_exceptions_are_logged(self):
        from unittest.mock import patch
        with patch.object(self.crashlog.log, "critical") as critical:
            try:
                raise ValueError("boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        critical.assert_called_once()
        self.assertIn("boom", str(critical.call_args))

    def test_keyboard_interrupt_is_left_alone(self):
        """Ctrl-C is a user action, not a crash to report."""
        from unittest.mock import patch
        with patch.object(self.crashlog.log, "critical") as critical:
            try:
                raise KeyboardInterrupt()
            except KeyboardInterrupt:
                sys.excepthook(*sys.exc_info())
        critical.assert_not_called()

    def test_worker_thread_exceptions_are_logged(self):
        """A worker dying silently is how a search stops halfway with no
        explanation anywhere."""
        import threading
        from unittest.mock import patch
        with patch.object(self.crashlog.log, "critical") as critical:
            def boom():
                raise RuntimeError("worker died")
            t = threading.Thread(target=boom)
            t.start()
            t.join()
        self.assertTrue(critical.called)
        self.assertIn("worker died", str(critical.call_args))

    def test_a_qt_fatal_message_is_logged_before_it_aborts(self):
        """REGRESSION: 'QThread: Destroyed while thread is still running'
        is reported through qFatal, which prints to stderr and aborts -
        so the one line explaining the crash was the one line guaranteed
        not to be kept."""
        from unittest.mock import MagicMock, patch
        from PyQt6.QtCore import QtMsgType
        from core import crashlog
        captured = {}
        with patch("PyQt6.QtCore.qInstallMessageHandler",
                   side_effect=lambda h: captured.setdefault("handler", h)):
            crashlog._install_qt_handler()
        handler = captured["handler"]
        with patch.object(crashlog.log, "critical") as critical, \
             patch.object(crashlog, "_flush_log") as flush:
            handler(QtMsgType.QtFatalMsg, MagicMock(file=None, line=0),
                    "QThread: Destroyed while thread is still running")
        critical.assert_called_once()
        self.assertIn("still running", str(critical.call_args))
        flush.assert_called_once()   # qFatal aborts the moment we return

    def test_qt_warnings_do_not_masquerade_as_crashes(self):
        from unittest.mock import MagicMock, patch
        from PyQt6.QtCore import QtMsgType
        from core import crashlog
        captured = {}
        with patch("PyQt6.QtCore.qInstallMessageHandler",
                   side_effect=lambda h: captured.setdefault("handler", h)):
            crashlog._install_qt_handler()
        with patch.object(crashlog.log, "warning") as warned, \
             patch.object(crashlog.log, "critical") as critical:
            captured["handler"](QtMsgType.QtWarningMsg, MagicMock(file=None, line=0), "just a warning")
        warned.assert_called_once()
        critical.assert_not_called()

    def test_retiring_a_worker_does_not_warn(self):
        """_retire_worker's wildcard disconnect provokes this on every
        worker it lets go, and it was 125 of one session's 153 warnings.
        Four in five warnings saying nothing is how the one that means
        something gets skipped."""
        from unittest.mock import MagicMock, patch
        from PyQt6.QtCore import QtMsgType
        from core import crashlog
        captured = {}
        with patch("PyQt6.QtCore.qInstallMessageHandler",
                   side_effect=lambda h: captured.setdefault("handler", h)):
            crashlog._install_qt_handler()
        message = ("QObject::disconnect: wildcard call disconnects from "
                   "destroyed signal of SearchWorker::unnamed")
        with patch.object(crashlog.log, "warning") as warned, \
             patch.object(crashlog.log, "debug") as debugged:
            captured["handler"](QtMsgType.QtWarningMsg, MagicMock(file=None, line=0), message)
        warned.assert_not_called()
        # Kept, not dropped: still evidence if a teardown needs
        # reconstructing later.
        debugged.assert_called_once()

    def test_a_real_disconnect_problem_still_warns(self):
        """Only the known-benign wildcard message is quietened. Anything
        else Qt says about disconnecting is a genuine bug in wiring."""
        from unittest.mock import MagicMock, patch
        from PyQt6.QtCore import QtMsgType
        from core import crashlog
        captured = {}
        with patch("PyQt6.QtCore.qInstallMessageHandler",
                   side_effect=lambda h: captured.setdefault("handler", h)):
            crashlog._install_qt_handler()
        with patch.object(crashlog.log, "warning") as warned:
            captured["handler"](
                QtMsgType.QtWarningMsg, MagicMock(file=None, line=0),
                "QObject::disconnect: No such signal QWidget::nonexistent()")
        warned.assert_called_once()


class TestUncleanShutdownMarker(unittest.TestCase):
    """DAN-485: a crash and a normal quit used to restore identically -
    nothing recorded whether the previous run ever reached a clean exit.
    These exercise the running-marker sentinel in isolation, against a
    throwaway directory rather than the real CONFIG_DIR."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core import crashlog
        self.crashlog = crashlog
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        marker = Path(self._tmpdir.name) / "crash.log.running"
        fault = Path(self._tmpdir.name) / "crash.log"
        self._marker_patch = patch.object(crashlog, "RUNNING_MARKER", marker)
        self._fault_patch = patch.object(crashlog, "FAULT_FILE", fault)
        # mark_clean_shutdown also records WHEN, beside the marker - keep
        # that off the real config directory too.
        self._exit_patch = patch.object(
            crashlog, "CLEAN_EXIT_FILE", Path(self._tmpdir.name) / "last_clean_exit")
        self._marker_patch.start()
        self._fault_patch.start()
        self._exit_patch.start()
        self.addCleanup(self._marker_patch.stop)
        self.addCleanup(self._fault_patch.stop)
        self.addCleanup(self._exit_patch.stop)

    def test_first_ever_launch_is_not_an_unclean_shutdown(self):
        """No marker on disk at all - e.g. a fresh install - must not be
        mistaken for a crash."""
        self.assertFalse(self.crashlog.check_unclean_shutdown())

    def test_marker_left_behind_means_the_last_run_crashed(self):
        """REGRESSION: a crash and a normal quit used to be indistinguishable.
        Starting once without a clean shutdown must be visible to the next
        startup's check."""
        self.crashlog.mark_running()
        self.assertTrue(self.crashlog.check_unclean_shutdown())

    def test_clean_shutdown_clears_the_marker(self):
        self.crashlog.mark_running()
        self.crashlog.mark_clean_shutdown()
        self.assertFalse(self.crashlog.check_unclean_shutdown())

    def test_clearing_a_marker_that_was_never_written_does_not_raise(self):
        """mark_clean_shutdown runs unconditionally in closeEvent; it must
        not matter whether mark_running ever actually got called."""
        self.crashlog.mark_clean_shutdown()

    def test_full_cycle_two_launches(self):
        """Simulates: launch 1 crashes (no clean shutdown), launch 2 starts
        and must see that, then itself exits cleanly, so launch 3 sees a
        clean history again."""
        self.crashlog.mark_running()
        # launch 1 "crashes": no mark_clean_shutdown call.

        self.assertTrue(self.crashlog.check_unclean_shutdown())  # launch 2's check
        self.crashlog.mark_running()
        self.crashlog.mark_clean_shutdown()  # launch 2 exits cleanly

        self.assertFalse(self.crashlog.check_unclean_shutdown())  # launch 3's check


class TestCrashLogTail(unittest.TestCase):
    """DAN-485 gap 2: crash.log was written by crashlog.py but never read
    back by anything - the log viewer only ever opened app.log."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from core import crashlog
        self.crashlog = crashlog
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fault_file = Path(self._tmpdir.name) / "crash.log"
        self._fault_patch = patch.object(crashlog, "FAULT_FILE", self.fault_file)
        self._fault_patch.start()
        self.addCleanup(self._fault_patch.stop)

    def test_missing_crash_log_reads_as_empty_not_an_error(self):
        self.assertEqual(self.crashlog.get_recent_crash_log(), "")

    def test_reads_back_what_was_written(self):
        self.fault_file.write_text("--- session started ---\nQThread: boom\n", encoding="utf-8")
        self.assertIn("QThread: boom", self.crashlog.get_recent_crash_log())

    def test_only_the_tail_is_returned_for_a_log_grown_over_time(self):
        """crash.log is appended to for CONFIG_DIR's whole lifetime -
        potentially years of runs - so a multi-megabyte file must not be
        read in full just to show the most recent fault."""
        from unittest.mock import patch
        old_patch = patch.object(self.crashlog, "CRASH_LOG_TAIL_CHARS", 20)
        old_patch.start()
        self.addCleanup(old_patch.stop)
        self.fault_file.write_text("x" * 1000 + "RECENT_FAULT", encoding="utf-8")
        tail = self.crashlog.get_recent_crash_log()
        self.assertLessEqual(len(tail), 20)
        self.assertIn("RECENT_FAULT", tail)


class TestParserHealth(unittest.TestCase):
    """Safebooru, rule34 and Xbooru returned zero tags - possibly for as
    long as they were supported - and nothing said so. This tracks what
    each parser produced so the app can notice instead of the user."""

    def setUp(self):
        from core import parser_health
        self.health = parser_health
        parser_health.reset()
        self.addCleanup(parser_health.reset)

    def test_a_run_of_empties_with_no_successes_looks_broken(self):
        for _ in range(self.health.CONSECUTIVE_EMPTIES_BEFORE_SUSPECT):
            self.health.record_empty("safebooru", "u")
        entry = {h.site: h for h in self.health.snapshot()}["safebooru"]
        self.assertTrue(entry.suspect)

    def test_one_empty_result_is_not_a_fault(self):
        """A post genuinely can have no tags, and a hash-keyed lookup
        misses far more often than it hits."""
        self.health.record_empty("sankaku", "u")
        self.assertFalse({h.site: h for h in self.health.snapshot()}["sankaku"].suspect)

    def test_a_parser_that_has_ever_worked_is_not_called_broken(self):
        """Otherwise a run of tagless posts would cry wolf and train the
        user to ignore the warning."""
        self.health.record_success("e621", 30)
        for _ in range(10):
            self.health.record_empty("e621", "u")
        self.assertFalse({h.site: h for h in self.health.snapshot()}["e621"].suspect)

    def test_a_success_resets_the_run(self):
        self.health.record_empty("gelbooru", "u")
        self.health.record_empty("gelbooru", "u")
        self.health.record_success("gelbooru", 5)
        entry = {h.site: h for h in self.health.snapshot()}["gelbooru"]
        self.assertEqual(entry.consecutive_empty, 0)

    def test_a_failed_fetch_is_kept_apart_from_an_empty_parse(self):
        """A site being down says nothing about whether its parser fits."""
        for _ in range(5):
            self.health.record_failure("zerochan", "u", "timed out")
        entry = {h.site: h for h in self.health.snapshot()}["zerochan"]
        self.assertFalse(entry.suspect)
        self.assertEqual(entry.failed, 5)

    def test_each_site_is_reported_only_once(self):
        """A batch must not repeat the same warning on every image."""
        for _ in range(self.health.CONSECUTIVE_EMPTIES_BEFORE_SUSPECT):
            self.health.record_empty("xbooru", "u")
        self.assertEqual([h.site for h in self.health.newly_suspect()], ["xbooru"])
        self.assertEqual(self.health.newly_suspect(), [])

    def test_broken_sites_sort_first(self):
        self.health.record_success("e621", 3)
        for _ in range(self.health.CONSECUTIVE_EMPTIES_BEFORE_SUSPECT):
            self.health.record_empty("rule34", "u")
        self.assertEqual(self.health.snapshot()[0].site, "rule34")


class TestParserProbe(unittest.TestCase):
    """The live check. The fixture suite could not have caught the bugs
    that happened - three came from fixtures that did not match reality -
    so this fetches a real post per site instead."""

    def _info(self, **kwargs):
        from core.boorus import BooruPageInfo
        from core.models import Tag, TagSource
        defaults = dict(
            tags=[Tag("1girl", TagSource.BOORU)], width=800, height=600,
            preview_url="https://x/p.jpg", file_url="https://x/f.jpg", fetched=True,
        )
        defaults.update(kwargs)
        return BooruPageInfo(**defaults)

    def _run(self, info_or_exc, site="danbooru"):
        from unittest.mock import patch
        from core import parser_probe
        def fake(url, **kwargs):
            if isinstance(info_or_exc, Exception):
                raise info_or_exc
            return info_or_exc
        with patch("core.boorus.fetch_page_info", side_effect=fake):
            return parser_probe.run(only=[site])[0]

    def test_a_healthy_site_passes(self):
        self.assertTrue(self._run(self._info()).ok)

    def test_no_tags_is_a_fault_for_a_booru(self):
        result = self._run(self._info(tags=[]))
        self.assertFalse(result.ok)
        self.assertIn("no tags", result.problem)

    def test_no_tags_is_expected_for_sites_without_them(self):
        """A tweet has no tag vocabulary; flagging that would be noise."""
        self.assertTrue(self._run(self._info(tags=[]), site="twitter").ok)

    def test_missing_dimensions_are_a_fault(self):
        """They drive the 'is this an upgrade?' comparison, so a site
        quietly ceasing to report them would silently disable it."""
        result = self._run(self._info(width=None, height=None))
        self.assertFalse(result.ok)
        self.assertIn("dimensions", result.problem)

    def test_a_post_the_site_withholds_is_not_a_parser_fault(self):
        result = self._run(self._info(preview_url=None, file_url=None,
                                      restricted="needs an account"))
        self.assertTrue(result.ok)
        self.assertIn("held back", result.summary())

    def test_a_fetch_failure_is_reported_not_raised(self):
        result = self._run(RuntimeError("site is down"))
        self.assertFalse(result.ok)
        self.assertIn("site is down", result.problem)

    def test_progress_is_reported_for_each_site(self):
        from unittest.mock import patch
        from core import parser_probe
        seen = []
        with patch("core.boorus.fetch_page_info", return_value=self._info()):
            parser_probe.run(on_progress=lambda d, t, s: seen.append((d, t, s)),
                             only=["danbooru", "e621"])
        self.assertEqual([s for _, _, s in seen[:2]], ["danbooru", "e621"])
        self.assertEqual(seen[-1][0], seen[-1][1], "the last call should report completion")

    def test_every_probe_url_matches_the_parser_it_claims(self):
        """A reference URL that drifts would test the wrong parser, or
        none - which is how a probe quietly stops checking anything."""
        from core.boorus import find_parser
        from core import parser_probe
        for site, url in parser_probe.PROBES:
            with self.subTest(site=site):
                parser = find_parser(url)
                self.assertIsNotNone(parser, f"no parser matches the {site} probe URL")
                self.assertEqual(parser.__name__.rsplit(".", 1)[-1], site)


class TestRateLimitCredit(unittest.TestCase):
    """The gap between searches is measured engine-request to
    engine-request. Work that happens in between - the booru page fetch,
    the availability sweep, auto-import polling Hydrus - hits other hosts
    entirely, so it counts toward the gap instead of being waited out on
    top of it."""

    def test_non_engine_work_is_credited(self):
        """20s of booru/Hydrus work after the engine request leaves 40s of
        a 60s gap, not another full 60s."""
        from core.rate_limit import remaining_delay
        self.assertAlmostEqual(remaining_delay(60.0, 1000.0, 1020.0), 40.0)

    def test_a_slow_enough_image_waits_not_at_all(self):
        """When the follow-up work outlasts the gap, the next engine
        request is already overdue - waiting further would be waiting for
        no reason."""
        from core.rate_limit import remaining_delay
        self.assertEqual(remaining_delay(60.0, 1000.0, 1075.0), 0.0)

    def test_never_returns_negative(self):
        """A negative would be passed to sleep() as-is downstream."""
        from core.rate_limit import remaining_delay
        self.assertGreaterEqual(remaining_delay(45.0, 1000.0, 5000.0), 0.0)

    def test_unknown_engine_time_waits_the_full_delay(self):
        """None means no engine request completed - a cache hit, or a
        failure part-way. Crediting an unknown would risk hammering the
        engines, so the conservative full delay applies."""
        from core.rate_limit import remaining_delay
        self.assertEqual(remaining_delay(60.0, None, 9999.0), 60.0)

    def test_credit_never_extends_the_wait(self):
        """REGRESSION GUARD: mismatched clocks gave a negative elapsed,
        which would otherwise have been SUBTRACTED - lengthening the gap
        instead of shortening it."""
        from core.rate_limit import remaining_delay
        self.assertEqual(remaining_delay(60.0, 2000.0, 1000.0), 60.0)

    def test_instant_followup_still_waits_the_whole_gap(self):
        """Nothing is credited when nothing happened - the politeness
        interval is unchanged in the case it was designed for."""
        from core.rate_limit import remaining_delay
        self.assertAlmostEqual(remaining_delay(60.0, 1000.0, 1000.0), 60.0)

    def test_search_image_reports_when_the_engines_finished(self):
        """The credit is only correct if the callback fires BEFORE the
        booru page fetch - firing it at the end would credit nothing."""
        import inspect
        from core import search_engine
        src = inspect.getsource(search_engine.search_image)
        self.assertIn("on_engines_done", src)
        fired = src.index("on_engines_done()")
        fetched = src.index("fetch_candidate_details")
        self.assertLess(
            fired, fetched,
            "on_engines_done must fire before candidate details are fetched, "
            "otherwise the booru fetch time is not credited",
        )

    def test_search_image_accepts_the_callback(self):
        """Signature guard: the worker passes this by keyword."""
        import inspect
        from core import search_engine
        params = inspect.signature(search_engine.search_image).parameters
        self.assertIn("on_engines_done", params)
        self.assertIsNone(params["on_engines_done"].default)


class TestPerEngineIntervals(unittest.TestCase):
    """Resolving one engine's delay from its override plus the global."""

    def _iv(self, overrides, engine="iqdb", lo=45.0, hi=75.0):
        from core.rate_limit import engine_interval
        return engine_interval(engine, overrides, lo, hi)

    def test_no_override_uses_the_global_delay(self):
        """The default for every engine, so an untouched config paces
        exactly as it did before per-engine delays existed."""
        self.assertEqual(self._iv({}), (45.0, 75.0))
        self.assertEqual(self._iv(None), (45.0, 75.0))

    def test_an_override_wins_for_that_engine_only(self):
        overrides = {"iqdb": [10.0, 20.0]}
        self.assertEqual(self._iv(overrides, "iqdb"), (10.0, 20.0))
        self.assertEqual(self._iv(overrides, "saucenao"), (45.0, 75.0))

    def test_a_reversed_pair_is_ordered_not_rejected(self):
        self.assertEqual(self._iv({"iqdb": [30.0, 10.0]}), (10.0, 30.0))

    def test_malformed_overrides_fall_back_to_the_global(self):
        """A hand-edited config must degrade to the SAFE setting. Falling
        back to no delay would be the one failure that gets a user
        banned."""
        for bad in ([], [5.0], "nonsense", None, [None, None], [-5.0, -1.0], {}):
            with self.subTest(bad=bad):
                self.assertEqual(self._iv({"iqdb": bad}), (45.0, 75.0))


class TestEngineRateLimiter(unittest.TestCase):
    """Each engine is a separate host with its own clock, so a search
    that queries only some of them owes nothing to the ones it skipped."""

    def setUp(self):
        from core.rate_limit import EngineRateLimiter
        self.limiter = EngineRateLimiter()

    def test_an_unqueried_engine_imposes_no_wait(self):
        """The whole point: forcing "IQDB only" must not wait on SauceNAO,
        which is exactly what you do once its daily quota is spent."""
        self.limiter.record("saucenao", 1000.0)
        self.assertEqual(
            self.limiter.wait_needed(["iqdb"], {"iqdb": 60.0}, 1000.0), 0.0)

    def test_waits_the_remainder_of_that_engines_own_gap(self):
        self.limiter.record("iqdb", 1000.0)
        self.assertAlmostEqual(
            self.limiter.wait_needed(["iqdb"], {"iqdb": 60.0}, 1020.0), 40.0)

    def test_takes_the_longest_wait_across_the_wave(self):
        """A wave queries its engines together, so it can only start when
        the slowest of them is ready. Taking the minimum would start the
        wave early and hit the others before their gap had elapsed - the
        one mistake this must never make."""
        self.limiter.record("iqdb", 1000.0)
        self.limiter.record("saucenao", 1000.0)
        wait = self.limiter.wait_needed(
            ["iqdb", "saucenao"], {"iqdb": 10.0, "saucenao": 60.0}, 1000.0)
        self.assertAlmostEqual(wait, 60.0)

    def test_never_negative(self):
        self.limiter.record("iqdb", 1000.0)
        self.assertEqual(
            self.limiter.wait_needed(["iqdb"], {"iqdb": 10.0}, 9999.0), 0.0)

    def test_backwards_clock_falls_back_to_the_full_interval(self):
        """A negative elapsed would otherwise be SUBTRACTED, crediting
        time that never passed."""
        self.limiter.record("iqdb", 2000.0)
        self.assertAlmostEqual(
            self.limiter.wait_needed(["iqdb"], {"iqdb": 60.0}, 1000.0), 60.0)

    def test_recording_again_restarts_that_engines_gap(self):
        self.limiter.record("iqdb", 1000.0)
        self.limiter.record("iqdb", 1050.0)
        self.assertAlmostEqual(
            self.limiter.wait_needed(["iqdb"], {"iqdb": 60.0}, 1060.0), 50.0)

    def test_is_safe_from_parallel_engine_threads(self):
        """A wave records each engine from its own thread."""
        import threading
        from core.engines import ALL_ENGINES
        def hammer(engine):
            for i in range(200):
                self.limiter.record(engine, float(i))
                self.limiter.wait_needed(ALL_ENGINES, {e: 1.0 for e in ALL_ENGINES}, 500.0)
        threads = [threading.Thread(target=hammer, args=(e,)) for e in ALL_ENGINES]
        for t in threads: t.start()
        for t in threads: t.join()
        for e in ALL_ENGINES:
            self.assertIsNotNone(self.limiter.last_seen(e))


class TestPlannedEngines(unittest.TestCase):
    """Which hosts the next search will touch - the caller has to know
    before it queries them."""

    def _plan(self, override=None, **kw):
        from core.config import Settings
        from core.search_engine import planned_engines
        s = Settings()
        s.enable_pawchive_index = False     # local and inert until used; noise here
        for k, v in kw.items():
            setattr(s, k, v)
        return planned_engines(s, override)

    def test_a_forced_engine_is_the_only_one_planned(self):
        self.assertEqual(self._plan("iqdb"), ["iqdb"])

    def test_the_conditional_fallback_engine_is_counted(self):
        """It only runs when the primary finds nothing, but counting it
        always means occasionally waiting for a host that turns out not
        to be queried - which costs a little time. Leaving it out would
        mean occasionally querying a host early, which costs a ban."""
        plan = self._plan(primary_engine="iqdb", secondary_engine_mode="fallback",
                          enable_ascii2d=False, enable_tracemoe=False, enable_iqdb3d=False)
        self.assertIn("saucenao", plan)
        self.assertIn("iqdb", plan)

    def test_a_disabled_secondary_is_not_planned(self):
        plan = self._plan(primary_engine="iqdb", secondary_engine_mode="disabled",
                          enable_ascii2d=False, enable_tracemoe=False, enable_iqdb3d=False)
        self.assertEqual(plan, ["iqdb"])

    def test_enabled_extras_are_planned(self):
        plan = self._plan(primary_engine="iqdb", secondary_engine_mode="disabled",
                          enable_ascii2d=True, enable_tracemoe=True, enable_iqdb3d=True)
        for engine in ("iqdb", "ascii2d", "tracemoe", "iqdb3d"):
            self.assertIn(engine, plan)

    def test_no_engine_is_listed_twice(self):
        """Duplicates would be harmless for waiting but signal that the
        wave flattening had gone wrong."""
        plan = self._plan(primary_engine="iqdb", secondary_engine_mode="fallback")
        self.assertEqual(len(plan), len(set(plan)))


class TestMyAnimeListSite(unittest.TestCase):
    """MyAnimeList, named so it can be filtered deliberately.

    Like MangaUpdates, a match there is a database entry for a series
    (/manga/123094/, /anime/1535/) rather than a page of it - no image,
    no tags, nothing for a parser to fetch.
    """

    def test_a_manga_entry_is_classified_as_myanimelist(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://myanimelist.net/manga/123094/Some-Title"),
            "MyAnimeList")

    def test_the_www_form_counts_too(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://www.myanimelist.net/anime/1535/Death_Note"),
            "MyAnimeList")

    def test_it_is_offered_in_the_sites_menu(self):
        from core.sites import ALL_SITE_OPTIONS
        self.assertIn("MyAnimeList", ALL_SITE_OPTIONS)

    def test_the_other_database_sites_keep_their_own_names(self):
        """Three sites of the same shape, three separate switches - one
        label for all of them would make the Sites menu lie."""
        from core.sites import classify_site
        self.assertEqual(classify_site(None, "https://anilist.co/anime/21"), "AniList")
        self.assertEqual(
            classify_site(None, "https://www.mangaupdates.com/series.html?id=1"),
            "MangaUpdates")

    def test_unticking_it_filters_those_matches_out(self):
        from core.sites import classify_site
        enabled = {"Danbooru", "Other"}          # MyAnimeList unticked
        url = "https://myanimelist.net/manga/123094/Some-Title"
        self.assertNotIn(classify_site(None, url), enabled)

    def test_an_existing_config_has_it_added_rather_than_unchecked(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config

        stored = {"schema_version": 8,
                  "enabled_sites": ["Danbooru", "Paheal", "MangaUpdates", "Other"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                loaded = config.Settings.load()
        self.assertIn("MyAnimeList", loaded.enabled_sites)
        for kept in ("MangaUpdates", "Paheal", "Danbooru"):
            self.assertIn(kept, loaded.enabled_sites, f"{kept} must survive the migration")

    def test_a_very_old_config_gains_every_site_added_since(self):
        """Migrations are cumulative: a config from v6 has to arrive with
        all three of the sites named since, not just the newest."""
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config

        stored = {"schema_version": 6, "enabled_sites": ["Danbooru", "Other"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                loaded = config.Settings.load()
        for site in ("Paheal", "MangaUpdates", "MyAnimeList"):
            self.assertIn(site, loaded.enabled_sites, site)
        self.assertEqual(loaded.schema_version, config.CURRENT_SCHEMA_VERSION)


class TestRule34UsSite(unittest.TestCase):
    """Rule34.us, a third site despite the name - its own label, not
    rule34.xxx's "Rule34" and not Paheal."""

    def _load(self, stored):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                return config.Settings.load()

    def test_a_post_is_classified_as_rule34us(self):
        from core.sites import ALL_SITE_OPTIONS, classify_site
        self.assertEqual(
            classify_site(None, "https://rule34.us/index.php?r=posts/view&id=6608448"),
            "Rule34.us")
        self.assertIn("Rule34.us", ALL_SITE_OPTIONS)

    def test_the_other_rule34_sites_keep_their_own_names(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://rule34.xxx/index.php?page=post&s=view&id=1"), "Rule34")
        self.assertEqual(classify_site(None, "https://rule34.paheal.net/post/view/1"), "Paheal")

    def test_an_existing_config_keeps_showing_it(self):
        """Its matches used to count as "Other"; with "Other" ticked they
        were shown, so they still are."""
        loaded = self._load({"schema_version": 9, "enabled_sites": ["Danbooru", "Other"]})
        self.assertIn("Rule34.us", loaded.enabled_sites)
        from core.config import CURRENT_SCHEMA_VERSION
        self.assertEqual(loaded.schema_version, CURRENT_SCHEMA_VERSION)

    def test_a_config_that_hid_other_keeps_hiding_it(self):
        loaded = self._load({"schema_version": 9, "enabled_sites": ["Danbooru"]})
        self.assertNotIn("Rule34.us", loaded.enabled_sites)


class TestRedditSite(unittest.TestCase):
    def test_posts_and_images_are_classified_as_reddit(self):
        from core.sites import ALL_SITE_OPTIONS, classify_site
        for url in ("https://www.reddit.com/r/x/comments/1hhrx0v/t/", "https://i.redd.it/a.jpeg",
                    "https://redd.it/1hhrx0v"):
            with self.subTest(url=url):
                self.assertEqual(classify_site(None, url), "Reddit")
        self.assertIn("Reddit", ALL_SITE_OPTIONS)

    def _load(self, stored):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                return config.Settings.load()

    def test_an_existing_config_keeps_showing_it(self):
        loaded = self._load({"schema_version": 10, "enabled_sites": ["Danbooru", "Other"]})
        self.assertIn("Reddit", loaded.enabled_sites)
        from core.config import CURRENT_SCHEMA_VERSION
        self.assertEqual(loaded.schema_version, CURRENT_SCHEMA_VERSION)

    def test_a_config_that_hid_other_keeps_hiding_it(self):
        loaded = self._load({"schema_version": 10, "enabled_sites": ["Danbooru"]})
        self.assertNotIn("Reddit", loaded.enabled_sites)


class TestToon34Site(unittest.TestCase):
    """toon34.com, classification-only like e-Hentai above: its pages
    (e.g. /porn-comics/finn-x-bronwyn/) are GALLERIES holding many
    images, not a single file, so they're named to keep them out of
    "Other" rather than treated as a direct image link.
    """

    def test_a_gallery_page_is_classified_as_toon34(self):
        from core.sites import ALL_SITE_OPTIONS, classify_site
        self.assertEqual(
            classify_site(None, "https://toon34.com/porn-comics/finn-x-bronwyn/"),
            "Toon34")
        self.assertIn("Toon34", ALL_SITE_OPTIONS)

    def test_the_bare_domain_counts_too(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://www.toon34.com/porn-comics/finn-x-bronwyn/"),
            "Toon34")

    def _load(self, stored):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                return config.Settings.load()

    def test_an_existing_config_keeps_showing_it(self):
        loaded = self._load({"schema_version": 11, "enabled_sites": ["Danbooru", "Other"]})
        self.assertIn("Toon34", loaded.enabled_sites)
        from core.config import CURRENT_SCHEMA_VERSION
        self.assertEqual(loaded.schema_version, CURRENT_SCHEMA_VERSION)

    def test_a_config_that_hid_other_keeps_hiding_it(self):
        loaded = self._load({"schema_version": 11, "enabled_sites": ["Danbooru"]})
        self.assertNotIn("Toon34", loaded.enabled_sites)


class TestMangaUpdatesSite(unittest.TestCase):
    """MangaUpdates, named so it can be filtered deliberately.

    A match there is a SERIES entry (/series.html?id=...), not a page of
    the manga - no image, no booru-style tags, nothing for a parser to
    fetch. Which is exactly why someone would want to untick it, and why
    it needed a name of its own rather than sitting inside "Other".
    """

    def test_a_series_page_is_classified_as_mangaupdates(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://www.mangaupdates.com/series.html?id=100075"),
            "MangaUpdates")

    def test_the_bare_domain_counts_too(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://mangaupdates.com/series/abc/a-title"),
            "MangaUpdates")

    def test_it_is_offered_in_the_sites_menu(self):
        from core.sites import ALL_SITE_OPTIONS
        self.assertIn("MangaUpdates", ALL_SITE_OPTIONS)

    def test_it_is_not_confused_with_mangadex(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://mangadex.org/chapter/abc"), "MangaDex")

    def test_unticking_it_filters_those_matches_out(self):
        """The whole point: the Sites filter works on the classified
        name, so a named site is one the user can actually exclude."""
        from core.sites import classify_site
        enabled = {"Danbooru", "Other"}          # MangaUpdates unticked
        url = "https://www.mangaupdates.com/series.html?id=100075"
        self.assertNotIn(classify_site(None, url), enabled)

    def test_an_existing_config_has_it_added_rather_than_unchecked(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config

        stored = {"schema_version": 7, "enabled_sites": ["Danbooru", "Paheal", "Other"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)), \
                 _patch.object(config, "CONFIG_BACKUP_FILE", Path(tmp) / "config.json.bak"):
                loaded = config.Settings.load()
        self.assertIn("MangaUpdates", loaded.enabled_sites)
        self.assertIn("Paheal", loaded.enabled_sites, "earlier migrations are kept")
        self.assertIn("Danbooru", loaded.enabled_sites, "existing choices are kept")


class TestPerceptualSimilarityScale(unittest.TestCase):
    """Turning a hash distance into a percentage.

    The scale is anchored to this module's OWN bands rather than
    invented: the boundary of "looks like the same image" reads 90, the
    boundary of "similar but not identical" reads 70, and 32 - what two
    unrelated pictures average on a 64-bit hash - reads 0. It shares a
    column with IQDB's and SauceNAO's numbers, so 90+ has to mean the
    same picture here too.
    """

    def test_the_anchors_land_where_the_bands_say(self):
        from core.image_compare import (DHASH_SAME_MAX, DHASH_SIMILAR_MAX,
                                        perceptual_similarity)
        self.assertEqual(perceptual_similarity(0), 100.0)
        self.assertEqual(perceptual_similarity(DHASH_SAME_MAX), 90.0)
        self.assertEqual(perceptual_similarity(DHASH_SIMILAR_MAX), 70.0)
        self.assertEqual(perceptual_similarity(32), 0.0)

    def test_it_decreases_all_the_way_down(self):
        from core.image_compare import perceptual_similarity
        scores = [perceptual_similarity(d) for d in range(0, 33)]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_a_distance_beyond_the_scale_is_zero_not_negative(self):
        from core.image_compare import perceptual_similarity
        self.assertEqual(perceptual_similarity(64), 0.0)

    def test_no_distance_means_no_number(self):
        """Better than a number that was never measured."""
        from core.image_compare import perceptual_similarity
        self.assertIsNone(perceptual_similarity(None))

    def test_a_resize_and_a_re_encode_still_read_as_the_same_picture(self):
        """The whole point of a perceptual hash over a pixel diff: the
        two images are nearly always different resolutions, and boorus
        re-encode."""
        import io
        from PIL import Image
        from core.image_compare import dhash, hamming_distance, perceptual_similarity

        source = Image.effect_mandelbrot((256, 256), (-2, -1.5, 1, 1.5), 40).convert("RGB")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")
        original = dhash(buffer.getvalue())

        smaller = io.BytesIO()
        source.resize((128, 128)).save(smaller, format="JPEG", quality=40)
        distance = hamming_distance(original, dhash(smaller.getvalue()))
        self.assertGreaterEqual(perceptual_similarity(distance), 90.0)

    def test_a_different_picture_scores_low(self):
        import io
        from PIL import Image
        from core.image_compare import dhash, hamming_distance, perceptual_similarity

        one, two = io.BytesIO(), io.BytesIO()
        Image.effect_mandelbrot((256, 256), (-2, -1.5, 1, 1.5), 40).convert("RGB").save(
            one, format="PNG")
        Image.new("RGB", (256, 256), (128, 128, 128)).save(two, format="PNG")
        distance = hamming_distance(dhash(one.getvalue()), dhash(two.getvalue()))
        self.assertLess(perceptual_similarity(distance), 50.0)


class TestPahealSite(unittest.TestCase):
    """Paheal, recognised now that Google Lens surfaces its posts.

    Its own name rather than "Rule34": rule34.xxx and Paheal are
    different sites holding different posts, and one label for both
    would make the Sites menu lie about what is being kept.
    """

    def test_a_paheal_post_is_classified_as_paheal(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://rule34.paheal.net/post/view/5674224"), "Paheal")

    def test_it_is_offered_in_the_sites_menu(self):
        from core.sites import ALL_SITE_OPTIONS
        self.assertIn("Paheal", ALL_SITE_OPTIONS)

    def test_it_is_not_confused_with_rule34_xxx(self):
        from core.sites import classify_site
        self.assertEqual(
            classify_site(None, "https://rule34.xxx/index.php?page=post&s=view&id=1"), "Rule34")

    def test_an_existing_config_has_it_added_rather_than_unchecked(self):
        """REGRESSION GUARD for every site added since v3: a stored
        enabled_sites list predates the new name, so without a migration
        the site looks deliberately unchecked and its matches vanish."""
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config

        stored = {
            "schema_version": 6,
            "enabled_sites": ["Danbooru", "Gelbooru", "Other"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            with _patch.object(config, "CONFIG_FILE", path), \
                 _patch.object(config, "CONFIG_DIR", Path(tmp)):
                loaded = config.Settings.load()
        self.assertIn("Paheal", loaded.enabled_sites)
        self.assertIn("Danbooru", loaded.enabled_sites, "existing choices are kept")
        self.assertEqual(loaded.schema_version, config.CURRENT_SCHEMA_VERSION)


class TestConfigSurvivesAccidents(unittest.TestCase):
    """The config holds the Hydrus access key, the SauceNAO and Cloud
    Vision keys, and the site cookies. None of it can be clicked back.

    Written after a config was lost and there was nothing to restore
    from: the old save emptied the file with O_TRUNC before writing, and
    an unreadable config fell back to defaults which the next save then
    wrote straight over it.
    """

    def _sandbox(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch
        from core import config

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        patches = [
            _patch.object(config, "CONFIG_DIR", tmp),
            _patch.object(config, "CONFIG_FILE", tmp / "config.json"),
            _patch.object(config, "CONFIG_BACKUP_FILE", tmp / "config.json.bak"),
            _patch.object(config, "CONFIG_BROKEN_FILE", tmp / "config.json.unreadable"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return tmp, config

    def test_saving_keeps_the_previous_version_as_a_backup(self):
        tmp, config = self._sandbox()
        first = config.Settings()
        first.hydrus.access_key = "the-real-key"
        first.save()

        second = config.Settings()          # as if defaults were saved over it
        second.save()

        import json
        backup = json.loads((tmp / "config.json.bak").read_text())
        self.assertEqual(backup["hydrus"]["access_key"], "the-real-key",
                         "the previous config must still be recoverable")

    def test_a_save_never_leaves_a_half_written_file(self):
        """os.replace is atomic: the config is the old content or the new
        one, never the empty file O_TRUNC left behind mid-write."""
        import inspect
        from core import config
        source = inspect.getsource(config.Settings.save)
        self.assertIn("os.replace", source)
        self.assertNotIn("O_TRUNC | ", source.replace("\n", " "))
        self.assertIn("fsync", source, "the rename must not outrun the content")

    def test_an_unreadable_config_falls_back_to_the_backup(self):
        tmp, config = self._sandbox()
        good = config.Settings()
        good.hydrus.access_key = "the-real-key"
        good.save()
        good.save()                          # now there is a backup too
        (tmp / "config.json").write_text("{ this is not json", encoding="utf-8")

        loaded = config.Settings.load()
        self.assertEqual(loaded.hydrus.access_key, "the-real-key")

    def test_an_unreadable_config_is_kept_when_there_is_no_backup(self):
        """It is the only copy of those credentials, so it must not be
        left where the next save will overwrite it."""
        tmp, config = self._sandbox()
        (tmp / "config.json").write_text("{ not json either", encoding="utf-8")

        loaded = config.Settings.load()
        self.assertEqual(loaded.hydrus.access_key, "")     # defaults, as before
        self.assertTrue((tmp / "config.json.unreadable").exists())
        self.assertIn("not json either", (tmp / "config.json.unreadable").read_text())

    def test_the_backup_is_not_world_readable(self):
        tmp, config = self._sandbox()
        config.Settings().save()
        config.Settings().save()
        mode = (tmp / "config.json.bak").stat().st_mode & 0o777
        self.assertEqual(mode & 0o077, 0, "a copy of the credentials must stay private")


class TestConfigPermissions(unittest.TestCase):
    """The config file holds the Hydrus access key, the SauceNAO key, and
    whole logged-in session cookies for Pixiv, Sankaku, DeviantArt and
    Anime-Pictures. Those cookies are passwords: anyone who can read the
    file is logged in as the user.

    It was written with the default umask, which on most systems means
    -rw-r--r-- - readable by every account on the machine.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from core import config
        self.config = config
        self.tmp = Path(tempfile.mkdtemp(prefix="hatate-perm-"))
        self._saved = (config.CONFIG_DIR, config.CONFIG_FILE)
        config.CONFIG_DIR = self.tmp / "hatate-linux"
        config.CONFIG_FILE = config.CONFIG_DIR / "config.json"

    def tearDown(self):
        import shutil
        self.config.CONFIG_DIR, self.config.CONFIG_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mode(self, path):
        return path.stat().st_mode & 0o777

    def test_a_saved_config_is_readable_only_by_its_owner(self):
        self.config.Settings().save()
        self.assertEqual(self._mode(self.config.CONFIG_FILE), 0o600,
                         "the file holding the Hydrus key and site cookies is not private")

    def test_the_directory_is_private_too(self):
        """It also holds the session database, which records every file
        path the user has tagged."""
        self.config.Settings().save()
        self.assertEqual(self._mode(self.config.CONFIG_DIR), 0o700)

    def test_an_existing_loose_config_is_tightened_on_load(self):
        """A config written before this existed is world-readable right
        now. A user who never changes a setting would never trigger a
        save, so loading has to fix it."""
        import os
        settings = self.config.Settings()
        settings.save()
        os.chmod(self.config.CONFIG_FILE, 0o644)      # how it used to be written
        self.assertEqual(self._mode(self.config.CONFIG_FILE), 0o644)
        self.config.Settings.load()
        self.assertEqual(self._mode(self.config.CONFIG_FILE), 0o600)

    def test_rewriting_an_existing_config_keeps_it_private(self):
        """O_CREAT's mode applies only when the file is created, so a
        second save must not silently leave the old mode in place."""
        import os
        self.config.Settings().save()
        os.chmod(self.config.CONFIG_FILE, 0o666)
        self.config.Settings().save()
        self.assertEqual(self._mode(self.config.CONFIG_FILE), 0o600)

    def test_the_secrets_really_do_round_trip(self):
        """Guards against the permission work quietly breaking the write:
        a config that saves securely but loses its contents is worse than
        the bug it fixed."""
        settings = self.config.Settings()
        settings.hydrus.access_key = "hydrus-key"
        settings.saucenao.api_key = "saucenao-key"
        settings.pixiv_session_cookie = "PHPSESSID=secret"
        settings.save()
        loaded = self.config.Settings.load()
        self.assertEqual(loaded.hydrus.access_key, "hydrus-key")
        self.assertEqual(loaded.saucenao.api_key, "saucenao-key")
        self.assertEqual(loaded.pixiv_session_cookie, "PHPSESSID=secret")

    def test_saving_still_works_where_permissions_cannot_be_set(self):
        """On a filesystem without POSIX permissions the chmod raises.
        That is worth warning about, never worth losing the user's
        settings over."""
        import core.config as config

        def refuse(path, mode):
            raise OSError("read-only filesystem")

        original = config.os.chmod
        config.os.chmod = refuse
        try:
            config.Settings().save()
        finally:
            config.os.chmod = original
        self.assertTrue(config.CONFIG_FILE.exists(), "settings were lost")


class TestResetResult(unittest.TestCase):
    """Putting an entry back to never-searched.

    The interesting part is what SURVIVES: a reset discards this search's
    findings, not everything the app knows about the file."""

    def _entry(self, **kw):
        from core.models import ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource
        e = ImageEntry(path="/tmp/reset.png")
        candidate = MatchCandidate(
            url="https://danbooru.donmai.us/posts/1", similarity=93.0,
            source_name="Danbooru", engine="IQDB", width=2000, height=3000,
        )
        e.candidates = [candidate]
        e.select_candidate(0)
        e.status = MatchStatus.GOOD
        e.result_source = "fresh"
        e.last_searched = 1000.0
        e.error_message = "some earlier error"
        e.local_width, e.local_height = 800, 600
        e.hydrus_hash = "abc123"
        e.tags = [
            Tag("mine", TagSource.USER),
            Tag("from_hydrus", TagSource.HYDRUS),
            Tag("from_booru", TagSource.BOORU),
            Tag("from_engine", TagSource.SEARCH_ENGINE),
            Tag("auto", TagSource.HATATE),
        ]
        for k, v in kw.items():
            setattr(e, k, v)
        return e

    def test_the_match_is_gone(self):
        from core.models import MatchStatus
        e = self._entry()
        e.reset_result()
        self.assertEqual(e.status, MatchStatus.NOT_SEARCHED)
        self.assertEqual(e.candidates, [])
        self.assertIsNone(e.matched_url)
        self.assertIsNone(e.booru_name)
        self.assertIsNone(e.similarity)
        self.assertIsNone(e.result_source)
        self.assertIsNone(e.last_searched)
        self.assertIsNone(e.error_message)

    def test_user_and_hydrus_tags_survive(self):
        """A tag someone typed, or one Hydrus already holds, was never
        this search's to remove. Hydrus's especially: dropping it here
        cannot remove it from Hydrus, so hiding it would just misreport
        the library."""
        e = self._entry()
        e.reset_result()
        self.assertEqual(sorted(t.name for t in e.tags), ["from_hydrus", "mine"])

    def test_search_derived_tags_are_dropped(self):
        from core.models import TagSource
        e = self._entry()
        e.reset_result()
        sources = {t.source for t in e.tags}
        for gone in (TagSource.BOORU, TagSource.SEARCH_ENGINE, TagSource.HATATE):
            self.assertNotIn(gone, sources)

    def test_having_been_sent_to_hydrus_survives(self):
        """Whether Hydrus holds the file is a fact about the library, not
        about this search. Clearing it would claim a file had never been
        sent while it sits in Hydrus."""
        e = self._entry(sent_to_hydrus=True, hydrus_import_confirmed=True)
        e.reset_result()
        self.assertTrue(e.sent_to_hydrus)
        self.assertTrue(e.hydrus_import_confirmed)

    def test_facts_about_the_local_file_survive(self):
        """The hash and dimensions describe the file, not the match -
        and keeping the hash spares a re-search from reading it again."""
        e = self._entry()
        e.reset_result()
        self.assertEqual(e.hydrus_hash, "abc123")
        self.assertEqual((e.local_width, e.local_height), (800, 600))
        self.assertEqual(e.path, "/tmp/reset.png")

    def test_resetting_twice_is_harmless(self):
        e = self._entry()
        e.reset_result()
        e.reset_result()
        self.assertFalse(e.has_result())

    def test_a_reset_entry_can_be_searched_again(self):
        """It has to look exactly like a freshly-added file, or the
        search worker would skip it."""
        from core.models import MatchStatus
        e = self._entry()
        e.reset_result()
        self.assertEqual(e.status, MatchStatus.NOT_SEARCHED)
        self.assertFalse(e.has_result())

    def test_the_upscale_verdict_is_cleared(self):
        """REGRESSION GUARD: the verdict's source-comparison half is
        derived from the match this reset just discarded, so keeping it
        would go on naming a match that no longer exists as evidence."""
        e = self._entry(upscale_verdict="flagged", upscale_check_detail="⚠ some earlier finding")
        e.reset_result()
        self.assertIsNone(e.upscale_verdict)
        self.assertIsNone(e.upscale_check_detail)


class TestHasResult(unittest.TestCase):
    """Tells an entry that would lose something by being reset from one
    where resetting is a no-op - which is what greys the menu item out."""

    def _fresh(self):
        from core.models import ImageEntry
        return ImageEntry(path="/tmp/x.png")

    def test_a_new_entry_has_no_result(self):
        self.assertFalse(self._fresh().has_result())

    def test_a_searched_entry_has_one(self):
        from core.models import MatchStatus
        e = self._fresh()
        e.status = MatchStatus.GOOD
        self.assertTrue(e.has_result())

    def test_not_found_counts_as_a_result(self):
        """"Searched and found nothing" is a real outcome that a reset
        discards - resetting it means the next run searches again."""
        from core.models import MatchStatus
        e = self._fresh()
        e.status = MatchStatus.NOT_FOUND
        self.assertTrue(e.has_result())

    def test_an_error_counts_as_a_result(self):
        from core.models import MatchStatus
        e = self._fresh()
        e.status = MatchStatus.ERROR
        self.assertTrue(e.has_result())

    def test_a_recorded_search_time_counts(self):
        """Covers an entry searched to a result that was then filtered
        away, which leaves the status alone but did use a request."""
        e = self._fresh()
        e.last_searched = 1000.0
        self.assertTrue(e.has_result())


class TestDropCachedResult(unittest.TestCase):
    """A reset entry must not be handed back the result it just
    discarded the next time a search runs."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from core import search_cache
        self.search_cache = search_cache
        self.tmp = Path(tempfile.mkdtemp(prefix="hatate-cache-"))
        self._saved = search_cache.SEARCH_CACHE_DIR
        search_cache.SEARCH_CACHE_DIR = self.tmp

    def tearDown(self):
        import shutil
        self.search_cache.SEARCH_CACHE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, file_hash):
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / f"{file_hash}.json").write_text("{}", encoding="utf-8")

    def test_an_existing_entry_is_removed(self):
        self._write("abc")
        self.assertTrue(self.search_cache.drop_cached_result("abc"))
        self.assertFalse((self.tmp / "abc.json").exists())

    def test_a_missing_entry_reports_nothing_dropped(self):
        """Not an error: an entry searched with the cache off never had
        one, and the count shown to the user must not claim otherwise."""
        self.assertFalse(self.search_cache.drop_cached_result("never-cached"))

    def test_an_empty_hash_is_handled(self):
        """An entry added but not yet hashed has no key to look up."""
        self.assertFalse(self.search_cache.drop_cached_result(""))
        self.assertFalse(self.search_cache.drop_cached_result(None))

    def test_only_the_named_entry_goes(self):
        self._write("keep")
        self._write("drop")
        self.search_cache.drop_cached_result("drop")
        self.assertTrue((self.tmp / "keep.json").exists())
        self.assertFalse((self.tmp / "drop.json").exists())


class TestEntryFilter(unittest.TestCase):
    """What a filter matches. A filter only changes what is DISPLAYED, so
    the risk it carries is not losing entries but an action reaching a row
    the user cannot see - covered at the window level in test_gui_smoke."""

    def _entry(self, name, status=None, url=None, source=None):
        from core.models import ImageEntry, MatchCandidate
        e = ImageEntry(path=f"/tmp/{name}")
        if status is not None:
            e.status = status
        if url:
            e.candidates = [MatchCandidate(url=url, similarity=90.0,
                                           source_name=source, engine="IQDB")]
            e.select_candidate(0)
        return e

    def test_an_empty_filter_matches_everything(self):
        """The default state, and the one that must never hide a row."""
        from core.entry_filter import EntryFilter
        from core.models import MatchStatus
        entries = [self._entry("a.png", MatchStatus.GOOD),
                   self._entry("b.png", MatchStatus.NOT_FOUND)]
        f = EntryFilter()
        self.assertFalse(f.is_active())
        self.assertEqual(len(f.apply(entries)), 2)

    def test_filtering_by_status(self):
        from core.entry_filter import EntryFilter
        from core.models import MatchStatus
        entries = [self._entry("a.png", MatchStatus.GOOD),
                   self._entry("b.png", MatchStatus.NOT_FOUND),
                   self._entry("c.png", MatchStatus.GOOD)]
        f = EntryFilter(statuses={MatchStatus.GOOD})
        self.assertEqual([e.filename for e in f.apply(entries)], ["a.png", "c.png"])

    def test_several_statuses_at_once(self):
        """"Everything that failed OR errored" is the common ask, which is
        why these are multi-select rather than one-at-a-time."""
        from core.entry_filter import EntryFilter
        from core.models import MatchStatus
        entries = [self._entry("a.png", MatchStatus.GOOD),
                   self._entry("b.png", MatchStatus.NOT_FOUND),
                   self._entry("c.png", MatchStatus.ERROR)]
        f = EntryFilter(statuses={MatchStatus.NOT_FOUND, MatchStatus.ERROR})
        self.assertEqual([e.filename for e in f.apply(entries)], ["b.png", "c.png"])

    def test_filtering_by_site(self):
        from core.entry_filter import EntryFilter
        entries = [self._entry("a.png", url="https://danbooru.donmai.us/posts/1"),
                   self._entry("b.png", url="https://gelbooru.com/index.php?id=2"),
                   self._entry("c.png", url="https://danbooru.donmai.us/posts/3")]
        f = EntryFilter(sites={"Danbooru"})
        self.assertEqual([e.filename for e in f.apply(entries)], ["a.png", "c.png"])

    def test_entries_with_no_match_have_their_own_site(self):
        """Distinct from "Other", which means a match from a site with no
        name of its own - a real result, and a different thing to look
        for."""
        from core.entry_filter import EntryFilter, NO_SITE, site_of
        matched = self._entry("a.png", url="https://danbooru.donmai.us/posts/1")
        unmatched = self._entry("b.png")
        self.assertEqual(site_of(unmatched), NO_SITE)
        f = EntryFilter(sites={NO_SITE})
        self.assertEqual([e.filename for e in f.apply([matched, unmatched])], ["b.png"])

    def test_filtering_by_filename_text(self):
        from core.entry_filter import EntryFilter
        entries = [self._entry("holiday_01.png"), self._entry("work.png")]
        self.assertEqual(
            [e.filename for e in EntryFilter(text="holiday").apply(entries)],
            ["holiday_01.png"])

    def test_text_matching_ignores_case_and_padding(self):
        from core.entry_filter import EntryFilter
        entries = [self._entry("Holiday.png")]
        self.assertEqual(len(EntryFilter(text="  HOLIDAY ").apply(entries)), 1)

    def test_blank_text_is_not_a_filter(self):
        """REGRESSION GUARD: treating whitespace as a needle would hide
        every row the moment someone typed a space."""
        from core.entry_filter import EntryFilter
        self.assertFalse(EntryFilter(text="   ").is_active())
        self.assertEqual(len(EntryFilter(text="   ").apply([self._entry("a.png")])), 1)

    def test_filters_combine(self):
        from core.entry_filter import EntryFilter
        from core.models import MatchStatus
        entries = [
            self._entry("keep.png", MatchStatus.GOOD, "https://danbooru.donmai.us/posts/1"),
            self._entry("wrong_status.png", MatchStatus.POOR, "https://danbooru.donmai.us/posts/2"),
            self._entry("wrong_site.png", MatchStatus.GOOD, "https://gelbooru.com/index.php?id=3"),
            self._entry("wrong_name.png", MatchStatus.GOOD, "https://danbooru.donmai.us/posts/4"),
        ]
        f = EntryFilter(statuses={MatchStatus.GOOD}, sites={"Danbooru"}, text="keep")
        self.assertEqual([e.filename for e in f.apply(entries)], ["keep.png"])

    def test_an_empty_set_is_not_the_same_as_no_filter(self):
        """None means "not filtering on this"; an empty set means "nothing
        is allowed". Conflating them would make "untick everything" show
        the whole list instead of none of it."""
        from core.entry_filter import EntryFilter
        from core.models import MatchStatus
        entries = [self._entry("a.png", MatchStatus.GOOD)]
        self.assertEqual(len(EntryFilter(statuses=None).apply(entries)), 1)
        self.assertEqual(len(EntryFilter(statuses=set()).apply(entries)), 0)

    def test_only_the_selected_candidates_site_counts(self):
        """The list shows one match per row, so filtering on a site the
        user cannot see in the Booru column would be baffling."""
        from core.entry_filter import EntryFilter, site_of
        from core.models import MatchCandidate
        e = self._entry("a.png", url="https://danbooru.donmai.us/posts/1")
        e.candidates.append(MatchCandidate(url="https://gelbooru.com/index.php?id=9",
                                           similarity=50.0, source_name="Gelbooru", engine="IQDB"))
        self.assertEqual(site_of(e), "Danbooru")
        self.assertEqual(len(EntryFilter(sites={"Gelbooru"}).apply([e])), 0)

    def test_filtering_by_upscale_verdict(self):
        from core.entry_filter import EntryFilter
        flagged = self._entry("a.png")
        flagged.upscale_verdict = "flagged"
        clear = self._entry("b.png")
        clear.upscale_verdict = "clear"
        never_checked = self._entry("c.png")
        f = EntryFilter(upscale_verdicts={"Flagged"})
        self.assertEqual(
            [e.filename for e in f.apply([flagged, clear, never_checked])], ["a.png"])

    def test_not_checked_is_its_own_upscale_verdict(self):
        """None on the entry means "never run", which has to be a
        selectable filter value distinct from "not filtering at all"."""
        from core.entry_filter import EntryFilter
        never_checked = self._entry("a.png")
        checked = self._entry("b.png")
        checked.upscale_verdict = "clear"
        f = EntryFilter(upscale_verdicts={"Not checked"})
        self.assertEqual(
            [e.filename for e in f.apply([never_checked, checked])], ["a.png"])


class TestFilterOptionsOffered(unittest.TestCase):
    """The menus offer only what the list actually holds, so a choice can
    never be ticked into showing nothing."""

    def _entry(self, name, status=None, url=None):
        from core.models import ImageEntry, MatchCandidate
        e = ImageEntry(path=f"/tmp/{name}")
        if status is not None:
            e.status = status
        if url:
            e.candidates = [MatchCandidate(url=url, similarity=90.0,
                                           source_name=None, engine="IQDB")]
            e.select_candidate(0)
        return e

    def test_only_present_sites_are_offered(self):
        from core.entry_filter import sites_present
        entries = [self._entry("a.png", url="https://danbooru.donmai.us/posts/1"),
                   self._entry("b.png", url="https://danbooru.donmai.us/posts/2")]
        self.assertEqual(sites_present(entries), ["Danbooru"])

    def test_no_match_sorts_last(self):
        """It is the absence of a result, not a site, so it belongs at the
        end rather than alphabetically among real ones."""
        from core.entry_filter import sites_present, NO_SITE
        entries = [self._entry("a.png"),
                   self._entry("b.png", url="https://gelbooru.com/index.php?id=1"),
                   self._entry("c.png", url="https://danbooru.donmai.us/posts/2")]
        self.assertEqual(sites_present(entries), ["Danbooru", "Gelbooru", NO_SITE])

    def test_statuses_are_offered_in_the_enums_order(self):
        """So the menu reads the same way every time, whatever is loaded."""
        from core.entry_filter import statuses_present
        from core.models import MatchStatus
        entries = [self._entry("a.png", MatchStatus.ERROR),
                   self._entry("b.png", MatchStatus.GOOD)]
        got = statuses_present(entries)
        self.assertEqual(got, [s for s in MatchStatus if s in set(got)])

    def test_an_empty_list_offers_nothing(self):
        from core.entry_filter import sites_present, statuses_present
        self.assertEqual(sites_present([]), [])
        self.assertEqual(statuses_present([]), [])

    def test_upscale_verdicts_are_offered_worst_news_first(self):
        from core.entry_filter import upscale_verdicts_present
        checked = self._entry("a.png")
        checked.upscale_verdict = "clear"
        flagged = self._entry("b.png")
        flagged.upscale_verdict = "flagged"
        never_checked = self._entry("c.png")
        self.assertEqual(
            upscale_verdicts_present([checked, flagged, never_checked]),
            ["Flagged", "Clear", "Not checked"],
        )


class TestDroppingDeadCandidates(unittest.TestCase):
    """Removing matches the site has confirmed gone. Deleting a live match
    by accident is worse than keeping a dead one, so what counts as
    "gone" is deliberately narrow - and none of this had a test while it
    lived in a GUI slot."""

    def _entry(self, *availability):
        from core.models import ImageEntry, MatchCandidate, MatchStatus
        e = ImageEntry(path="/tmp/x.png")
        e.candidates = [
            MatchCandidate(url=f"https://site/{i}", similarity=90.0 - i, remote_available=a)
            for i, a in enumerate(availability)
        ]
        e.select_candidate(0)
        e.status = MatchStatus.GOOD
        return e

    def test_only_definitely_gone_candidates_are_dropped(self):
        """None means unchecked or unreachable. Dropping those would
        delete a good match over a network hiccup."""
        entry = self._entry(True, None, False)
        removed = entry.drop_unavailable_candidates()
        self.assertEqual(removed, 1)
        self.assertEqual(len(entry.candidates), 2)

    def test_nothing_to_drop_changes_nothing(self):
        entry = self._entry(True, None)
        self.assertEqual(entry.drop_unavailable_candidates(), 0)
        self.assertEqual(len(entry.candidates), 2)

    def test_the_selection_follows_the_same_match(self):
        """Its index shifts when an EARLIER candidate is removed, so
        reusing the old number would quietly select a different match."""
        entry = self._entry(False, True, True)
        chosen = entry.candidates[1]
        entry.select_candidate(1)
        entry.drop_unavailable_candidates()
        self.assertIs(entry.selected_candidate, chosen)
        self.assertEqual(entry.selected_candidate_index, 0)

    def test_removing_the_selected_match_falls_back_to_the_best(self):
        entry = self._entry(True, False)
        entry.select_candidate(1)
        entry.drop_unavailable_candidates()
        self.assertEqual(entry.selected_candidate_index, 0)
        self.assertIs(entry.selected_candidate, entry.candidates[0])

    def test_losing_every_match_resets_to_no_match(self):
        from core.models import MatchStatus
        entry = self._entry(False, False)
        removed = entry.drop_unavailable_candidates()
        self.assertEqual(removed, 2)
        self.assertEqual(entry.candidates, [])
        self.assertEqual(entry.status, MatchStatus.NOT_FOUND)
        self.assertIsNone(entry.matched_url)
        self.assertIsNone(entry.booru_name)
        self.assertIsNone(entry.similarity)

    def test_an_entry_with_no_candidates_is_safe(self):
        from core.models import ImageEntry
        self.assertEqual(ImageEntry(path="/tmp/x.png").drop_unavailable_candidates(), 0)


class TestSauceNaoQuotaText(unittest.TestCase):
    """The status-bar reading of the daily allowance. Only ever tested for
    "does not raise" before, so none of the wording was pinned."""

    def _quota(self, remaining=None, limit=None):
        from core.saucenao import SauceNaoQuota
        return SauceNaoQuota(long_remaining=remaining, long_limit=limit)

    def test_used_out_of_limit_is_preferred(self):
        """How much of the day is GONE is what decides whether to keep
        searching."""
        from core.saucenao import describe_quota
        self.assertEqual(describe_quota(self._quota(remaining=40, limit=100)),
                         "SauceNAO: 60/100 used today")

    def test_a_missing_limit_falls_back_to_what_is_left(self):
        """SauceNAO does not always send the limit, and the remaining
        count is still the number that runs out."""
        from core.saucenao import describe_quota
        self.assertEqual(describe_quota(self._quota(remaining=40)),
                         "SauceNAO: 40 left today")

    def test_nothing_observed_yet_is_blank_not_zero(self):
        """"No search made this session" is not "no quota left"."""
        from core.saucenao import describe_quota
        self.assertEqual(describe_quota(None), "")
        self.assertEqual(describe_quota(self._quota()), "")

    def test_an_exhausted_allowance_reads_as_fully_used(self):
        from core.saucenao import describe_quota
        self.assertEqual(describe_quota(self._quota(remaining=0, limit=100)),
                         "SauceNAO: 100/100 used today")


class TestSauceNaoShortWindowText(unittest.TestCase):
    """The burst-window tooltip. Deliberately not in the label: it clears
    by itself within seconds, so showing it there would flicker during a
    batch and imply something was wrong."""

    def _quota(self, remaining=None, limit=None):
        from core.saucenao import SauceNaoQuota
        return SauceNaoQuota(short_remaining=remaining, short_limit=limit)

    def test_used_out_of_limit_when_both_are_known(self):
        from core.saucenao import describe_short_window
        self.assertEqual(describe_short_window(self._quota(remaining=2, limit=6)),
                         "4/6 used in the current ~30s window")

    def test_remaining_alone_when_the_limit_is_absent(self):
        from core.saucenao import describe_short_window
        self.assertEqual(describe_short_window(self._quota(remaining=2)),
                         "2 remaining in the current ~30s window")

    def test_nothing_known_is_blank(self):
        from core.saucenao import describe_short_window
        self.assertEqual(describe_short_window(None), "")
        self.assertEqual(describe_short_window(self._quota()), "")


class TestHydrusReconcile(unittest.TestCase):
    """Settling entries stuck at "Queued".

    The URL-importer poll gives up after a minute and nothing ever asks
    again, so a real session here held 162 entries sent to Hydrus with
    none confirmed. The Sent column is what lets a part-finished batch be
    resumed, so one that says Queued forever is the feature not working.
    """

    def _entry(self, name, sent=True, confirmed=False, file_hash="aa"):
        from core.models import ImageEntry
        e = ImageEntry(path=f"/tmp/{name}")
        e.sent_to_hydrus = sent
        e.hydrus_import_confirmed = confirmed
        e.hydrus_hash = file_hash
        return e

    def _states(self, mapping):
        return lambda hashes: {h: mapping.get(h, "unknown") for h in hashes}

    def test_a_file_hydrus_holds_is_confirmed(self):
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", file_hash="h1")
        result = reconcile([entry], self._states({"h1": "present"}))
        self.assertTrue(entry.hydrus_import_confirmed)
        self.assertEqual(result.confirmed, [entry])

    def test_a_file_hydrus_never_saw_stays_queued(self):
        """It may simply not have finished downloading yet."""
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", file_hash="h1")
        result = reconcile([entry], self._states({}))
        self.assertFalse(entry.hydrus_import_confirmed)
        self.assertEqual(result.unresolved, [entry])

    def test_a_file_deleted_in_hydrus_still_counts_as_imported(self):
        """The question is whether the SEND worked, not whether the file
        is still there - the import poll takes the same view."""
        from core.hydrus_reconcile import reconcile
        for state in ("trashed", "deleted"):
            with self.subTest(state=state):
                entry = self._entry("a.png", file_hash="h1")
                reconcile([entry], self._states({"h1": state}))
                self.assertTrue(entry.hydrus_import_confirmed)

    def test_entries_never_sent_are_not_touched(self):
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", sent=False, file_hash="h1")
        result = reconcile([entry], self._states({"h1": "present"}))
        self.assertEqual(result.checked, 0)
        self.assertFalse(entry.hydrus_import_confirmed)

    def test_already_confirmed_entries_are_not_re_asked(self):
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", confirmed=True, file_hash="h1")
        self.assertEqual(reconcile([entry], self._states({})).checked, 0)

    def test_confirmation_is_never_taken_away(self):
        """Un-confirming would lose information that was correct when it
        was recorded."""
        from core.hydrus_reconcile import reconcile
        confirmed = self._entry("a.png", confirmed=True, file_hash="h1")
        reconcile([confirmed], self._states({"h1": "unknown"}))
        self.assertTrue(confirmed.hydrus_import_confirmed)

    def test_an_entry_with_no_hash_is_left_alone(self):
        """The hash is the only handle on the file here; without one
        there is nothing to ask about, so it is not guessed at."""
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", file_hash=None)
        self.assertEqual(reconcile([entry], self._states({})).checked, 0)

    def test_two_rows_sharing_a_hash_both_get_the_answer(self):
        """The same file added twice is two rows."""
        from core.hydrus_reconcile import reconcile
        a, b = self._entry("a.png", file_hash="h1"), self._entry("copy.png", file_hash="h1")
        reconcile([a, b], self._states({"h1": "present"}))
        self.assertTrue(a.hydrus_import_confirmed)
        self.assertTrue(b.hydrus_import_confirmed)

    def test_hashes_are_asked_in_batches(self):
        """A 25,000-entry library must not become one enormous query."""
        from core.hydrus_reconcile import reconcile
        entries = [self._entry(f"{i}.png", file_hash=f"h{i}") for i in range(10)]
        sizes = []

        def states(hashes):
            sizes.append(len(hashes))
            return {h: "present" for h in hashes}

        reconcile(entries, states, batch_size=4)
        self.assertEqual(sizes, [4, 4, 2])

    def test_each_hash_is_asked_about_once(self):
        from core.hydrus_reconcile import reconcile
        entries = [self._entry("a.png", file_hash="h1"), self._entry("b.png", file_hash="h1")]
        asked = []

        def states(hashes):
            asked.extend(hashes)
            return {h: "present" for h in hashes}

        reconcile(entries, states)
        self.assertEqual(asked, ["h1"])

    def test_it_can_be_stopped_part_way(self):
        from core.hydrus_reconcile import reconcile
        entries = [self._entry(f"{i}.png", file_hash=f"h{i}") for i in range(10)]
        calls = []

        def states(hashes):
            calls.append(hashes)
            return {h: "present" for h in hashes}

        result = reconcile(entries, states, batch_size=2, should_stop=lambda: len(calls) >= 2)
        self.assertEqual(len(calls), 2)
        self.assertLess(result.checked, 10)

    def test_a_lookup_returning_nothing_is_survived(self):
        """Hydrus being unreachable must leave everything as it was
        rather than raising or un-confirming anything."""
        from core.hydrus_reconcile import reconcile
        entry = self._entry("a.png", file_hash="h1")
        result = reconcile([entry], lambda hashes: None)
        self.assertFalse(entry.hydrus_import_confirmed)
        self.assertEqual(result.checked, 1)

    def test_nothing_waiting_asks_hydrus_nothing(self):
        """Startup must not cost a request when there is nothing to
        settle."""
        from core.hydrus_reconcile import reconcile
        called = []
        result = reconcile([], lambda h: called.append(h) or {})
        self.assertEqual(called, [])
        self.assertEqual(result.checked, 0)
        self.assertIn("Nothing was waiting", result.summary)


class TestSauceNaoBurstWindow(unittest.TestCase):
    """SauceNAO reports how many requests are left in its ~30s burst
    window, and the app read that number only to render a tooltip. At a
    5s delay it fires ~6 requests per window against a limit of 17 and
    collides whenever the window is already part used - 15 real 429s in
    one library's logs, each one costing that image its primary-engine
    result permanently, because the weaker fallback gets cached."""

    def _quota(self, short_remaining):
        from core.saucenao import SauceNaoQuota
        return SauceNaoQuota(short_remaining=short_remaining, short_limit=17)

    def _wait(self, quota, observed_at, now):
        from core.saucenao import seconds_until_short_window_clears
        return seconds_until_short_window_clears(quota, observed_at, now)

    def test_headroom_means_no_waiting(self):
        """Pausing at one remaining would halve a batch's throughput to
        avoid a 429 that has not happened."""
        for remaining in (1, 5, 17):
            with self.subTest(remaining=remaining):
                self.assertEqual(self._wait(self._quota(remaining), 1000.0, 1000.0), 0.0)

    def test_an_exhausted_window_waits_the_rest_of_it(self):
        from core.saucenao import SHORT_WINDOW_SECONDS
        wait = self._wait(self._quota(0), observed_at=1000.0, now=1010.0)
        self.assertAlmostEqual(wait, SHORT_WINDOW_SECONDS - 10.0)

    def test_a_window_that_has_already_passed_needs_no_wait(self):
        self.assertEqual(self._wait(self._quota(0), observed_at=1000.0, now=1100.0), 0.0)

    def test_it_never_asks_for_a_negative_wait(self):
        self.assertGreaterEqual(self._wait(self._quota(0), 1000.0, 9999.0), 0.0)

    def test_nothing_observed_yet_does_not_wait(self):
        """A first search must not be delayed by a counter nobody has
        read."""
        self.assertEqual(self._wait(None, 0.0, 1000.0), 0.0)
        self.assertEqual(self._wait(self._quota(None), 0.0, 1000.0), 0.0)

    def test_the_daily_limit_is_not_this_function_s_business(self):
        """It does not clear by waiting, and is_daily_quota_exhausted
        handles it - so a spent DAY with burst headroom still returns 0."""
        from core.saucenao import SauceNaoQuota
        quota = SauceNaoQuota(short_remaining=5, short_limit=17,
                              long_remaining=0, long_limit=5000)
        self.assertEqual(self._wait(quota, 1000.0, 1000.0), 0.0)


class TestSauceNaoRetriesA429(unittest.TestCase):
    """Giving up on a 429 is not free: the image falls through to the
    secondary engine, gets a weaker result, and that result is cached and
    marked searched - so the primary-engine answer is lost permanently."""

    def setUp(self):
        import core.saucenao as sn
        self.sn = sn
        sn._last_quota = None
        sn._last_quota_at = 0.0
        self.addCleanup(setattr, sn, "_last_quota", None)

    def _settings(self):
        from core.config import SauceNaoSettings
        return SauceNaoSettings(api_key="k", use_json_api=True)

    def _response(self, status, payload=None):
        from unittest.mock import MagicMock
        r = MagicMock(status_code=status)
        r.json.return_value = payload or {
            "header": {"status": 0, "short_remaining": 5, "short_limit": 17,
                       "long_remaining": 100, "long_limit": 5000},
            "results": [],
        }
        return r

    def test_a_429_is_retried_once_after_waiting(self):
        from unittest.mock import patch
        responses = [self._response(429), self._response(200)]
        slept = []
        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", side_effect=lambda *a, **k: responses.pop(0)), \
             patch("core.saucenao._sleep_interruptibly",
                   side_effect=lambda secs, *a, **k: slept.append(secs)):
            result = self.sn.search("/tmp/x.jpg", self._settings())
        self.assertEqual(result, [])          # got through on the retry
        self.assertEqual(responses, [])       # both responses consumed
        self.assertEqual(len(slept), 1, "it should wait exactly one burst window")
        self.assertAlmostEqual(slept[0], self.sn.SHORT_WINDOW_SECONDS)

    def test_a_second_429_gives_up_rather_than_looping(self):
        from unittest.mock import patch
        responses = [self._response(429), self._response(429)]
        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", side_effect=lambda *a, **k: responses.pop(0)), \
             patch("core.saucenao._sleep_interruptibly"):
            with self.assertRaises(self.sn.SauceNaoError):
                self.sn.search("/tmp/x.jpg", self._settings())

    def test_a_successful_search_never_sleeps(self):
        """The common path must not have gained a delay."""
        from unittest.mock import patch
        slept = []
        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", return_value=self._response(200)), \
             patch("core.saucenao._sleep_interruptibly",
                   side_effect=lambda secs, *a, **k: slept.append(secs)):
            self.sn.search("/tmp/x.jpg", self._settings())
        self.assertEqual(slept, [])

    def test_an_exhausted_window_is_waited_out_before_asking(self):
        from unittest.mock import patch
        import time as _time
        self.sn._last_quota = self.sn.SauceNaoQuota(short_remaining=0, short_limit=17)
        self.sn._last_quota_at = _time.time()
        slept = []
        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", return_value=self._response(200)), \
             patch("core.saucenao._sleep_interruptibly",
                   side_effect=lambda secs, *a, **k: slept.append(secs)):
            self.sn.search("/tmp/x.jpg", self._settings())
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)

    def test_the_quota_reading_is_timestamped(self):
        """"5 left" is only true at a moment, so when it was read is part
        of the reading."""
        from unittest.mock import patch
        before = self.sn._last_quota_at
        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", return_value=self._response(200)):
            self.sn.search("/tmp/x.jpg", self._settings())
        self.assertGreater(self.sn._last_quota_at, before)
        self.assertEqual(self.sn._last_quota.short_remaining, 5)


class TestBurstWaitAnswersStop(unittest.TestCase):
    """The batch worker checks its stop flag every quarter second while
    pacing between images. A wait here that ignored it would make Stop
    appear not to work for up to half a minute."""

    def test_a_stop_ends_the_wait_immediately(self):
        import time
        from core.saucenao import _sleep_interruptibly
        started = time.monotonic()
        _sleep_interruptibly(30.0, "test", None, should_stop=lambda: True)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_it_waits_when_nothing_asks_it_to_stop(self):
        import time
        from core.saucenao import _sleep_interruptibly
        started = time.monotonic()
        _sleep_interruptibly(0.4, "test", None, should_stop=lambda: False)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_no_stop_callable_is_allowed(self):
        from core.saucenao import _sleep_interruptibly
        _sleep_interruptibly(0.05, "test", None, should_stop=None)

    def test_stopping_mid_retry_does_not_fire_a_second_request(self):
        """Waiting out a 429 and then asking anyway would spend a request
        the user has just cancelled."""
        from unittest.mock import MagicMock, patch
        import core.saucenao as sn
        from core.config import SauceNaoSettings
        calls = []

        def fake_post(*args, **kwargs):
            calls.append(1)
            return MagicMock(status_code=429)

        with patch("core.saucenao.prepare_upload_bytes", return_value=(b"x", "f.jpg")), \
             patch("core.saucenao.net.post", side_effect=fake_post), \
             patch("core.saucenao._sleep_interruptibly"):
            with self.assertRaises(sn.SauceNaoError):
                sn.search("/tmp/x.jpg", SauceNaoSettings(api_key="k", use_json_api=True),
                          should_stop=lambda: True)
        self.assertEqual(len(calls), 1, "the retry must not run after a stop")


class TestImageDifferenceMap(unittest.TestCase):
    """Highlighting WHERE two copies of a picture differ - a watermark, a
    caption, a censor bar. Deliberately structural: the two are almost
    always different resolutions, so a pixel-exact diff would light the
    whole frame with re-encoding noise and bury the real change."""

    def _scene(self, size=(800, 600)):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", size, (35, 80, 130))
        d = ImageDraw.Draw(im)
        d.ellipse([80, 80, 380, 380], fill=(225, 95, 70))
        d.rectangle([500, 300, 760, 540], fill=(40, 200, 140))
        return im

    def _diff(self, a, b):
        from core.image_diff import difference_map
        return difference_map(a, b)

    def test_a_picture_against_itself_shows_nothing(self):
        base = self._scene()
        self.assertEqual(self._diff(base, base.copy()).changed_fraction, 0.0)

    def test_a_resize_and_re_encode_is_not_a_difference(self):
        """The case that makes or breaks this. A match is nearly always a
        different size and a different JPEG; if that reads as "changed"
        the view is useless."""
        import io
        from PIL import Image
        base = self._scene()
        buf = io.BytesIO()
        base.resize((1600, 1200), Image.LANCZOS).save(buf, "JPEG", quality=70)
        buf.seek(0)
        result = self._diff(base, Image.open(buf))
        self.assertLess(result.changed_percent, 1.0,
                        f"re-encoding alone read as {result.changed_percent:.1f}% changed")

    def test_a_watermark_is_found(self):
        from PIL import ImageDraw
        base = self._scene()
        marked = base.copy()
        ImageDraw.Draw(marked).rectangle([600, 520, 780, 580], fill=(255, 255, 255))
        result = self._diff(base, marked)
        self.assertGreater(result.changed_percent, 0.5)
        self.assertTrue(result.reliable)
        self.assertIn("watermark", result.verdict)

    def test_the_mask_marks_where_the_change_is(self):
        """Not just that something changed - the highlight has to land on
        the right part of the picture."""
        import numpy as np
        from PIL import ImageDraw
        base = self._scene()
        marked = base.copy()
        ImageDraw.Draw(marked).rectangle([600, 480, 780, 580], fill=(255, 255, 255))
        mask = np.asarray(self._diff(base, marked).mask, dtype=float)
        h, w = mask.shape
        bottom_right = mask[int(h * 0.7):, int(w * 0.6):].mean()
        top_left = mask[:int(h * 0.4), :int(w * 0.4)].mean()
        self.assertGreater(bottom_right, top_left * 5 + 1,
                           "the highlight is not where the change was made")

    def test_a_wholesale_difference_declines_to_explain_itself(self):
        """Past a point this view cannot say WHAT changed, and a
        uniformly red rectangle is not a result."""
        from PIL import Image, ImageDraw
        a = self._scene()
        b = Image.new("RGB", (800, 600), (200, 180, 120))
        ImageDraw.Draw(b).polygon([(400, 60), (700, 500), (120, 520)], fill=(60, 60, 200))
        result = self._diff(a, b)
        self.assertFalse(result.reliable)
        self.assertIn("perceptual match", result.verdict)

    def test_different_shapes_are_refused_rather_than_distorted(self):
        """Squeezing one into the other's box misaligns every edge, so
        the highlights would be the mismatch, not an edit."""
        result = self._diff(self._scene((800, 600)), self._scene((600, 800)))
        self.assertFalse(result.reliable)
        self.assertIn("different shapes", result.verdict)

    def test_a_global_brightness_shift_is_not_an_edit(self):
        """Two encodes can sit a few levels apart overall. Without
        levelling, that constant offset would light the entire frame."""
        from PIL import ImageEnhance
        base = self._scene()
        brighter = ImageEnhance.Brightness(base).enhance(1.08)
        self.assertLess(self._diff(base, brighter).changed_percent, 5.0)

    def test_a_recolour_at_the_same_brightness_is_found(self):
        """REGRESSION: the first version compared luminance only, so two
        colours of similar brightness cancelled out and a recolour was
        invisible. Reported against a real pair, where the red channel
        differed by a 99th-percentile of 146 levels while luminance showed
        106."""
        from PIL import ImageDraw
        base = self._scene()
        recoloured = base.copy()
        # Two fills chosen to be close in luminance and far apart in hue:
        # greyscale sees almost nothing between them.
        ImageDraw.Draw(base).rectangle([100, 420, 400, 560], fill=(190, 90, 90))
        ImageDraw.Draw(recoloured).rectangle([100, 420, 400, 560], fill=(90, 150, 90))
        result = self._diff(base, recoloured)
        self.assertGreater(result.changed_percent, 1.0,
                           "a colour-only change went unnoticed")

    def test_the_strongest_channel_decides_not_the_average(self):
        """A change confined to one channel is diluted by two-thirds if
        the channels are averaged - which is what a recolour is."""
        from PIL import ImageDraw
        base = self._scene()
        red_only = base.copy()
        ImageDraw.Draw(red_only).rectangle([120, 430, 380, 550], fill=(240, 80, 130))
        ImageDraw.Draw(base).rectangle([120, 430, 380, 550], fill=(80, 80, 130))
        self.assertGreater(self._diff(base, red_only).changed_percent, 1.0)

    def test_a_saturation_shift_is_not_an_edit(self):
        """Colour sensitivity must not turn every profile difference into
        a finding."""
        from PIL import ImageEnhance
        base = self._scene()
        self.assertLess(
            self._diff(base, ImageEnhance.Color(base).enhance(1.10)).changed_percent, 5.0)

    def test_a_missing_image_is_handled(self):
        base = self._scene()
        for a, b in ((None, base), (base, None), (None, None)):
            with self.subTest():
                result = self._diff(a, b)
                self.assertIsNone(result.mask)
                self.assertFalse(result.reliable)

    def test_a_small_image_is_not_upscaled(self):
        """Upscaling to the working size would invent detail and compare
        interpolation artefacts."""
        small = self._scene((120, 90))
        result = self._diff(small, small.copy())
        self.assertEqual(result.mask.size, (120, 90))


class TestContinuingWithoutSauceNao(unittest.TestCase):
    """When SauceNAO's DAILY allowance runs out the batch stops, and the
    reasoning is sound - every remaining image would be recorded with a
    weaker result, cached, and marked searched. This makes the weaker
    result temporary instead, so continuing stops being a trap."""

    def _settings(self, primary="saucenao", mode="fallback"):
        from core.config import Settings
        s = Settings()
        s.primary_engine = primary
        s.secondary_engine_mode = mode
        return s

    def test_skipping_removes_saucenao_from_the_plan(self):
        from core.engines import IQDB, SAUCENAO
        from core.search_engine import planned_engines
        s = self._settings()
        self.assertIn(SAUCENAO, planned_engines(s))
        after = planned_engines(s, skip_saucenao=True)
        self.assertNotIn(SAUCENAO, after)
        self.assertIn(IQDB, after, "IQDB has no daily cap and must still run")

    def test_skipping_leaves_no_empty_wave(self):
        """With SauceNAO as the primary, dropping it can empty a wave -
        and the pacing counts waves as hosts about to be queried."""
        from core.search_engine import _engine_waves
        waves = _engine_waves(self._settings("saucenao", "fallback"), None, skip_saucenao=True)
        self.assertTrue(all(waves), f"an empty wave survived: {waves}")
        self.assertTrue(waves, "everything was dropped")

    def test_the_extra_engines_are_kept(self):
        from core.engines import ASCII2D
        from core.search_engine import planned_engines
        s = self._settings()
        s.enable_ascii2d = True
        self.assertIn(ASCII2D, planned_engines(s, skip_saucenao=True))

    def test_a_provisional_result_is_not_cached(self):
        """The whole safeguard. Caching it would hand back the weaker
        answer tomorrow as if it were the real one."""
        import importlib, os, tempfile
        os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="hatate-prov-")
        import core.paths
        importlib.reload(core.paths)
        import core.search_cache
        importlib.reload(core.search_cache)
        from core.models import ImageEntry, MatchCandidate, MatchStatus

        entry = ImageEntry(path="/tmp/p.png")
        entry.status = MatchStatus.GOOD
        entry.candidates = [MatchCandidate(url="https://danbooru.donmai.us/posts/1")]
        entry.select_candidate(0)
        entry.searched_without_saucenao = True
        core.search_cache.save_cached_result("ee" * 32, entry)
        self.assertIsNone(core.search_cache.load_cached_result("ee" * 32))

        entry.searched_without_saucenao = False
        core.search_cache.save_cached_result("ee" * 32, entry)
        self.assertIsNotNone(core.search_cache.load_cached_result("ee" * 32),
                             "a complete result must still be cached")

    def test_resetting_clears_the_provisional_mark(self):
        from core.models import ImageEntry
        entry = ImageEntry(path="/tmp/p.png")
        entry.searched_without_saucenao = True
        entry.reset_result()
        self.assertFalse(entry.searched_without_saucenao)

    def test_the_mark_survives_a_session_round_trip(self):
        """A restart that forgot would leave them looking finished, and
        they would never be run again."""
        from core.models import ImageEntry, MatchStatus
        from core.session import _entry_from_dict, _entry_to_dict
        entry = ImageEntry(path="/tmp/p.png")
        entry.status = MatchStatus.GOOD
        entry.searched_without_saucenao = True
        restored = _entry_from_dict(_entry_to_dict(entry))
        self.assertTrue(restored.searched_without_saucenao)

    def test_the_cache_column_calls_it_provisional(self):
        from core.models import ImageEntry
        from gui.image_table_model import _entry_cache_label
        entry = ImageEntry(path="/tmp/p.png")
        entry.result_source = "fresh"
        self.assertEqual(_entry_cache_label(entry), "Fresh")
        entry.searched_without_saucenao = True
        self.assertEqual(_entry_cache_label(entry), "Provisional")


class TestRunEstimate(unittest.TestCase):
    """The search run's "how much longer" estimate.

    The cases that matter are the two shapes of wrongness a naive version
    has: reading far too optimistic at the start, and swinging wildly when
    the run hits a cluster of cached images.
    """

    def setUp(self):
        from core.eta import RunEstimate
        self.est = RunEstimate()

    def test_says_nothing_until_it_has_two_intervals(self):
        """One sample of a randomised 45-75s delay is not a pace. Quoting
        a finish date off it would be a guess wearing a number's clothes."""
        self.assertEqual(self.est.seconds_remaining(), -1.0)
        self.est.record(1, 100, now=0.0)
        self.assertEqual(self.est.seconds_remaining(), -1.0)
        self.est.record(2, 100, now=60.0)
        self.assertEqual(self.est.seconds_remaining(), -1.0)
        self.est.record(3, 100, now=120.0)
        self.assertEqual(self.est.seconds_remaining(), 97 * 60.0)

    def test_the_leading_search_does_not_drag_the_pace_down(self):
        """The worker emits progress AFTER an image's search and BEFORE
        that image's rate-limit wait. Dividing elapsed-since-start by
        images-done therefore misses one whole wait, and at 60s a wait
        that reads as 5s/image on the first sample - an estimate an order
        of magnitude short exactly when someone first looks at it.
        Measuring the interval BETWEEN progress signals is immune: here
        the first signal lands 5s in (the search alone) and every cycle
        after it is a full 65s."""
        self.est.record(1, 101, now=5.0)
        self.est.record(2, 101, now=70.0)
        self.est.record(3, 101, now=135.0)
        self.assertEqual(self.est.seconds_per_image(), 65.0)
        self.assertEqual(self.est.seconds_remaining(), 98 * 65.0)

    def test_a_cluster_of_cached_images_does_not_collapse_the_estimate(self):
        """An image served from the search cache skips the rate-limit
        wait, and cache hits arrive in clusters (a re-imported folder).
        A trailing-window average sitting inside one would report minutes
        remaining for a run with days left, then leap back. Averaging the
        whole run means the cluster moves the pace only as far as its real
        share of the run."""
        t = 0.0
        for i in range(1, 21):          # 20 images at a full 60s cycle
            self.est.record(i, 120, now=t)
            t += 60.0
        steady = self.est.seconds_per_image()
        self.assertAlmostEqual(steady, 60.0, places=6)
        for i in range(21, 41):         # 20 cache hits, near-instant
            self.est.record(i, 120, now=t)
            t += 0.05
        # 39 intervals spanning 1200.95s: nineteen 60s cycles, the 60s
        # cycle of the last slow image, then twenty near-instant ones.
        # ~30.8s/image - half the steady pace, not the ~0 a trailing
        # window sitting inside the cluster would report.
        blended = self.est.seconds_per_image()
        self.assertAlmostEqual(blended, 1200.95 / 39, places=6)
        self.assertGreater(blended, steady / 3)

    def test_it_follows_total_growing_when_retries_are_queued(self):
        """`total` grows once mid-run, when images that failed on a
        network fault are queued for their one retry. The estimate has to
        cover the images that appeared, not the total it first saw."""
        self.est.record(1, 10, now=0.0)
        self.est.record(2, 10, now=60.0)
        self.est.record(3, 10, now=120.0)
        self.assertEqual(self.est.seconds_remaining(), 7 * 60.0)
        self.est.record(4, 14, now=180.0)   # four retries queued
        self.assertEqual(self.est.seconds_remaining(), 10 * 60.0)

    def test_reset_forgets_the_previous_run(self):
        """A re-search of cached rows runs at a completely different speed
        to a fresh batch, so last run's pace must not seed this one's."""
        for i, t in ((1, 0.0), (2, 60.0), (3, 120.0)):
            self.est.record(i, 100, now=t)
        self.assertGreater(self.est.seconds_remaining(), 0)
        self.est.reset()
        self.assertEqual(self.est.seconds_remaining(), -1.0)
        self.est.record(1, 100, now=1000.0)
        self.est.record(2, 100, now=1002.0)
        self.est.record(3, 100, now=1004.0)
        self.assertEqual(self.est.seconds_per_image(), 2.0)

    def test_a_finished_run_has_nothing_left(self):
        for i, t in ((1, 0.0), (2, 60.0), (3, 120.0)):
            self.est.record(i, 3, now=t)
        self.assertEqual(self.est.seconds_remaining(), 0.0)


class TestReviewShortcutRegistry(unittest.TestCase):
    """Defaults, overrides, and what gets written back to the config."""

    def test_the_shipped_defaults_do_not_collide(self):
        """Two actions on one key means Qt fires neither, so a collision
        shipped as a default would be fourteen keys minus two that just
        don't work, with nothing on screen to explain it."""
        from core.shortcuts import conflicts, default_bindings
        self.assertEqual(conflicts(default_bindings()), {})

    def test_every_action_has_a_default_and_a_label(self):
        from core.shortcuts import REVIEW_ACTIONS
        for action in REVIEW_ACTIONS:
            self.assertTrue(action.default, f"{action.id} ships unbound")
            self.assertTrue(action.label, f"{action.id} has no label")

    def test_an_override_wins_and_the_rest_stay_default(self):
        from core.shortcuts import ACTIONS_BY_ID, resolve
        bindings = resolve({"compare": "F2"})
        self.assertEqual(bindings["compare"], "F2")
        self.assertEqual(bindings["next_row"], ACTIONS_BY_ID["next_row"].default)
        self.assertEqual(set(bindings), set(ACTIONS_BY_ID))

    def test_an_override_for_an_action_this_build_lacks_is_dropped(self):
        """Bindings are stored as a plain id -> key map, so a config from
        a build with an action this one doesn't have would otherwise keep
        a key reserved for something nothing can fire."""
        from core.shortcuts import ACTIONS_BY_ID, resolve
        bindings = resolve({"a_removed_action": "J"})
        self.assertNotIn("a_removed_action", bindings)
        self.assertEqual(set(bindings), set(ACTIONS_BY_ID))

    def test_a_hand_edited_config_cannot_smuggle_a_non_string_through(self):
        """This value reaches QKeySequence. A number there raises inside
        Qt, where the message says nothing about the config file."""
        from core.shortcuts import ACTIONS_BY_ID, resolve
        bindings = resolve({"compare": 42, "next_row": None})
        self.assertEqual(bindings["compare"], ACTIONS_BY_ID["compare"].default)
        self.assertEqual(bindings["next_row"], ACTIONS_BY_ID["next_row"].default)

    def test_cleared_is_not_the_same_as_unset(self):
        """Absent means "use the default"; "" means the user took the key
        back deliberately. Collapsing the two would resurrect a binding
        someone had cleared, on the next launch, every time."""
        from core.shortcuts import prune, resolve
        self.assertEqual(resolve({"remove_row": ""})["remove_row"], "")
        self.assertEqual(prune({**resolve({}), "remove_row": ""})["remove_row"], "")

    def test_only_real_changes_are_written_to_the_config(self):
        """Saving all fourteen would freeze today's defaults into every
        config, so a later build could never improve one for anybody who
        had opened the Shortcuts tab once."""
        from core.shortcuts import default_bindings, prune
        self.assertEqual(prune(default_bindings()), {})
        changed = {**default_bindings(), "compare": "F2"}
        self.assertEqual(prune(changed), {"compare": "F2"})

    def test_conflicts_ignore_case_and_surrounding_space(self):
        from core.shortcuts import conflicts
        clashes = conflicts({"next_row": "J", "compare": " j "})
        self.assertEqual(len(clashes), 1)
        self.assertEqual(sorted(next(iter(clashes.values()))), ["compare", "next_row"])

    def test_any_number_of_actions_may_have_no_key(self):
        from core.shortcuts import conflicts
        self.assertEqual(conflicts({"a": "", "b": "", "c": "J"}), {})


class TestReviewedState(unittest.TestCase):
    """The flag that separates "I decided against this" from "I have not
    looked at this yet"."""

    def _entry(self):
        from core.models import ImageEntry
        return ImageEntry(path="/tmp/reviewed.png")

    def test_a_fresh_entry_wants_a_decision(self):
        self.assertTrue(self._entry().needs_review)

    def test_marking_it_settles_it(self):
        entry = self._entry()
        entry.reviewed = True
        self.assertFalse(entry.needs_review)

    def test_sending_settles_it_without_marking(self):
        """Sending IS a decision. Requiring both would mean every sent
        file still showed up as needing review, which is the exact
        confusion this flag exists to remove."""
        entry = self._entry()
        entry.sent_to_hydrus = True
        self.assertFalse(entry.needs_review)
        self.assertFalse(entry.reviewed, "sending must not fake an explicit mark")

    def test_resetting_a_result_clears_the_mark(self):
        """The judgement was about a specific match, and reset throws
        that match away. Leaving the row marked would hide it from the
        next pass on the strength of a decision about a result that no
        longer exists."""
        entry = self._entry()
        entry.reviewed = True
        entry.reset_result()
        self.assertFalse(entry.reviewed)
        self.assertTrue(entry.needs_review)

    def test_resetting_a_result_still_keeps_sent(self):
        """REGRESSION GUARD: reviewed and sent clear differently, and the
        reason is in reset_result's docstring - whether Hydrus holds the
        file is a fact about the library, not about this search."""
        entry = self._entry()
        entry.sent_to_hydrus = True
        entry.hydrus_import_confirmed = True
        entry.reset_result()
        self.assertTrue(entry.sent_to_hydrus)
        self.assertTrue(entry.hydrus_import_confirmed)

    def test_marking_counts_as_a_change_worth_saving(self):
        """The session store writes only entries whose revision moved. A
        mark that did not bump it would be dropped by an autosave and
        lost on the next restart - which is the one thing this flag has
        to survive."""
        entry = self._entry()
        before = entry.revision
        entry.reviewed = True
        self.assertGreater(entry.revision, before)


class TestReviewedSurvivesTheSession(unittest.TestCase):
    def test_it_round_trips(self):
        from core.models import ImageEntry
        from core.session import _entry_from_dict, _entry_to_dict
        entry = ImageEntry(path="/tmp/round-trip.png")
        entry.reviewed = True
        restored = _entry_from_dict(_entry_to_dict(entry))
        self.assertIsNotNone(restored)
        self.assertTrue(restored.reviewed)
        self.assertFalse(restored.needs_review)

    def test_a_session_written_before_this_existed_reads_as_unreviewed(self):
        """Correct rather than merely safe: nothing had marked those rows."""
        from core.session import _entry_from_dict
        restored = _entry_from_dict({"path": "/tmp/old.png"})
        self.assertIsNotNone(restored)
        self.assertFalse(restored.reviewed)
        self.assertTrue(restored.needs_review)


class TestResultsExport(unittest.TestCase):
    """Writing the working list out as CSV or JSON."""

    def _entry(self):
        from core.models import (
            ImageEntry, MatchCandidate, MatchStatus, Tag, TagSource,
        )
        e = ImageEntry(path="/mnt/pics/a.png")
        e.status = MatchStatus.GOOD
        e.local_width, e.local_height = 1200, 1800
        e.hydrus_hash = "abc123"
        e.candidates = [
            MatchCandidate(url="https://danbooru.donmai.us/posts/1",
                           source_name="Danbooru", similarity=94.5,
                           engine="IQDB", width=2400, height=3600),
        ]
        e.select_candidate(0)
        e.add_tags([Tag("1girl", TagSource.BOORU),
                    Tag("someone", TagSource.USER, "creator")])
        return e

    def _rows(self, text):
        import csv, io
        return list(csv.reader(io.StringIO(text)))

    def test_csv_starts_with_a_header_naming_every_field(self):
        from core.export import HEADERS, to_csv
        rows = self._rows(to_csv([self._entry()]))
        self.assertEqual(rows[0], list(HEADERS))

    def test_one_row_per_image(self):
        from core.export import to_csv
        rows = self._rows(to_csv([self._entry(), self._entry()]))
        self.assertEqual(len(rows), 3)      # header + two

    def test_the_row_carries_the_result(self):
        from core.export import HEADERS, to_csv
        rows = self._rows(to_csv([self._entry()]))
        row = dict(zip(HEADERS, rows[1], strict=True))
        self.assertEqual(row["filename"], "a.png")
        self.assertEqual(row["status"], "Found (good)")
        self.assertEqual(row["matched_url"], "https://danbooru.donmai.us/posts/1")
        self.assertEqual(row["site"], "Danbooru")
        self.assertEqual(row["engine"], "IQDB")
        self.assertEqual(row["similarity"], "94.5")
        self.assertEqual(row["match_width"], "2400")
        self.assertEqual(row["tag_count"], "2")

    def test_a_comma_in_a_field_does_not_shift_the_columns(self):
        """The reason this goes through the csv module rather than
        joining strings: an unquoted comma in a filename moves every
        later column along by one, silently."""
        from core.export import HEADERS, to_csv
        entry = self._entry()
        entry.path = "/mnt/pics/a, b.png"
        rows = self._rows(to_csv([entry]))
        self.assertEqual(len(rows[1]), len(HEADERS))
        self.assertEqual(dict(zip(HEADERS, rows[1], strict=True))["filename"], "a, b.png")

    def test_a_quote_in_a_field_survives(self):
        from core.export import HEADERS, to_csv
        entry = self._entry()
        entry.error_message = 'timeout after 30s "hard"'
        rows = self._rows(to_csv([entry]))
        self.assertEqual(dict(zip(HEADERS, rows[1], strict=True))["error"],
                         'timeout after 30s "hard"')

    def test_tags_carry_their_namespace(self):
        from core.export import HEADERS, to_csv
        rows = self._rows(to_csv([self._entry()]))
        self.assertEqual(dict(zip(HEADERS, rows[1], strict=True))["tags"],
                         "1girl, creator:someone")

    def test_csv_line_endings_are_fixed_not_platform_dependent(self):
        """A file that opens correctly only on the machine that wrote it
        is not an export."""
        from core.export import to_csv
        self.assertIn("\r\n", to_csv([self._entry()]))

    def test_booleans_render_as_a_spreadsheet_understands_them(self):
        from core.export import HEADERS, to_csv
        entry = self._entry()
        entry.reviewed = True
        row = dict(zip(HEADERS, self._rows(to_csv([entry]))[1], strict=True))
        self.assertEqual(row["reviewed"], "TRUE")
        self.assertEqual(row["file_missing"], "FALSE")

    def test_the_sent_column_uses_the_same_three_states_as_the_table(self):
        from core.export import HEADERS, to_csv

        def sent_of(entry):
            return dict(zip(HEADERS, self._rows(to_csv([entry]))[1], strict=True))["sent"]

        entry = self._entry()
        self.assertEqual(sent_of(entry), "")
        entry.sent_to_hydrus = True
        self.assertEqual(sent_of(entry), "queued")
        entry.hydrus_import_confirmed = True
        self.assertEqual(sent_of(entry), "sent")

    def test_the_upscale_verdict_column(self):
        from core.export import HEADERS, to_csv

        def verdict_of(entry):
            return dict(zip(HEADERS, self._rows(to_csv([entry]))[1], strict=True))["upscale_verdict"]

        entry = self._entry()
        self.assertEqual(verdict_of(entry), "")
        entry.upscale_verdict = "flagged"
        self.assertEqual(verdict_of(entry), "flagged")

    def test_a_provisional_result_says_so_over_where_it_came_from(self):
        """It being deliberately unfinished matters more to a reader than
        whether it was fresh."""
        from core.export import HEADERS, to_csv
        entry = self._entry()
        entry.result_source = "fresh"
        entry.searched_without_saucenao = True
        self.assertEqual(
            dict(zip(HEADERS, self._rows(to_csv([entry]))[1], strict=True))["cache"], "provisional")

    def test_an_empty_list_still_writes_the_header(self):
        from core.export import HEADERS, to_csv
        self.assertEqual(self._rows(to_csv([])), [list(HEADERS)])

    # -- JSON ----------------------------------------------------------

    def test_json_wraps_the_rows_with_a_count_and_a_timestamp(self):
        """What makes two exports of the same library comparable, and
        what tells a reader a truncated file is truncated."""
        import json
        from core.export import SCHEMA_VERSION, to_json
        payload = json.loads(to_json([self._entry(), self._entry()]))
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["images"]), 2)
        self.assertTrue(payload["generated_at"])

    def test_json_keeps_tags_as_structured_values(self):
        """The lossless form. Flattening to "namespace:name" cannot be
        undone, since a tag may itself contain a colon."""
        import json
        from core.export import to_json
        tags = json.loads(to_json([self._entry()]))["images"][0]["tags"]
        self.assertEqual(tags[0], {"name": "1girl", "namespace": None, "source": "Booru"})
        self.assertEqual(tags[1],
                         {"name": "someone", "namespace": "creator", "source": "User"})

    def test_a_comma_in_a_tag_survives_json_but_not_csv(self):
        """The documented difference between the two formats, pinned so
        it stays a stated tradeoff rather than becoming a surprise."""
        import json
        from core.models import Tag, TagSource
        from core.export import HEADERS, to_csv, to_json
        entry = self._entry()
        entry.tags = [Tag("some, one", TagSource.USER)]
        self.assertEqual(json.loads(to_json([entry]))["images"][0]["tags"][0]["name"],
                         "some, one")
        # CSV quotes the cell correctly, so no column shifts - the loss is
        # only that the separator inside it is no longer unambiguous.
        row = dict(zip(HEADERS, self._rows(to_csv([entry]))[1], strict=True))
        self.assertEqual(row["tags"], "some, one")
        self.assertEqual(row["tag_count"], "1")

    def test_both_formats_describe_the_same_fields(self):
        """One schema, two renderers - so a field can never exist in one
        and not the other."""
        import json
        from core.export import HEADERS, to_json
        record = json.loads(to_json([self._entry()]))["images"][0]
        self.assertEqual(set(record), set(HEADERS))

    def test_json_is_valid_for_an_empty_list(self):
        import json
        from core.export import to_json
        payload = json.loads(to_json([]))
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["images"], [])

    # -- format choice --------------------------------------------------

    def test_the_extension_picks_the_format_and_csv_is_the_fallback(self):
        from core.export import format_for_path
        self.assertEqual(format_for_path("/tmp/r.json"), "json")
        self.assertEqual(format_for_path("/tmp/r.JSON"), "json")
        self.assertEqual(format_for_path("/tmp/r.csv"), "csv")
        # Someone who typed "results.txt" meant a table, not an error.
        self.assertEqual(format_for_path("/tmp/r.txt"), "csv")


class TestShowFilesInHydrus(unittest.TestCase):
    """Opening a Hydrus page on files the app is holding."""

    def setUp(self):
        from core.config import HydrusSettings
        from core.hydrus_client import HydrusClient
        self.client = HydrusClient(HydrusSettings(api_url="http://x", access_key="k"))

    def _post(self, status=200, payload=None):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload if payload is not None else {}
        resp.text = ""
        return resp

    def test_it_asks_for_a_file_search_page_holding_the_hashes(self):
        from unittest.mock import patch
        hashes = ["aa" * 32, "bb" * 32]
        with patch("requests.post", return_value=self._post(
                payload={"page_key": "ff" * 32})) as post:
            key = self.client.show_files_in_client(hashes, "Hatate: 2 files")
        self.assertEqual(key, "ff" * 32)
        self.assertIn("/manage_pages/new_page", post.call_args.args[0])
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["page_type"], 6)      # 6 = file search, the only kind
        self.assertEqual(body["hashes"], hashes)    # that can be handed hashes
        self.assertEqual(body["page_name"], "Hatate: 2 files")
        self.assertTrue(body["focus_page"], "showing a page you must then find is no use")

    def test_the_page_is_not_locked_to_those_hashes(self):
        """system_hash_locked would pin the page to exactly this set,
        which takes away the user's ability to search or navigate on from
        what they were shown - and being shown the file is the point."""
        from unittest.mock import patch
        with patch("requests.post", return_value=self._post()) as post:
            self.client.show_files_in_client(["aa" * 32], "one")
        self.assertNotIn("system_hash_locked", post.call_args.kwargs["json"])

    def test_an_empty_list_never_reaches_the_network(self):
        from unittest.mock import patch
        from core.hydrus_client import HydrusError
        with patch("requests.post") as post:
            with self.assertRaises(HydrusError):
                self.client.show_files_in_client([], "none")
        self.assertFalse(post.called)

    def test_a_missing_permission_surfaces_as_a_403(self):
        """This is the only call here needing Manage Pages, so a key set
        up for importing has never had to have it."""
        from unittest.mock import patch
        from core.hydrus_client import HydrusError
        with patch("requests.post", return_value=self._post(status=403)):
            with self.assertRaises(HydrusError) as caught:
                self.client.show_files_in_client(["aa" * 32], "one")
        self.assertIn("403", str(caught.exception))

    def test_an_older_client_without_the_endpoint_surfaces_as_a_404(self):
        from unittest.mock import patch
        from core.hydrus_client import HydrusError
        with patch("requests.post", return_value=self._post(status=404)):
            with self.assertRaises(HydrusError) as caught:
                self.client.show_files_in_client(["aa" * 32], "one")
        self.assertIn("404", str(caught.exception))

    def test_a_response_without_a_page_key_is_not_fatal(self):
        """The page opened either way; the key is only useful for further
        page commands, which this makes none of."""
        from unittest.mock import patch
        with patch("requests.post", return_value=self._post(payload={})):
            self.assertEqual(self.client.show_files_in_client(["aa" * 32], "one"), "")


class TestEnginePipelineSummary(unittest.TestCase):
    """REGRESSION GUARD for DAN-28 Item 3: the engine pipeline summary
    function must correctly describe the ordered pipeline from Settings,
    including the Pawchive-always-first-wave special case."""

    def _make_settings(self, **overrides):
        from core.config import Settings
        s = Settings()
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def test_default_pipeline(self):
        """Default settings: IQDB primary, SauceNAO always, extras upfront."""
        from core.engines import pipeline_summary
        s = self._make_settings()
        summary = pipeline_summary(s)
        # Default: IQDB → SauceNAO → ascii2d (enabled by default)
        self.assertIn("IQDB", summary)
        self.assertIn("SauceNAO", summary)
        self.assertIn("ascii2d", summary)
        self.assertTrue(summary.index("IQDB") < summary.index("SauceNAO"),
                        "IQDB should come before SauceNAO")

    def test_secondary_fallback_mode(self):
        """Secondary engine in fallback mode."""
        from core.engines import pipeline_summary
        s = self._make_settings(
            secondary_engine_mode="fallback",
            fallback_below_similarity=0,
        )
        summary = pipeline_summary(s)
        self.assertIn("fallback", summary.lower())

    def test_secondary_fallback_with_threshold(self):
        """Secondary engine in fallback mode with similarity threshold."""
        from core.engines import pipeline_summary
        s = self._make_settings(
            secondary_engine_mode="fallback",
            fallback_below_similarity=70,
        )
        summary = pipeline_summary(s)
        self.assertIn("70", summary)
        self.assertIn("best match", summary.lower())

    def test_extras_only_as_fallback(self):
        """Extras held back for fallback wave."""
        from core.engines import pipeline_summary
        s = self._make_settings(
            secondary_engine_mode="fallback",
            fallback_below_similarity=70,
            extras_only_as_fallback=True,
            enable_ascii2d=True,
            enable_google_lens=True,
        )
        summary = pipeline_summary(s)
        # With threshold > 0, fallback is described as "(if best match < 70%)"
        self.assertIn("70", summary)
        self.assertIn("best match", summary.lower())
        self.assertIn("ascii2d", summary)
        self.assertIn("Google Lens", summary)

    def test_pawchive_always_first_wave(self):
        """Pawchive runs in first wave regardless of extras_only_as_fallback."""
        from core.engines import pipeline_summary
        s = self._make_settings(
            extras_only_as_fallback=True,
            enable_pawchive=True,
            enable_pawchive_index=True,
        )
        summary = pipeline_summary(s)
        # Pawchive and Pawchive index should be in first wave (before fallback)
        pawchive_pos = summary.index("Pawchive")
        pawchive_idx_pos = summary.index("Pawchive index")
        fallback_pos = summary.lower().index("fallback") if "fallback" in summary.lower() else len(summary)
        self.assertLess(pawchive_pos, fallback_pos,
                        "Pawchive should be before fallback")
        self.assertLess(pawchive_idx_pos, fallback_pos,
                        "Pawchive index should be before fallback")

    def test_secondary_disabled(self):
        """Second engine completely disabled."""
        from core.engines import pipeline_summary
        s = self._make_settings(secondary_engine_mode="disabled")
        summary = pipeline_summary(s)
        self.assertNotIn("SauceNAO", summary, "SauceNAO should not appear when disabled")
        self.assertIn("IQDB", summary)

    def test_saucenao_as_primary(self):
        """SauceNAO configured as primary engine."""
        from core.engines import pipeline_summary
        s = self._make_settings(primary_engine="saucenao")
        summary = pipeline_summary(s)
        self.assertTrue(summary.index("SauceNAO") < summary.index("IQDB"),
                        "SauceNAO should come before IQDB when primary")

    def test_all_extras_disabled(self):
        """No extra engines enabled."""
        from core.engines import pipeline_summary
        s = self._make_settings(
            enable_ascii2d=False,
            enable_tracemoe=False,
            enable_iqdb3d=False,
            enable_google_images=False,
            enable_google_lens=False,
            enable_yandex=False,
            enable_pawchive=False,
            enable_pawchive_index=False,
        )
        summary = pipeline_summary(s)
        self.assertIn("IQDB", summary)
        self.assertIn("SauceNAO", summary)
        self.assertNotIn("ascii2d", summary)
        self.assertNotIn("Google Lens", summary)
