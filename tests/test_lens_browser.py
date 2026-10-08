"""core/lens_browser.py's page driving, against a fake page with a fake clock.

No browser runs. The fake delivers a "response" some milliseconds after
the action that causes it, so the tests can measure what each step
costs in (fake) time - which is the whole point of the settle logic.
"""
import threading
import unittest

from . import _path  # noqa: F401

RESULT_BODY = 'x "https://danbooru.donmai.us/posts/1" y'


class _Response:
    def __init__(self, url="https://www.google.com/search?vsrid=1", body=RESULT_BODY):
        self.url, self._body = url, body
        self.request = type("R", (), {"resource_type": "xhr"})()

    def text(self):
        return self._body


class _Locator:
    def __init__(self, page, key):
        self.page, self.key = page, key

    def count(self):
        return 1 if self.key in self.page.present else 0

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        if self.key not in self.page.present:
            self.page.now += timeout or 30000          # what a real miss costs
            raise TimeoutError(f"Locator.click: Timeout {timeout}ms exceeded.")
        self.page.clicked.append(self.key)
        self.page.present.discard(self.key)
        if self.page.challenge_on == self.key:
            self.page.challenge()
        delay = self.page.response_after.get(self.key)
        if delay is not None:
            self.page.schedule(delay)


class FakePage:
    """Just enough of Playwright's Page for LensBrowser._one."""

    def __init__(self, present=(), response_after=None, tiles=None, first_results_after=500,
                 challenge_on=None, answered_after_ms=None, recaptcha=False):
        # challenge_on: the step that brings a robot check - a control key,
        # "upload", or "warmup". answered_after_ms: when the user answers
        # it (None: never). recaptcha: in-page box rather than /sorry/.
        self.challenge_on = challenge_on
        self.answered_after_ms = answered_after_ms
        self.recaptcha = recaptcha
        self._answer_at = None
        self._showing_recaptcha = False
        self.now = 0
        self.present = set(present)
        self.response_after = response_after or {}
        self.clicked = []
        self.url = "https://www.google.com/"
        self._listeners = []
        self._pending = []                      # (due_ms, response)
        self._tiles = tiles or []
        self._first_results_after = first_results_after
        self.closed = False

    # -- responses ------------------------------------------------------
    def schedule(self, delay_ms):
        self._pending.append((self.now + delay_ms, _Response()))

    def _deliver(self):
        due = [p for p in self._pending if p[0] <= self.now]
        self._pending = [p for p in self._pending if p[0] > self.now]
        for _, response in due:
            for listener in list(self._listeners):
                listener(response)

    def challenge(self):
        if self.recaptcha:
            self._showing_recaptcha = True
        else:
            self._before_challenge = self.url
            self.url = "https://www.google.com/sorry/index?continue=x"
        if self.answered_after_ms is not None:
            self._answer_at = self.now + self.answered_after_ms

    def _maybe_answered(self):
        if self._answer_at is not None and self.now >= self._answer_at:
            self._answer_at = None
            self._showing_recaptcha = False
            if "/sorry/" in self.url:
                self.url = self._before_challenge

    # -- the Page API used ----------------------------------------------
    def locator(self, selector):
        page = self

        class _Frames:
            def count(self):
                return 1 if "recaptcha" in selector and page._showing_recaptcha else 0
        return _Frames()
    def _check(self):
        if self.closed:
            raise type("TargetClosedError", (Exception,), {})(
                "Page.goto: Target page, context or browser has been closed")

    def on(self, event, fn):
        self._listeners.append(fn)

    def remove_listener(self, event, fn):
        self._listeners.remove(fn)

    def goto(self, url, **kwargs):
        self._check()
        self.url = url
        if self.challenge_on == "warmup" and "q=weather" in url:
            self.challenge()

    def evaluate(self, script, *args):
        self._check()
        if "querySelectorAll('img')" in script:
            return list(self._tiles)
        self.url = "https://www.google.com/search?vsrid=abc"
        self.schedule(self._first_results_after)
        if self.challenge_on == "upload":
            self.challenge()
        return True

    def wait_for_url(self, *args, **kwargs):
        self._check()

    def wait_for_timeout(self, ms):
        self.now += ms
        self._maybe_answered()
        self._deliver()

    def bring_to_front(self):
        pass

    def query_selector_all(self, selector):
        return []                               # no crop handles: nothing to widen

    def get_by_role(self, role, name=None):
        return _Locator(self, f"tab:{name}")

    def get_by_text(self, text, exact=False):
        return _Locator(self, f"text:{text}")


