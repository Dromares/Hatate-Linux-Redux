"""The AppImage's build inputs and shipped files stay honest.

Nothing here builds an image (that needs bwrap, the network and several
GB); these are the checks that can fail in CI when someone edits the
packaging:

  * every external input is pinned to a fixed URL AND a SHA-256 - a
    `latest`/`continuous` URL, or a hash left empty, is a supply-chain hole;
  * Playwright is pinned exactly and the wheel pins carry hashes;
  * the AppImage ships the same package versions as requirements-lock.txt,
    and the source install's requirements.txt keeps Playwright optional;
  * AppRun sets the two variables the app relies on, and the desktop file,
    icon and NOTICE are in place;
  * AppRun writes a fontconfig file that names the bundled font directory by
    its absolute path (the image's fontconfig 2.13 loads 0 fonts from a
    relative <dir>) and writes nothing inside $APPDIR;
  * the shell scripts at least parse.

A built image can be checked the same way with smoke-fonts.sh (bwrap, no
system fonts): set HATATE_APPIMAGE_FILE to run that here too.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from . import _path  # noqa: F401  (sys.path + throwaway XDG_CONFIG_HOME)

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging" / "appimage"


def _pins() -> dict:
    values = {}
    for line in (PKG / "pins.sh").read_text().splitlines():
        m = re.match(r'^([A-Z0-9_]+)="([^"]*)"\s*$', line)
        if m:
            values[m.group(1)] = m.group(2)
    return values


def _requirement_pins(path: Path) -> dict:
    pins = {}
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


class TestPins(unittest.TestCase):
    def setUp(self):
        self.pins = _pins()

    def test_every_url_has_a_sha256_and_is_fixed(self):
        urls = {k[:-4]: v for k, v in self.pins.items() if k.endswith("_URL")}
        self.assertGreaterEqual(len(urls), 5)          # rootfs, python, appimagetool, runtime, excludelist
        for name, url in urls.items():
            with self.subTest(name):
                self.assertTrue(url.startswith("https://"), url)
                self.assertNotRegex(url.lower(), r"/(latest|continuous)(/|$)")
                self.assertRegex(self.pins.get(f"{name}_SHA256", ""), r"^[0-9a-f]{64}$")

    def test_the_chromium_archive_is_pinned_too(self):
        self.assertRegex(self.pins["CHROMIUM_ZIP_SHA256"], r"^[0-9a-f]{64}$")

    def test_playwright_is_pinned_exactly(self):
        self.assertRegex(self.pins["PLAYWRIGHT_VERSION"], r"^\d+\.\d+\.\d+$")
        lock = _requirement_pins(PKG / "requirements-appimage.txt")
        self.assertEqual(lock["playwright"], self.pins["PLAYWRIGHT_VERSION"])


class TestWheelLock(unittest.TestCase):
    def test_every_pin_carries_hashes(self):
        text = (PKG / "requirements-appimage.txt").read_text()
        entries = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", text)
        pinned = [e for e in entries if re.match(r"[A-Za-z0-9_.-]+==", e)]
        self.assertGreaterEqual(len(pinned), 10)
        for entry in pinned:
            with self.subTest(entry.split()[0]):
                self.assertRegex(entry, r"--hash=sha256:[0-9a-f]{64}")

    def test_mcp_is_not_bundled(self):
        self.assertNotIn("mcp", _requirement_pins(PKG / "requirements-appimage.txt"))

    def test_same_versions_as_requirements_lock(self):
        """The image ships what the test-suite was run against."""
        lock = _requirement_pins(ROOT / "requirements-lock.txt")
        image = _requirement_pins(PKG / "requirements-appimage.txt")
        self.assertTrue(lock)
        for name, version in lock.items():
            with self.subTest(name):
                self.assertEqual(
                    image.get(name), version,
                    f"requirements-lock.txt pins {name}=={version} but the AppImage lock "
                    "differs: regenerate packaging/appimage/requirements-appimage.txt "
                    "(make-lock.py)")

    def test_source_install_keeps_playwright_optional(self):
        """DAN-1937: run.sh and requirements.txt are unchanged in kind."""
        active = [ln for ln in (ROOT / "requirements.txt").read_text().splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertFalse([ln for ln in active if "playwright" in ln.lower()])
        self.assertFalse([ln for ln in active if ln.lower().startswith("mcp")])
        self.assertNotIn("playwright", (ROOT / "run.sh").read_text().lower())


class TestShippedFiles(unittest.TestCase):
    def test_apprun_sets_what_the_app_relies_on(self):
        apprun = PKG / "AppRun"
        text = apprun.read_text()
        self.assertTrue(os.access(apprun, os.X_OK))
        self.assertIn("HATATE_APPIMAGE=1", text)
        self.assertIn('PLAYWRIGHT_BROWSERS_PATH="$APPDIR/usr/lib/ms-playwright"', text)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", text)       # the mount is read-only

    def test_desktop_file_and_icon(self):
        text = (PKG / "hatate.desktop").read_text()
        for key in ("Name=Hatate", "Exec=hatate", "Icon=hatate", "Type=Application"):
            self.assertIn(key, text)
        self.assertTrue((ROOT / "resources" / "icon.svg").is_file())

    def test_notice_states_the_licence_position(self):
        text = (PKG / "NOTICE").read_text()
        self.assertRegex(text, r"AS A WHOLE IS DISTRIBUTED UNDER THE GNU GENERAL PUBLIC\s+LICENSE")
        self.assertIn("MIT", text)
        for component in ("CPython", "PyQt6", "Qt 6", "Playwright", "Chromium"):
            self.assertIn(component, text)

    def test_licence_texts_the_build_copies_exist(self):
        self.assertTrue((PKG / "licenses" / "chromium-LICENSE.txt").is_file())
        self.assertTrue((PKG / "licenses" / "qt-source-offer.txt").is_file())

    def test_shell_scripts_parse(self):
        for name in ("build.sh", "inner-build.sh", "AppRun", "pins.sh", "smoke-fonts.sh"):
            with self.subTest(name):
                proc = subprocess.run(["bash", "-n", str(PKG / name)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_releasing_md_names_draft_and_probes(self):
        text = (ROOT / "RELEASING.md").read_text()
        self.assertIn("gh release create", text)
        self.assertIn("--draft", text)
        self.assertIn("DAN-1702", text)
        self.assertIn("v0.0.0-nope", text)


if __name__ == "__main__":
    unittest.main()


def _fake_appdir(root: Path) -> Path:
    """An AppDir whose 'python' prints the environment AppRun hands it."""
    appdir = root / "mnt dir"                      # a space: the path must survive quoting
    (appdir / "usr" / "python" / "bin").mkdir(parents=True)
    (appdir / "usr" / "share" / "hatate").mkdir(parents=True)
    (appdir / "usr" / "share" / "fonts" / "hatate").mkdir(parents=True)
    shutil.copy(PKG / "AppRun", appdir / "AppRun")
    python = appdir / "usr" / "python" / "bin" / "python3.13"
    python.write_text('#!/bin/sh\necho "$FONTCONFIG_FILE"\n')
    python.chmod(0o755)
    (appdir / "usr" / "share" / "hatate" / "main.py").write_text("")
    return appdir


def _tree(path: Path) -> set:
    return {str(p.relative_to(path)) for p in path.rglob("*")}


class TestAppRunFonts(unittest.TestCase):
    """DAN-1942 F1: fontconfig 2.13 loaded 0 fonts from the shipped relative <dir>."""

    def _run(self, runtime: Path, appdir: Path) -> str:
        env = {"PATH": os.environ["PATH"], "HOME": str(runtime), "XDG_RUNTIME_DIR": str(runtime)}
        proc = subprocess.run([str(appdir / "AppRun")], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_conf_has_the_absolute_bundled_font_dir_and_lives_outside_appdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            appdir = _fake_appdir(tmp)
            runtime = tmp / "run"
            runtime.mkdir()
            before = _tree(appdir)
            conf = Path(self._run(runtime, appdir))
            self.assertTrue(conf.is_file(), conf)
            self.assertTrue(str(conf).startswith(str(runtime)), conf)    # not inside $APPDIR
            self.assertEqual(_tree(appdir), before)                      # nothing written there
            text = conf.read_text()
            self.assertIn(f"<dir>{appdir}/usr/share/fonts/hatate</dir>", text)
            self.assertNotIn('prefix="relative"', text)

    def test_a_second_launch_sweeps_confs_of_unmounted_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = _fake_appdir(tmp / "a")
            second = _fake_appdir(tmp / "b")
            runtime = tmp / "run"
            runtime.mkdir()
            stale = Path(self._run(runtime, first))
            shutil.rmtree(first)                                         # that mount is gone
            live = Path(self._run(runtime, second))
            self.assertFalse(stale.exists())
            self.assertTrue(live.is_file())

    @unittest.skipUnless(shutil.which("fc-list"), "fc-list not installed")
    def test_fontconfig_loads_a_font_from_the_conf(self):
        fonts = next(iter(Path("/usr/share/fonts").rglob("*.ttf")), None)
        if fonts is None:
            self.skipTest("no .ttf on this machine to stand in for the bundled fonts")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            appdir = _fake_appdir(tmp)
            shutil.copy(fonts, appdir / "usr" / "share" / "fonts" / "hatate" / "bundled.ttf")
            runtime = tmp / "run"
            runtime.mkdir()
            conf = self._run(runtime, appdir)
            proc = subprocess.run(
                ["fc-list", "--format", "%{file}\\n"],
                env={"PATH": os.environ["PATH"], "HOME": str(tmp), "FONTCONFIG_FILE": conf,
                     "XDG_CACHE_HOME": str(tmp / "cache")},
                capture_output=True, text=True)
            bundled = str(appdir / "usr" / "share" / "fonts" / "hatate" / "bundled.ttf")
            self.assertIn(bundled, proc.stdout.splitlines(), "bundled font not loaded")


@unittest.skipUnless(os.environ.get("HATATE_APPIMAGE_FILE") and shutil.which("bwrap"),
                     "set HATATE_APPIMAGE_FILE to a built AppImage (needs bwrap)")
class TestBuiltImageFonts(unittest.TestCase):
    def test_bundled_fontconfig_loads_the_bundled_fonts_with_no_system_fonts(self):
        proc = subprocess.run([str(PKG / "smoke-fonts.sh"), os.environ["HATATE_APPIMAGE_FILE"]],
                              capture_output=True, text=True)
        print(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"nfont=([1-9]\d*)")
        self.assertNotIn("U+29C9-in=none", proc.stdout)
