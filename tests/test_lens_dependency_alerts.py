"""Google Lens's missing dependencies reach the user, not just the log.

REGRESSION (DAN-80): with Playwright or its Chromium absent, the engine
stood down on the first image and said so in the log alone. A batch then
ran to completion looking exactly like one where Lens had searched and
found nothing, and the row's "not searched" was read as "no match".

Three things are asserted here, because each failed in its own way:

  * the two failures are told apart. `pip install playwright` is the
    wrong advice for a missing browser binary, and the user who follows
    it reinstalls a package they already have.
  * the warning is raised once per condition, not once per image. A
    fallback engine fires for every image in the run.
  * the rest of the search still degrades gracefully - the engine
    reports its error and returns, and nothing propagates.

Playwright itself is never installed in CI, so everything here works
against stand-ins: a fake browsers directory for the download probe and
a raised LensBrowserUnavailable for the search path.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

from core import engine_alerts, google_lens, lens_browser


class _AlertsReset(unittest.TestCase):
    """Every test starts with nothing raised and nobody listening.

    The listener list is swapped out rather than merely added to. The
    registry is module state that lasts the whole process, and a gui test
    module earlier in the run leaves real MainWindow listeners on it -
    which would put a modal dialog on screen, with nobody to dismiss it,
    from inside a core unit test.
    """

    def setUp(self):
        engine_alerts.reset()
        self.addCleanup(engine_alerts.reset)
        others = list(engine_alerts._listeners)
        engine_alerts._listeners.clear()
        self.addCleanup(engine_alerts._listeners.extend, others)
        self.seen = []
        engine_alerts.subscribe(self.seen.append)
        self.addCleanup(engine_alerts.unsubscribe, self.seen.append)


class TestAlertsAreSaidOnce(_AlertsReset):
    def _alert(self, key="k"):
        return engine_alerts.EngineAlert(
            key=key, engine="Google Lens", title="t", body="b", remedy="do this")

    def test_an_alert_reaches_its_listeners(self):
        self.assertTrue(engine_alerts.raise_alert(self._alert()))
        self.assertEqual([a.title for a in self.seen], ["t"])

    def test_the_same_key_is_only_delivered_once(self):
        """The point of the whole module: a fallback engine fires once per
        image, and a 400-image batch must not stack 400 dialogs."""
        engine_alerts.raise_alert(self._alert())
        for _ in range(50):
            self.assertFalse(engine_alerts.raise_alert(self._alert()))
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(engine_alerts.already_raised("k"))

    def test_a_different_condition_is_still_said(self):
        engine_alerts.raise_alert(self._alert("playwright"))
        engine_alerts.raise_alert(self._alert("chromium"))
        self.assertEqual(len(self.seen), 2)

    def test_a_listener_that_throws_does_not_take_the_search_down(self):
        def explode(alert):
            raise RuntimeError("the window went away")

        engine_alerts.subscribe(explode)
        self.addCleanup(engine_alerts.unsubscribe, explode)
        self.assertTrue(engine_alerts.raise_alert(self._alert()))
        self.assertEqual(len(self.seen), 1)       # the good listener still ran

    def test_an_unsubscribed_listener_stops_hearing(self):
        engine_alerts.unsubscribe(self.seen.append)
        engine_alerts.raise_alert(self._alert())
        self.assertEqual(self.seen, [])

    def test_the_remedy_is_part_of_the_readable_text(self):
        alert = self._alert()
        self.assertIn("do this", alert.text())


class TestTellingTheTwoDependenciesApart(unittest.TestCase):
    """Which one is missing decides which command is offered."""

    def test_no_playwright_package(self):
        with patch.object(lens_browser, "playwright_available", return_value=False):
            status = lens_browser.check_dependencies()
        self.assertFalse(status.ok)
        self.assertEqual(status.missing, lens_browser.MISSING_PLAYWRIGHT)
        self.assertIn("pip install playwright", status.message)

    def test_playwright_but_no_chromium(self):
        with patch.object(lens_browser, "playwright_available", return_value=True), \
             patch.object(lens_browser, "chromium_installed", return_value=False):
            status = lens_browser.check_dependencies()
        self.assertFalse(status.ok)
        self.assertEqual(status.missing, lens_browser.MISSING_CHROMIUM)
        self.assertIn("playwright install chromium", status.message)
        # The wrong advice for this one - it is the package that IS there.
        self.assertNotIn("pip install playwright", status.message)

    def test_both_present(self):
        with patch.object(lens_browser, "playwright_available", return_value=True), \
             patch.object(lens_browser, "chromium_installed", return_value=True):
            self.assertTrue(lens_browser.check_dependencies().ok)

    def test_an_unreadable_layout_is_not_reported_as_missing(self):
        """chromium_installed answers None when it cannot tell, and a
        warning the user cannot act on is worse than none."""
        with patch.object(lens_browser, "playwright_available", return_value=True), \
             patch.object(lens_browser, "chromium_installed", return_value=None):
            self.assertTrue(lens_browser.check_dependencies().ok)


class TestFindingTheDownloadedBrowser(unittest.TestCase):
    def _root(self, *names):
        root = tempfile.mkdtemp(prefix="hatate-ms-playwright-")
        for name in names:
            os.mkdir(os.path.join(root, name))
        return root

    def _with_root(self, root):
        return patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": root})

    def test_a_downloaded_chromium_is_found(self):
        with self._with_root(self._root("chromium-1140", "ffmpeg-1011")):
            self.assertIs(lens_browser.chromium_installed(), True)

    def test_an_empty_registry_means_nothing_was_installed(self):
        with self._with_root(self._root()):
            self.assertIs(lens_browser.chromium_installed(), False)

    def test_the_headless_shell_alone_does_not_count(self):
        """This engine is headed on purpose - headless is challenged on
        sight - so the shell-only build cannot run it."""
        with self._with_root(self._root("chromium_headless_shell-1140")):
            self.assertIs(lens_browser.chromium_installed(), False)

    def test_a_missing_registry_directory_means_nothing_was_installed(self):
        root = os.path.join(tempfile.mkdtemp(prefix="hatate-ms-playwright-"), "never-made")
        with self._with_root(root):
            self.assertIs(lens_browser.chromium_installed(), False)

    def test_browsers_inside_the_package_cannot_be_read(self):
        """PLAYWRIGHT_BROWSERS_PATH=0 puts them in the installed package,
        a layout this does not try to read: None, not a false alarm."""
        with self._with_root("0"):
            self.assertIsNone(lens_browser.chromium_installed())


class TestReadingAFailedLaunch(unittest.TestCase):
    """The directory probe cannot see every layout, so a missing browser
    can still reach the launch - where Playwright says so in its own
    words."""

    def test_playwrights_own_wording_is_recognised(self):
        status = lens_browser.classify_launch_failure(
            RuntimeError("Executable doesn't exist at /home/u/.cache/ms-playwright/"
                         "chromium-1140/chrome-linux/chrome"))
        self.assertIsNotNone(status)
        self.assertEqual(status.missing, lens_browser.MISSING_CHROMIUM)

    def test_anything_else_is_not_a_dependency_problem(self):
        self.assertIsNone(lens_browser.classify_launch_failure(
            RuntimeError("Another Chromium is already using the Lens profile")))

    def test_the_helper_rewrites_only_the_dependency_case(self):
        rewritten = lens_browser._unavailable(
            RuntimeError("Executable doesn't exist at /nope/chrome"))
        self.assertEqual(rewritten.missing, lens_browser.MISSING_CHROMIUM)
        self.assertIn("playwright install chromium", str(rewritten))

        passed_through = lens_browser._unavailable(RuntimeError("display :0 refused"))
        self.assertIsNone(passed_through.missing)
        self.assertIn("display :0 refused", str(passed_through))


class TestFetchRefusesBeforeStartingAnything(unittest.TestCase):
    def test_a_missing_dependency_is_reported_with_which_one(self):
        browser = lens_browser.LensBrowser()
        status = lens_browser.DependencyStatus(
            lens_browser.MISSING_CHROMIUM, "install the browser")
        with patch.object(lens_browser, "check_dependencies", return_value=status), \
             patch.object(browser, "start") as start:
            with self.assertRaises(lens_browser.LensBrowserUnavailable) as caught:
                browser.fetch(b"x", "a.jpg", 5.0)
        self.assertEqual(caught.exception.missing, lens_browser.MISSING_CHROMIUM)
        start.assert_not_called()       # no browser thread for a search that cannot run


class TestTheSearchPathRaisesTheWarning(_AlertsReset):
    def setUp(self):
        super().setUp()
        google_lens.reset_blocked_flag()
        google_lens.end_rest()
        self.addCleanup(google_lens.reset_blocked_flag)
        self.addCleanup(google_lens.end_rest)

    def _search(self, missing):
        unavailable = lens_browser.LensBrowserUnavailable(
            "run: python3 -m playwright install chromium", missing)
        with patch.object(google_lens, "prepare_for_lens",
                          return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          side_effect=unavailable):
            with self.assertRaises(google_lens.GoogleLensUnavailableError):
                google_lens.search("/tmp/x.jpg", timeout=5)

    def test_a_missing_chromium_is_announced_with_its_fix(self):
        self._search(lens_browser.MISSING_CHROMIUM)
        self.assertEqual(len(self.seen), 1)
        alert = self.seen[0]
        self.assertEqual(alert.engine, "Google Lens")
        self.assertEqual(alert.key, google_lens.ALERT_KEYS[lens_browser.MISSING_CHROMIUM])
        self.assertIn("Chromium", alert.body)
        self.assertIn("playwright install chromium", alert.remedy)

    def test_a_missing_playwright_says_the_package(self):
        self._search(lens_browser.MISSING_PLAYWRIGHT)
        self.assertEqual(self.seen[0].key,
                         google_lens.ALERT_KEYS[lens_browser.MISSING_PLAYWRIGHT])
        self.assertIn("Playwright package", self.seen[0].body)

    def test_a_browser_that_would_not_start_is_still_announced(self):
        """No `missing` to name - a window that would not open, say - but
        the engine is just as dead for the rest of the run."""
        self._search(None)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0].key, google_lens.UNKNOWN_ALERT_KEY)

    def test_a_whole_batch_raises_it_once(self):
        """What the log-only version got right and a naive dialog would
        get wrong: the engine stands down, but is asked again on a new
        run, and a user must not come back to a stack of boxes."""
        for _ in range(5):
            google_lens.reset_blocked_flag()
            self._search(lens_browser.MISSING_CHROMIUM)
        self.assertEqual(len(self.seen), 1)


class TestTheRestOfTheSearchCarriesOn(_AlertsReset):
    """Requirement 5: an engine that cannot run reports itself and
    returns. It must not take the image, or the run, down with it."""

    def setUp(self):
        super().setUp()
        google_lens.reset_blocked_flag()
        google_lens.end_rest()
        self.addCleanup(google_lens.reset_blocked_flag)
        self.addCleanup(google_lens.end_rest)

    def test_the_engine_runner_records_the_error_and_returns_false(self):
        from core import engine_runner
        from core.config import Settings
        from core.models import ImageEntry

        entry = ImageEntry(path="/tmp/does-not-need-to-exist.png")
        candidates, errors = [], []
        unavailable = lens_browser.LensBrowserUnavailable(
            "install chromium", lens_browser.MISSING_CHROMIUM)
        with patch.object(google_lens, "prepare_for_lens",
                          return_value=(b"x", "a.jpg", None)), \
             patch.object(google_lens.lens_browser, "fetch_results_payloads",
                          side_effect=unavailable), \
             patch.object(google_lens, "wait_for_turn"):
            found = engine_runner._collect_google_lens(
                entry, Settings(), candidates, errors, None)

        self.assertFalse(found)
        self.assertEqual(candidates, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("Google Lens", errors[0])       # requirement 4: still reported
        self.assertEqual(len(self.seen), 1)           # requirement 2: and shown


if __name__ == "__main__":
    unittest.main()
