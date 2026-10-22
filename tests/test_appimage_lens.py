"""The AppImage reports Google Lens as available and never offers an install.

The AppImage carries its own Python, Playwright and Chromium, and its
AppRun sets HATATE_APPIMAGE=1 and PLAYWRIGHT_BROWSERS_PATH. Three things
are asserted, in both modes so the source install cannot drift:

  * with the bundled browsers in place, Lens is available;
  * with them missing, the message tells the user to re-download and
    never mentions `pip install playwright` / `playwright install`;
  * outside the AppImage the old install advice is unchanged.

Also covers `main.py --version`, which must print and exit without
loading Qt.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

from core import google_lens, lens_browser
from core.paths import PROJECT_DIR, running_as_appimage
from core.version import __version__

APPIMAGE_ENV = {"HATATE_APPIMAGE": "1"}


def _browsers_dir(*names):
    root = Path(tempfile.mkdtemp(prefix="hatate-appimage-browsers-"))
    for name in names:
        (root / name).mkdir()
    return root


class TestRunningAsAppImage(unittest.TestCase):
    def test_flag_comes_only_from_the_apprun_variable(self):
        with patch.dict(os.environ, APPIMAGE_ENV):
            self.assertTrue(running_as_appimage())
        with patch.dict(os.environ, {"HATATE_APPIMAGE": "0"}):
            self.assertFalse(running_as_appimage())
        with patch.dict(os.environ):
            os.environ.pop("HATATE_APPIMAGE", None)
            self.assertFalse(running_as_appimage())


class TestLensInsideTheAppImage(unittest.TestCase):
    def test_lens_is_available_with_the_bundled_browsers(self):
        root = _browsers_dir("chromium-1234")
        env = dict(APPIMAGE_ENV, PLAYWRIGHT_BROWSERS_PATH=str(root))
        with patch.dict(os.environ, env), \
             patch.object(lens_browser, "playwright_available", return_value=True):
            self.assertIs(lens_browser.chromium_installed(), True)
            self.assertTrue(lens_browser.check_dependencies().ok)

    def test_a_headless_shell_alone_is_not_the_headed_browser(self):
        root = _browsers_dir("chromium_headless_shell-1234")
        env = dict(APPIMAGE_ENV, PLAYWRIGHT_BROWSERS_PATH=str(root))
        with patch.dict(os.environ, env), \
             patch.object(lens_browser, "playwright_available", return_value=True):
            self.assertFalse(lens_browser.check_dependencies().ok)

    def test_missing_browsers_say_redownload_not_install(self):
        env = dict(APPIMAGE_ENV, PLAYWRIGHT_BROWSERS_PATH=str(_browsers_dir()))
        with patch.dict(os.environ, env), \
             patch.object(lens_browser, "playwright_available", return_value=True):
            status = lens_browser.check_dependencies()
        self.assertEqual(status.missing, lens_browser.MISSING_CHROMIUM)
        self.assertIn("AppImage", status.message)
        self.assertIn("SHA256SUMS", status.message)
        self.assertNotIn("pip install", status.message)
        self.assertNotIn("playwright install", status.message)

    def test_missing_playwright_package_does_not_offer_pip_either(self):
        with patch.dict(os.environ, APPIMAGE_ENV), \
             patch.object(lens_browser, "playwright_available", return_value=False):
            status = lens_browser.check_dependencies()
        self.assertEqual(status.missing, lens_browser.MISSING_PLAYWRIGHT)
        self.assertNotIn("pip install", status.message)
        self.assertNotIn("playwright install", status.message)
        self.assertNotIn("venv", status.message)

    def test_an_unset_browsers_path_is_missing_not_unknown(self):
        """Outside the AppImage a layout it cannot read answers None; the
        AppImage always sets the path, so None there means a wrong file."""
        with patch.dict(os.environ, APPIMAGE_ENV):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
            self.assertIs(lens_browser.chromium_installed(), False)

    def test_the_session_alert_does_not_offer_an_install(self):
        exc = lens_browser.LensBrowserUnavailable(
            lens_browser.bundled_browser_message(), lens_browser.MISSING_CHROMIUM)
        captured = []
        with patch.dict(os.environ, APPIMAGE_ENV), \
             patch.object(google_lens.engine_alerts, "raise_alert",
                          side_effect=lambda a: captured.append(a) or True):
            google_lens.announce_unavailable(exc)
        alert = captured[0]
        self.assertIn("bundled in this AppImage", alert.body)
        self.assertNotIn("pip install", alert.text())
        self.assertNotIn("playwright install", alert.text())


class TestSourceInstallIsUnchanged(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HATATE_APPIMAGE", None)

    def test_missing_playwright_still_offers_pip(self):
        with patch.object(lens_browser, "playwright_available", return_value=False):
            status = lens_browser.check_dependencies()
        self.assertIn("pip install playwright", status.message)
        self.assertIn("playwright install chromium", status.message)

    def test_missing_chromium_still_offers_playwright_install(self):
        with patch.object(lens_browser, "playwright_available", return_value=True), \
             patch.object(lens_browser, "chromium_installed", return_value=False):
            status = lens_browser.check_dependencies()
        self.assertIn("playwright install chromium", status.message)
        self.assertNotIn("AppImage", status.message)

    def test_unreadable_layout_is_still_unknown(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        self.assertIsNone(lens_browser.chromium_installed())


class TestVersionFlag(unittest.TestCase):
    def test_version_is_0_1_0(self):
        self.assertEqual(__version__, "0.1.0")

    def test_main_prints_version_and_exits_without_qt(self):
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["QT_QPA_PLATFORM"] = "this-platform-does-not-exist"   # Qt would abort if started
        script = (
            "import sys, runpy; sys.argv = ['main.py', '--version'];"
            "runpy.run_path('main.py', run_name='__main__')"
        )
        proc = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_DIR, env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"Hatate {__version__}")
        self.assertNotIn("PyQt6", proc.stderr)


if __name__ == "__main__":
    unittest.main()
