"""tools/render_states.py (DAN-1294): the offscreen render harness that emits
screenshots under the mockup's own file names.

One smoke test that the Queue, Review and Activity states render without
error, in both themes, plus the contract the design seat's diff depends on:
the names, the pixel size, and that nothing escapes into the real config.
No pixel assertions - what the pictures look like is the diff's business.
Runs the CLI as a subprocess, which is also how it is used, and keeps the
harness's process-wide environment edits out of the test run.
"""
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
