"""Regression coverage for scripts/privacy_audit.sh.

Builds throwaway bare "origin" + checkout repo pairs (never the real repo,
never real PII - every fixture value below is an obviously-synthetic
placeholder) and asserts the DAN-668 audit method's key behaviors:

- a tracked-tree leak shaped like DAN-668's A2/A3 (an absolute home path
  committed outside any test/fixture directory) fails the default run,
  and the same leak inside a test-fixture-shaped path does not - mirroring
  DAN-668's own adjudication that such paths inside tests/ are fixtures,
  not leaks (verified against the real repo's
  tests/test_lens_dependency_alerts.py before this script shipped).
- the commit-metadata (A1) check counts non-safe-domain commit identities
  correctly, and does not gate the overall exit code unless
  --fail-on-metadata is passed - this is deliberate, see the long comment
  in the script itself.
- --patterns-file catches an exact operator-supplied literal that the
  built-in shape-based patterns would never know to look for.
- --history reaches a blob that was deleted from the tree and is no
  longer visible to a plain tracked-tree scan - the DAN-668 "A5" shape.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "privacy_audit.sh"

# Some sandboxed runs shadow PATH with a GitHub-broker git wrapper that
# strips and reinjects GIT_AUTHOR_*/GIT_COMMITTER_* unconditionally (see
# the Paperclip runtime's git wrapper), which defeats this test's control
# over commit identity in its own throwaway repo. Use the real binary
# directly so identity assertions are deterministic regardless of
# whichever broker wrapper happens to be first on PATH in a given run.
GIT_BIN = "/usr/bin/git" if Path("/usr/bin/git").exists() else (shutil.which("git") or "git")

# Obviously-synthetic placeholders - not real usernames, emails, or names.
FAKE_HOME_PATH = "/home/testuser-synthetic/notes.txt"
FAKE_PERSONAL_EMAIL = "fake.author@personalmail.example"
FAKE_SAFE_DOMAIN = "agents.example.test"
FAKE_SAFE_EMAIL = f"builder@{FAKE_SAFE_DOMAIN}"
SECRET_MARKER = "SYNTHETIC-SECRET-MARKER-NOT-REAL"


def run_git(args, cwd, env=None, check=True):
    return subprocess.run(
        [GIT_BIN, *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


class PrivacyAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="privacy-audit-test-")
        self.tmp = Path(self.tmp_ctx.name)
        self.addCleanup(self.tmp_ctx.cleanup)

        self.origin = self.tmp / "origin.git"
        run_git(["init", "--bare", "-b", "main", str(self.origin)], cwd=self.tmp)

        self.checkout = self.tmp / "checkout"
        run_git(["clone", str(self.origin), str(self.checkout)], cwd=self.tmp)
        run_git(["config", "user.email", FAKE_SAFE_EMAIL], cwd=self.checkout)
        run_git(["config", "user.name", "Test Builder"], cwd=self.checkout)

    def commit(self, name, content, message, author_email=None):
        path = self.checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        run_git(["add", name], cwd=self.checkout)
        env = None
        if author_email:
            env = {**os.environ, "GIT_AUTHOR_EMAIL": author_email, "GIT_COMMITTER_EMAIL": author_email}
        run_git(["commit", "-m", message], cwd=self.checkout, env=env)
        return run_git(["rev-parse", "HEAD"], cwd=self.checkout).stdout.strip()

    def remove(self, name, message):
        (self.checkout / name).unlink()
        run_git(["add", "-A"], cwd=self.checkout)
        run_git(["commit", "-m", message], cwd=self.checkout)
        return run_git(["rev-parse", "HEAD"], cwd=self.checkout).stdout.strip()

    def run_audit(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=self.checkout,
            capture_output=True,
            text=True,
        )

    def test_fails_on_non_test_absolute_home_path_leak(self):
        sha = self.commit(
            "profiling/report.txt",
            f"profiled function at {FAKE_HOME_PATH}\n",
            "add synthetic profiling report",
        )

        result = self.run_audit(sha)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("FAIL", result.stdout)
        self.assertIn("profiling/report.txt", result.stdout)

    def test_passes_once_the_leak_is_removed_from_the_tree(self):
        sha = self.commit(
            "profiling/report.txt",
            f"profiled function at {FAKE_HOME_PATH}\n",
            "add synthetic profiling report",
        )
        clean_sha = self.remove("profiling/report.txt", "remove synthetic profiling report")

        dirty_result = self.run_audit(sha)
        clean_result = self.run_audit(clean_sha)

        self.assertNotEqual(dirty_result.returncode, 0, dirty_result.stdout)
        self.assertEqual(clean_result.returncode, 0, clean_result.stdout)
        self.assertIn("PASS", clean_result.stdout)

    def test_same_shaped_path_inside_tests_dir_is_review_not_fatal(self):
        sha = self.commit(
            "tests/test_something.py",
            f'FIXTURE_PATH = "{FAKE_HOME_PATH}"\n',
            "add synthetic test fixture",
        )

        result = self.run_audit(sha)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS", result.stdout)
        self.assertIn("REVIEW", result.stdout)

    def test_commit_metadata_counts_non_safe_identity_without_gating_by_default(self):
        self.commit("a.txt", "a\n", "safe commit", author_email=FAKE_SAFE_EMAIL)
        self.commit("b.txt", "b\n", "personal commit one", author_email=FAKE_PERSONAL_EMAIL)
        sha = self.commit("c.txt", "c\n", "personal commit two", author_email=FAKE_PERSONAL_EMAIL)

        safe_domains_file = self.tmp / "safe-domains.txt"
        safe_domains_file.write_text(f"@{FAKE_SAFE_DOMAIN}$\n")

        result = self.run_audit("--safe-domains-file", str(safe_domains_file), sha)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("commit_metadata_a1: RAN - count=2", result.stdout)

    def test_fail_on_metadata_gates_the_exit_code(self):
        sha = self.commit("a.txt", "a\n", "personal commit", author_email=FAKE_PERSONAL_EMAIL)

        default_result = self.run_audit(sha)
        strict_result = self.run_audit("--fail-on-metadata", sha)

        self.assertEqual(default_result.returncode, 0, default_result.stdout)
        self.assertNotEqual(strict_result.returncode, 0, strict_result.stdout)

    def test_patterns_file_catches_an_operator_supplied_literal(self):
        sha = self.commit(
            "config/defaults.json",
            f'{{"value": "{SECRET_MARKER}"}}\n',
            "add config carrying the synthetic marker",
        )
        patterns_file = self.tmp / "patterns.txt"
        patterns_file.write_text(f"synthetic-marker\t{SECRET_MARKER}\n")

        without_patterns = self.run_audit(sha)
        with_patterns = self.run_audit("--patterns-file", str(patterns_file), sha)

        self.assertEqual(without_patterns.returncode, 0, without_patterns.stdout)
        self.assertNotEqual(with_patterns.returncode, 0, with_patterns.stdout)
        self.assertIn("synthetic-marker", with_patterns.stdout)

    def test_history_mode_reaches_a_deleted_blob_the_tree_scan_cannot_see(self):
        sha = self.commit(
            "scratch/leak.txt",
            f"leaked at {FAKE_HOME_PATH}\n",
            "add then remove synthetic leak",
        )
        clean_sha = self.remove("scratch/leak.txt", "remove synthetic leak")
        self.assertNotEqual(sha, clean_sha)

        default_result = self.run_audit(clean_sha)
        history_result = self.run_audit("--history", clean_sha)

        self.assertEqual(default_result.returncode, 0, default_result.stdout)
        self.assertIn(
            "history_blob_scan: SKIPPED",
            default_result.stdout,
        )
        self.assertNotEqual(history_result.returncode, 0, history_result.stdout)
        self.assertIn("history_blob_scan: RAN - FINDINGS", history_result.stdout)

    def test_help_documents_the_metadata_scanner_blind_spot(self):
        result = self.run_audit("--help")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("commit metadata", result.stdout.lower())
        self.assertIn("secret scanner", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