def _no_desktop(test):
    """Never reach the real desktop from a test - on a KDE machine the
    page steps would otherwise send scripts to the live KWin."""
    from unittest.mock import patch
    patcher = patch("core.desktop_attention.available", return_value=False)
    patcher.start()
    test.addCleanup(patcher.stop)


def _job(**kwargs):
    from core.lens_browser import _Job
    return _Job(b"x", "a.jpg", 90.0, **kwargs)


class TestWaitingOnResultsNotTheClock(unittest.TestCase):
    def setUp(self):
        from core.lens_browser import LensBrowser
        self.browser = LensBrowser()
        self.browser._warmed = True
        _no_desktop(self)            # no warm-up navigation

    def test_a_typical_search_takes_seconds_not_a_minute(self):
        """Both tabs present and answering in about a second, and the
        explicit-results notice shown once. The fixed waits this replaces
        added up to ~35s of sleeping for the same page."""
        page = FakePage(
            present={"tab:Visual matches", "tab:Exact matches", "text:See exact matches"},
            response_after={"tab:Visual matches": 1200, "tab:Exact matches": 1000,
                            "text:See exact matches": 1500},
            tiles=[{"text": "t", "thumb": "data:image/jpeg;base64," + "A" * 500}])
        bodies, tiles = self.browser._one(page, _job())
        self.assertGreaterEqual(len(bodies), 3)
        self.assertEqual(len(tiles), 1)
        self.assertLess(page.now, 15000, f"took {page.now}ms of waiting")

    def test_controls_that_are_not_there_cost_nothing(self):
        """79 of 161 real searches spent 8s timing out on an absent tab,
        and each absent explicit label cost 4s more."""
        page = FakePage(present=set())
        self.browser._one(page, _job())
        self.assertEqual(page.clicked, [])
        self.assertLess(page.now, 5000, f"took {page.now}ms for a page with nothing to click")

    def test_a_step_whose_response_never_comes_waits_no_longer_than_before(self):
        from core import lens_browser
        page = FakePage(present={"tab:Visual matches"})   # clicked, answers nothing
        self.browser._one(page, _job())
        self.assertIn("tab:Visual matches", page.clicked)
        budget = (lens_browser.AFTER_RESULTS_MAX_MS + lens_browser.AFTER_TAB_MAX_MS
                  + lens_browser.TILE_RENDER_MAX_MS + 1000)
        self.assertLessEqual(page.now, budget)

    def test_a_slow_response_is_still_waited_for(self):
        """Settling must not give up on a response still on its way."""
        page = FakePage(present={"tab:Visual matches"},
                        response_after={"tab:Visual matches": 4000})
        bodies, _ = self.browser._one(page, _job())
        self.assertEqual(len(bodies), 2, "the first results and the tab's")


class _FakeContext:
    def __init__(self, page):
        self.pages = [page]


