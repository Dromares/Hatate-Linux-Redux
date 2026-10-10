"""Runs the seat-health watchdog originals' own test runners (DAN-1216).

tests/ops_runners/test_*.py are the verbatim, plain-assert runner scripts
(`python3 test_x.py`) that shipped with ops/seat_watchdog.py,
ops/stranded_review.py, ops/routine_checkout_watchdog.py and
ops/tick_finalize.py. unittest discovery cannot collect them (module-level
`def test_*` functions, bare `import seat_watchdog`), and they must stay
byte-identical to the originals, so each is run in a subprocess with ops/ on
PYTHONPATH and its own "N/N passed" line is asserted against a pinned count.
The pin is what proves all 202 cases executed: a runner that silently
collected fewer would still exit 0.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNERS = Path(__file__).resolve().parent / "ops_runners"

EXPECTED = {
    "test_seat_watchdog.py": 59,
    "test_stranded_review.py": 60,
    "test_routine_checkout_watchdog.py": 73,
    "test_tick_finalize.py": 10,
}


class OpsWatchdogRunners(unittest.TestCase):
    def run_runner(self, name: str) -> None:
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "ops"))
        proc = subprocess.run(
            [sys.executable, str(RUNNERS / name)],
            cwd=RUNNERS,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        match = re.search(r"^(\d+)/(\d+) passed$", proc.stdout, re.MULTILINE)
        self.assertIsNotNone(match, proc.stdout[-500:])
        assert match is not None
        passed, total = int(match.group(1)), int(match.group(2))
        self.assertEqual(passed, total)
        self.assertEqual(total, EXPECTED[name])

    def test_seat_watchdog(self) -> None:
        self.run_runner("test_seat_watchdog.py")

    def test_stranded_review(self) -> None:
        self.run_runner("test_stranded_review.py")

    def test_routine_checkout_watchdog(self) -> None:
        self.run_runner("test_routine_checkout_watchdog.py")

    def test_tick_finalize(self) -> None:
        self.run_runner("test_tick_finalize.py")

    def test_expected_total_is_202(self) -> None:
        self.assertEqual(sum(EXPECTED.values()), 202)


if __name__ == "__main__":
    unittest.main()
