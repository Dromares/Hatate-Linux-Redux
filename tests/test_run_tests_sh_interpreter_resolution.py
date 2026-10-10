"""Regression coverage for run_tests.sh's interpreter/linter resolution.

DAN-890: run_tests.sh resolved $PY and $RUFF via `$DIR/venv` or a bare PATH
lookup only. Any environment where PATH gets reset between the caller's
venv activation and run_tests.sh's own non-interactive bash invocation
silently fell through to whatever bare `python3`/`ruff` PATH resolves to
instead of the activated venv - this is exactly what scripts/ci_local_matrix.sh
triggers: it sources a venv's activate script, then shells out to
`./run_tests.sh`, a separate bash process, and this host's BASH_ENV resets
PATH for every non-interactive shell. $VIRTUAL_ENV survives that reset
untouched, so run_tests.sh must check it explicitly rather than trust PATH.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "run_tests.sh"


def _write_executable(path, content):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class RunTestsShInterpreterResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-tests-sh-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # An isolated copy, run from a directory with no `venv/` of its
        # own, so $DIR/venv can never mask what's under test here.
        self.workdir = self.tmp / "workdir"
        self.workdir.mkdir()
        shutil.copy(SCRIPT, self.workdir / "run_tests.sh")
        (self.workdir / "run_tests.sh").chmod(0o755)

        # A fake "activated venv", distinguishable from the real system
        # python3/ruff by a marker string each stub prints instead of
        # actually running anything.
        self.fake_venv = self.tmp / "fake-venv"
        fake_bin = self.fake_venv / "bin"
        fake_bin.mkdir(parents=True)
        _write_executable(fake_bin / "python3", "#!/bin/sh\necho FAKE_PY_RAN\n")
        _write_executable(fake_bin / "ruff", "#!/bin/sh\necho FAKE_RUFF_RAN\n")

    def run_script(self):
        env = {
            # Deliberately excludes fake_venv/bin, simulating PATH being
            # reset after activation without VIRTUAL_ENV being touched.
            "PATH": "/usr/bin:/bin",
            "VIRTUAL_ENV": str(self.fake_venv),
            "HOME": os.environ.get("HOME", "/root"),
        }
        return subprocess.run(
            ["./run_tests.sh"],
            cwd=self.workdir,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_uses_active_virtualenv_even_when_path_does_not_show_it(self):
        result = self.run_script()

        self.assertIn(
            "FAKE_PY_RAN", result.stdout, msg=result.stdout + result.stderr
        )
        self.assertIn(
            "FAKE_RUFF_RAN", result.stdout, msg=result.stdout + result.stderr
        )
        self.assertIn(str(self.fake_venv), result.stdout)


if __name__ == "__main__":
    unittest.main()