class TestAClosedWindowIsReopened(unittest.TestCase):
    def setUp(self):
        from core.lens_browser import LensBrowser
        self.browser = LensBrowser()
        self.browser._warmed = True
        _no_desktop(self)

    def test_the_interrupted_search_is_handed_back_to_run_again(self):
        page = FakePage()
        page.closed = True
        job = _job()
        again = self.browser._serve(_FakeContext(page), job)
        self.assertIs(again, job)
        self.assertTrue(job.retried)
        self.assertFalse(job.done.is_set(), "it has not been answered yet")

    def test_only_once(self):
        from core.lens_browser import LensBrowserError
        page = FakePage()
        page.closed = True
        job = _job(retried=True)
        self.browser._jobs.put(None)            # then stop
        self.assertIsNone(self.browser._serve(_FakeContext(page), job))
        self.assertTrue(job.done.is_set())
        self.assertIsInstance(job.error, LensBrowserError)
        self.assertNotIn("\n", str(job.error))

    def test_any_other_failure_reaches_the_caller_as_a_lens_error(self):
        """A raw Playwright exception used to escape as a traceback."""
        from core.lens_browser import LensBrowserError

        class Broken(FakePage):
            def goto(self, url, **kwargs):
                raise RuntimeError("net::ERR_NAME_NOT_RESOLVED at https://www.google.com/\n"
                                   "Call log:\n  - navigating")
        job = _job()
        self.browser._jobs.put(None)
        self.browser._serve(_FakeContext(Broken()), job)
        self.assertIsInstance(job.error, LensBrowserError)
        self.assertEqual(str(job.error), "net::ERR_NAME_NOT_RESOLVED at https://www.google.com/")

    def test_a_relaunch_that_fails_answers_the_waiting_search(self):
        """If the new window can't be opened, the search it was for must
        not be left waiting for an answer that will never come."""
        from unittest.mock import patch
        from core.lens_browser import LensBrowser, LensBrowserUnavailable
        browser = LensBrowser()
        job = _job()
        launches = []

        def launch(pw):
            launches.append(1)
            if len(launches) == 1:
                page = FakePage()
                page.closed = True
                return type("C", (), {"pages": [page], "close": lambda self: None})()
            raise RuntimeError("Chromium would not start")

        class _PW:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # Playwright is optional and CI doesn't install it: a stand-in
        # module is all _run needs to import.
        import sys
        import types
        fake = types.ModuleType("playwright.sync_api")
        fake.sync_playwright = lambda: _PW()
        browser._jobs.put(job)
        with patch.object(browser, "_launch", side_effect=launch), \
             patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"),
                                      "playwright.sync_api": fake}):
            thread = threading.Thread(target=browser._run)
            thread.start()
            thread.join(10)
        self.assertTrue(job.done.is_set())
        self.assertIsInstance(job.error, LensBrowserUnavailable)
        self.assertEqual(len(launches), 2)



