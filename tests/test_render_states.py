"""tools/render_states.py (DAN-1294): the offscreen render harness that emits
screenshots under the mockup's own file names.

One smoke test that the Queue, Review and Activity states render without
error, in both themes, plus the contract the design seat's diff depends on:
the names, the pixel size, and that nothing escapes into the real config.
No pixel assertions - what the pictures look like is the diff's business.
Runs the CLI as a subprocess, which is also how it is used, and keeps the
harness's process-wide environment edits out of the test run.
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import _path  # noqa: F401  (puts the project root on sys.path)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "render_states.py"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=300)


# Runs the real renderer for one state but reads the window's widgets just
# before the shot is saved, which the CLI cannot expose.
_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from tools import render_states as rs
seen = []
orig = rs._Renderer._save
def save(self, win, state, mode):
    win.show(); self._pump()
    parts = win._quota_banner_parts
    seen.append(dict(
        mode=mode, banner=win.quota_banner.isVisible(),
        review=parts['review_button'].isVisible(), review_text=parts['review_button'].text(),
        continue_visible=parts['continue_button'].isVisible(),
        note=win.run_note_label.text(), eta=win.run_eta_label.text(), modals=list(self.modals),
        texts=[w.text() for w in win.quota_banner.findChildren(rs_label)]))
    return orig(self, win, state, mode)
from PyQt6.QtWidgets import QLabel as rs_label
rs._Renderer._save = save
rs.render(__import__('pathlib').Path(sys.argv[2]), ['queue-interrupted-paused'])
print('PROBE' + json.dumps(seen))
"""


def _png_size(path):
    head = Path(path).read_bytes()[:24]
    assert head[:8] == PNG_MAGIC, f"{path} is not a PNG"
    return struct.unpack(">II", head[16:24])


class TestRenderStates(unittest.TestCase):
    def test_queue_review_and_activity_render_in_both_themes(self):
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as cfg:
            env = dict(os.environ, XDG_CONFIG_HOME=cfg, HOME=cfg)
            done = _run(out, "--states", "queue,review,activity", env=env)
            self.assertEqual(done.returncode, 0, done.stderr)
            expected = {"queue-1440.png", "queue-light-1440.png",
                        "review-1440.png", "review-light-1440.png",
                        "activity-1440.png", "activity-light-1440.png"}
            self.assertEqual({p.name for p in Path(out).iterdir()}, expected)
            for name in expected:
                self.assertEqual(_png_size(Path(out) / name), (1440, 900), name)
            # The real (here: stand-in) config dir was never touched.
            self.assertEqual(list(Path(cfg).iterdir()), [], "harness wrote into the invoking config")

    def test_interrupted_paused_shows_the_quota_banner_in_both_themes(self):
        # DAN-1359: this state used to set only the status text, so the shot
        # carried no banner, no strip note and no `ETA paused` - nothing for
        # a diff against the mockup's P12b to compare.
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as cfg:
            env = dict(os.environ, XDG_CONFIG_HOME=cfg, HOME=cfg)
            done = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT), out],
                                  capture_output=True, text=True, env=env, timeout=300)
            self.assertEqual(done.returncode, 0, done.stderr)
            seen = json.loads(done.stdout.split("PROBE", 1)[1])
            self.assertEqual([s["mode"] for s in seen], ["dark", "light"])
            for s in seen:
                self.assertTrue(s["banner"], s)
                self.assertIn("RUN PAUSED \u2014 SAUCENAO'S DAILY QUOTA IS EXHAUSTED",
                              [t.upper() for t in s["texts"]], s)
                self.assertTrue(s["review"], s)
                self.assertTrue(s["review_text"].startswith("Review the"), s)
                self.assertTrue(s["continue_visible"], s)
                self.assertEqual(s["note"], "paused \u2014 quota exhausted")
                self.assertIn("paused", s["eta"].lower())
                self.assertEqual(s["modals"], [], "a modal was raised")
            self.assertEqual({p.name for p in Path(out).iterdir()},
                             {"queue-interrupted-paused-1440.png",
                              "queue-interrupted-paused-light-1440.png"})

    def test_names_are_the_mockups(self):
        done = _run("--list")
        self.assertEqual(done.returncode, 0, done.stderr)
        names = done.stdout.split()
        self.assertIn("queue-1440.png", names)
        self.assertIn("queue-light-1440.png", names)
        self.assertIn("queue-interrupted-crashed-light-1440.png", names)
        self.assertTrue(all(n.endswith("-1440.png") for n in names), names)
        self.assertFalse([n for n in names if "-dark" in n], "dark is the unsuffixed default")

    def test_unknown_state_fails_loudly_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as out:
            done = _run(out, "--states", "queue,no-such-state")
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("no-such-state", done.stderr)
            self.assertEqual(list(Path(out).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
