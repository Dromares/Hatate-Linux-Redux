"""Regression coverage for scripts/ci_local_matrix.sh's stale-base refusal (DAN-1194).

DAN-652 says an attestation against a head that is not rebased on current
origin/main "does not count". Before DAN-1194 the script only annotated
`merge_base_origin_main: stale (...)` and still reported `overall: PASS`;
it must now refuse (exit 2, REFUSED block, no attestation block) instead.

Builds a throwaway bare "origin" + clone and puts a stub `uv` first on PATH
so the script is never allowed to provision interpreters or run the real
suite: the stub records that it was reached and fails, which is enough to
tell "refused before any work" from "got past the gate".
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "ci_local_matrix.sh"

# Use the real git binary: some sandboxes shadow PATH with a broker wrapper
# that rewrites identity env vars (same rationale as test_privacy_audit.py).
GIT_BIN = "/usr/bin/git" if Path("/usr/bin/git").exists() else (shutil.which("git") or "git")


def git(args, cwd):
    return subprocess.run(
        [GIT_BIN, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@agents.example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@agents.example.test",
        },
    ).stdout.strip()


class CiLocalMatrixStaleBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ci-matrix-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        origin = self.tmp / "origin.git"
        subprocess.run([GIT_BIN, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = self.tmp / "repo"
        subprocess.run([GIT_BIN, "clone", "-q", str(origin), str(self.repo)], check=True, capture_output=True)
        git(["checkout", "-q", "-b", "main"], self.repo)
        (self.repo / "a.txt").write_text("a\n")
        git(["add", "a.txt"], self.repo)
        git(["commit", "-q", "-m", "base"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)

        bindir = self.tmp / "bin"
        bindir.mkdir()
        self.uv_hits = self.tmp / "uv-hits"
        stub = bindir / "uv"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{self.uv_hits}"\nexit 1\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        # Drop BASH_ENV: some sandboxes point it at an rc file that re-prepends
        # PATH, which would put the real uv ahead of the stub.
        self.env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
        self.env["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"

    def run_script(self, sha):
        return subprocess.run(
            ["bash", str(SCRIPT), sha], cwd=self.repo, capture_output=True, text=True, env=self.env
        )

    def test_stale_merge_base_is_refused_before_any_work(self):
        # Branch off the base, then let origin/main move on.
        git(["checkout", "-q", "-b", "feature"], self.repo)
        (self.repo / "b.txt").write_text("b\n")
        git(["add", "b.txt"], self.repo)
        git(["commit", "-q", "-m", "feature"], self.repo)
        git(["checkout", "-q", "main"], self.repo)
        (self.repo / "c.txt").write_text("c\n")
        git(["add", "c.txt"], self.repo)
        git(["commit", "-q", "-m", "main moves on"], self.repo)
        git(["push", "-q", "origin", "main"], self.repo)
        git(["checkout", "-q", "feature"], self.repo)
        sha = git(["rev-parse", "HEAD"], self.repo)

        result = self.run_script(sha)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("CI-LOCAL-MATRIX-REFUSED", result.stderr)
        self.assertIn("stale merge-base", result.stderr)
        self.assertIn("1 commit(s) ahead", result.stderr)
        # No attestation block at all, so no `overall: PASS` line to quote.
        self.assertNotIn("CI-LOCAL-MATRIX-ATTESTATION", result.stdout + result.stderr)
        self.assertNotIn("overall:", result.stdout + result.stderr)
        # Refused before any interpreter provisioning / test work.
        self.assertFalse(self.uv_hits.exists(), "uv was invoked after a stale-base refusal")

    def test_up_to_date_branch_gets_past_the_gate(self):
        git(["checkout", "-q", "-b", "feature"], self.repo)
        (self.repo / "b.txt").write_text("b\n")
        git(["add", "b.txt"], self.repo)
        git(["commit", "-q", "-m", "feature"], self.repo)
        sha = git(["rev-parse", "HEAD"], self.repo)

        result = self.run_script(sha)

        self.assertNotIn("REFUSED", result.stderr)
        # The stub uv fails every provision, so the run itself is FAIL, but it
        # must have reached the legs and printed an up_to_date attestation.
        self.assertTrue(self.uv_hits.exists(), "script never got past the merge-base gate")
        self.assertIn("merge_base_origin_main: up_to_date", result.stdout)


if __name__ == "__main__":
    unittest.main()