class TestNotAskingGoogleTooOften(unittest.TestCase):
    def setUp(self):
        from core import google_lens
        google_lens.reset_blocked_flag()
        google_lens.end_rest()
        self.addCleanup(google_lens.reset_blocked_flag)
        self.addCleanup(google_lens.end_rest)

    def _challenge(self):
        from unittest.mock import patch
        from core import google_lens
        from core.lens_browser import LensChallengeUnanswered
        with patch.object(google_lens, "prepare_for_lens", return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          side_effect=LensChallengeUnanswered("nobody answered")):
            with self.assertRaises(google_lens.GoogleLensBlockedError) as caught:
                google_lens.search("/tmp/x.jpg", timeout=5)
        return str(caught.exception)

    def test_an_unanswered_check_rests_lens_through_a_new_search(self):
        """Starting a search cleared the stand-down, so every re-search
        met a fresh robot check: "an endless cycle of captchas"."""
        from core import google_lens
        message = self._challenge()
        google_lens.reset_blocked_flag()          # what starting a search does
        self.assertTrue(google_lens.is_blocked())
        self.assertIn(google_lens.resting_until(), message)

    def test_the_rest_ends(self):
        from unittest.mock import patch
        from core import google_lens
        self._challenge()
        later = __import__("time").monotonic() + google_lens.CHALLENGE_REST_SECONDS + 1
        with patch("core.google_lens.time.monotonic", return_value=later):
            google_lens.reset_blocked_flag()
            self.assertFalse(google_lens.is_blocked())

    def test_the_rest_ends_within_the_same_run(self):
        """The check also latched Lens off, so it skipped every image for
        an hour after its rest had ended, until the search was restarted."""
        from unittest.mock import patch
        from core import google_lens
        self._challenge()
        later = __import__("time").monotonic() + google_lens.CHALLENGE_REST_SECONDS + 1
        with patch("core.google_lens.time.monotonic", return_value=later):
            self.assertFalse(google_lens.is_blocked())

    def test_the_browser_does_not_say_it_is_automated(self):
        """navigator.webdriver was true, and reCAPTCHA answered each
        solved check with another."""
        from core.lens_browser import LensBrowser
        seen = {}

        class _Chromium:
            def launch_persistent_context(self, profile, **kwargs):
                seen.update(kwargs)

        LensBrowser()._launch_context(type("PW", (), {"chromium": _Chromium()})())
        self.assertIn("--enable-automation", seen["ignore_default_args"])
        self.assertIn("--disable-blink-features=AutomationControlled", seen["args"])

    def test_a_resting_lens_says_so_on_the_row(self):
        from core import engine_runner, google_lens
        from core.config import Settings
        from core.models import ImageEntry
        self._challenge()
        errors = []
        engine_runner._collect_google_lens(ImageEntry(path="/tmp/x.jpg"), Settings(), [], errors,
                                           None)
        self.assertIn("resting after an unanswered robot check", errors[0])
        self.assertIn(google_lens.resting_until(), errors[0])

    def test_searches_are_spaced_start_to_start(self):
        from unittest.mock import patch
        from core import google_lens
        clock = [1000.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds
        with patch("core.google_lens.time.monotonic", side_effect=lambda: clock[0]), \
             patch("core.google_lens.time.sleep", side_effect=sleep):
            google_lens.wait_for_turn()
            self.assertEqual(slept, [], "the first search goes straight away")
            clock[0] += 10                       # it took 10s
            google_lens.wait_for_turn()
            self.assertAlmostEqual(sum(slept), google_lens.MIN_SECONDS_BETWEEN_SEARCHES - 10)

    def test_two_arriving_together_are_both_spaced(self):
        """The turn is claimed before waiting, so callers arriving at the
        same moment get consecutive slots rather than all being let go."""
        from unittest.mock import patch
        from core import google_lens
        with patch("core.google_lens.time.monotonic", return_value=500.0):
            for _ in range(3):
                google_lens.wait_for_turn(should_stop=lambda: True)
        gap = google_lens.MIN_SECONDS_BETWEEN_SEARCHES
        self.assertEqual(google_lens._next_turn, 500.0 + 3 * gap)

    def test_stopping_ends_the_wait(self):
        from core import google_lens
        google_lens.wait_for_turn()
        started = __import__("time").monotonic()
        google_lens.wait_for_turn(should_stop=lambda: True)
        self.assertLess(__import__("time").monotonic() - started, 2)

    def test_resting_until_survives_a_concurrent_clear(self):
        """resting_until() used to re-read the module-level _resting_until
        after checking is_resting() - if another thread's reset_blocked_flag()
        landed in that window (as it can: multiple engines run in parallel
        worker threads), _resting_until had gone back to None and
        `None - time.monotonic()` raised a TypeError. Snapshotting it into a
        local before the check closes that window."""
        from unittest.mock import patch
        from core import google_lens
        google_lens._resting_until = __import__("time").monotonic() + 60
        real_is_resting = google_lens.is_resting

        def racing_is_resting():
            result = real_is_resting()
            google_lens._resting_until = None  # another thread wins the race here
            return result

        with patch.object(google_lens, "is_resting", side_effect=racing_is_resting):
            result = google_lens.resting_until()  # must not raise TypeError
        self.assertRegex(result, r"^\d{2}:\d{2}$")


class TestTheWindowHasItsOwnClass(unittest.TestCase):
    def test_chromium_is_launched_with_it(self):
        """So a desktop window rule can target the Lens window alone."""
        from unittest.mock import MagicMock
        from core.lens_browser import WINDOW_CLASS, LensBrowser
        pw = MagicMock()
        LensBrowser()._launch_context(pw)
        args = pw.chromium.launch_persistent_context.call_args.kwargs["args"]
        self.assertIn(f"--class={WINDOW_CLASS}", args)


class TestOnlyCallingForTheUserWhenNeeded(unittest.TestCase):
    def test_the_script_touches_only_the_lens_window(self):
        from core import desktop_attention
        script = desktop_attention.script_for("hatate-lens", True, False)
        self.assertIn("w.resourceClass !== 'hatate-lens'", script)
        self.assertIn("w.skipTaskbar = true", script)
        self.assertIn("w.demandsAttention = false", script)

    def test_outside_kde_nothing_is_run(self):
        from unittest.mock import patch
        from core import desktop_attention
        with patch.dict("os.environ", {"XDG_CURRENT_DESKTOP": "GNOME"}), \
             patch("core.desktop_attention.subprocess.run") as run:
            desktop_attention.quiet("hatate-lens")
        run.assert_not_called()

    def _run(self, page, warmed=True):
        from unittest.mock import patch
        from core.lens_browser import LensBrowser
        calls = []
        browser = LensBrowser()
        browser._warmed = warmed
        with patch("core.lens_browser.desktop_attention.quiet",
                   side_effect=lambda c: calls.append("quiet")), \
             patch("core.lens_browser.desktop_attention.ask_for_input",
                   side_effect=lambda c: calls.append("ask")), \
             patch.object(browser, "_turn_off_safesearch_blur"):
            result = browser._one(page, _job())
        return calls, result

    def _asked_then_quiet(self, calls):
        self.assertIn("ask", calls)
        self.assertEqual(calls[calls.index("ask") + 1], "quiet", "quiet again once answered")
        self.assertEqual(calls[-1], "quiet")

    def test_a_check_after_the_upload_asks_for_the_user(self):
        calls, _ = self._run(FakePage(challenge_on="upload", answered_after_ms=5000))
        self.assertEqual(calls[0], "quiet")
        self._asked_then_quiet(calls)

    def test_a_check_after_clicking_a_tab_asks_too(self):
        """Only the upload used to be checked - anywhere else the check sat
        in the window unannounced."""
        page = FakePage(present={"tab:Visual matches"}, challenge_on="tab:Visual matches",
                        answered_after_ms=3000)
        calls, _ = self._run(page)
        self._asked_then_quiet(calls)

    def test_a_check_after_the_explicit_notice_asks_too(self):
        page = FakePage(present={"text:See exact matches"},
                        challenge_on="text:See exact matches", answered_after_ms=3000)
        calls, _ = self._run(page)
        self._asked_then_quiet(calls)

    def test_a_check_on_first_opening_google_asks_too(self):
        page = FakePage(challenge_on="warmup", answered_after_ms=3000)
        calls, _ = self._run(page, warmed=False)
        self._asked_then_quiet(calls)

    def test_an_in_page_recaptcha_counts_as_a_check(self):
        page = FakePage(challenge_on="upload", answered_after_ms=3000, recaptcha=True)
        calls, _ = self._run(page)
        self._asked_then_quiet(calls)

    def test_an_unanswered_check_is_reported_and_the_window_goes_quiet(self):
        from core.lens_browser import LensChallengeUnanswered
        page = FakePage(present={"tab:Visual matches"}, challenge_on="tab:Visual matches")
        with self.assertRaises(LensChallengeUnanswered):
            self._run(page)

    def test_an_ordinary_search_never_asks(self):
        from unittest.mock import patch
        from core.lens_browser import LensBrowser
        browser = LensBrowser()
        browser._warmed = True
        with patch("core.lens_browser.desktop_attention.quiet"), \
             patch("core.lens_browser.desktop_attention.ask_for_input") as ask:
            browser._one(FakePage(present={"tab:Visual matches"}), _job())
        ask.assert_not_called()

if __name__ == "__main__":
    unittest.main()
