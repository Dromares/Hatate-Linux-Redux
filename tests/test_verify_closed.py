"""Regression coverage for scripts/verify_closed.sh.

Builds a throwaway bare "origin" + checkout pair (never the real repo) and
asserts the subject-line-only matching contract: a ticket id that only
appears in a *commit body* (changelog prose, a worked example in another
commit's message - exactly how this file's own DAN-187 merge commit
mentions DAN-152) must not produce a false PASS, and must not shadow an
older commit that legitimately carries the id in its subject.

DAN-1457 adds the merged-PR evidence fallback: a ticket whose own text names
a PR and a SHA that belong to a commit on the ref (squash "(#N)" or merge
commit) is a PASS even with no id in any subject, but a cited SHA that is
merely an old ancestor of the ref, or a PR/SHA pair that does not agree,
must stay NONE FOUND so the fallback hides no real false-done.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_closed.sh"


def run_git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


class VerifyClosedTest(unittest.TestCase):
    def setUp(self):
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="verify-closed-test-")
        self.tmp = Path(self.tmp_ctx.name)
        self.addCleanup(self.tmp_ctx.cleanup)

        self.origin = self.tmp / "origin.git"
        run_git(["init", "--bare", "-b", "main", str(self.origin)], cwd=self.tmp)

        self.checkout = self.tmp / "checkout"
        run_git(["clone", str(self.origin), str(self.checkout)], cwd=self.tmp)
        run_git(["config", "user.email", "test@example.com"], cwd=self.checkout)
        run_git(["config", "user.name", "Test"], cwd=self.checkout)

        # Older commit: ticket id genuinely in the subject - this is the
        # real fix for DAN-900.
        (self.checkout / "a.txt").write_text("a\n")
        run_git(["add", "a.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-900): real fix lands here"], cwd=self.checkout)
        self.real_fix_sha = run_git(
            ["rev-parse", "HEAD"], cwd=self.checkout
        ).stdout.strip()

        # Newer commit: subject carries no ticket id, but the body
        # prose-mentions DAN-900 and DAN-901 as a worked example/changelog
        # note - neither ticket's code lives in this commit's diff.
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "b.txt"], cwd=self.checkout)
        run_git(
            [
                "commit",
                "-m",
                "docs: note caveats\n\nSee DAN-900 and DAN-901 as discussed in standup.",
            ],
            cwd=self.checkout,
        )

        # Squash-merged PR #7: subject ends "(#7)" and carries a different
        # ticket id (the parent's), like every merge child ticket's PR here.
        (self.checkout / "c.txt").write_text("c\n")
        run_git(["add", "c.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "DAN-950: the parent feature (#7)"], cwd=self.checkout)
        self.squash_sha = run_git(["rev-parse", "HEAD"], cwd=self.checkout).stdout.strip()

        run_git(["push", "origin", "main"], cwd=self.checkout)

        self.evidence = self.tmp / "evidence"
        self.evidence.mkdir()

    def write_evidence(self, ticket_id, text):
        (self.evidence / f"{ticket_id}.txt").write_text(text)

    def run_verify(self, *ticket_ids):
        return subprocess.run(
            ["bash", str(SCRIPT), *ticket_ids],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            env={**os.environ, "VERIFY_CLOSED_EVIDENCE_DIR": str(self.evidence)},
        )

    def test_body_only_mention_does_not_shadow_the_real_subject_match(self):
        result = self.run_verify("DAN-900")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)
        self.assertIn(self.real_fix_sha[:7], result.stdout)

    def test_body_only_mention_is_not_a_false_pass(self):
        result = self.run_verify("DAN-901")

        self.assertEqual(result.returncode, 1)
        self.assertIn("NONE FOUND", result.stdout)
        self.assertNotIn("PASS", result.stdout)

    def test_merge_child_with_pr_and_sha_evidence_passes(self):
        self.write_evidence(
            "DAN-951", f"Merged PR #7 as {self.squash_sha[:7]}, on origin/main."
        )

        result = self.run_verify("DAN-951")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (merged PR #7", result.stdout)
        self.assertIn(self.squash_sha[:7], result.stdout)

    def test_old_ancestor_sha_with_a_pr_is_not_a_false_pass(self):
        # real_fix_sha is an ancestor of main but not part of PR #7.
        self.write_evidence(
            "DAN-952", f"PR #7 merged; based on {self.real_fix_sha[:7]}."
        )

        result = self.run_verify("DAN-952")

        self.assertEqual(result.returncode, 1)
        self.assertIn("NONE FOUND", result.stdout)

    def test_pr_not_on_the_ref_is_not_a_false_pass(self):
        self.write_evidence(
            "DAN-953", f"PR #99 merged as {self.squash_sha[:7]}."
        )

        result = self.run_verify("DAN-953")

        self.assertEqual(result.returncode, 1)
        self.assertIn("NONE FOUND", result.stdout)

    def test_sha_without_a_pr_is_not_a_false_pass(self):
        self.write_evidence("DAN-954", f"Landed as {self.squash_sha[:7]}.")

        result = self.run_verify("DAN-954")

        self.assertEqual(result.returncode, 1)
        self.assertIn("NONE FOUND", result.stdout)


if __name__ == "__main__":
    unittest.main()
