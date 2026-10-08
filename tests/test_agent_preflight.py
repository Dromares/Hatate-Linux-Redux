"""Regression coverage for scripts/agent-preflight.sh.

Builds a throwaway "origin" + "checkout" pair of git repos per test (never
the real repo) and asserts the guard's abort-only contract: it must refuse
to proceed on a dirty or unpushed checkout, and it must never discard
anything while doing so.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-preflight.sh"


def run_git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


class AgentPreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-preflight-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.origin = self.tmp / "origin.git"
        run_git(["init", "--bare", "-b", "main", str(self.origin)], cwd=self.tmp)

        self.checkout = self.tmp / "checkout"
        run_git(["clone", str(self.origin), str(self.checkout)], cwd=self.tmp)
        run_git(["config", "user.email", "test@example.com"], cwd=self.checkout)
        run_git(["config", "user.name", "Test"], cwd=self.checkout)

        (self.checkout / "README.md").write_text("hello\n")
        run_git(["add", "README.md"], cwd=self.checkout)
        run_git(["commit", "-m", "initial"], cwd=self.checkout)
        run_git(["push", "origin", "main"], cwd=self.checkout)

    def run_preflight(self, env=None):
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.checkout,
            capture_output=True,
            text=True,
            env=env,
        )

    def write_fake_gh(self, *, list_stdout, view_stdout):
        """A stub `gh` that answers `pr list --head ...` and `pr view ...`.

        Dispatches on the subcommand (argv[1]) rather than modeling full gh
        syntax, since branch_ship_verdict only ever issues these two shapes
        (see ops/ancestry_guard.py). Real `gh` can't be used here: these are
        throwaway local repos with no matching GitHub PRs to find.
        """
        path = self.tmp / "fake-gh"
        path.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            f'  "pr list") echo \'{list_stdout}\' ;;\n'
            f'  "pr view") echo \'{view_stdout}\' ;;\n'
            "esac\n"
        )
        path.chmod(0o755)
        return str(path)

    def test_aborts_on_dirty_working_tree(self):
        (self.checkout / "README.md").write_text("hello, dirtied\n")

        result = self.run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ABORT", result.stderr)
        # The guard must not touch anything it's aborting on.
        status = run_git(["status", "--porcelain"], cwd=self.checkout)
        self.assertIn("README.md", status.stdout)

    def test_aborts_on_unpushed_commit(self):
        (self.checkout / "new_file.txt").write_text("local only\n")
        run_git(["add", "new_file.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "local-only commit"], cwd=self.checkout)
        branch_before = run_git(
            ["rev-parse", "HEAD"], cwd=self.checkout
        ).stdout.strip()

        result = self.run_preflight()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ABORT", result.stderr)
        # Nothing was reset or rewritten while aborting.
        branch_after = run_git(["rev-parse", "HEAD"], cwd=self.checkout).stdout.strip()
        self.assertEqual(branch_before, branch_after)

    def test_succeeds_and_syncs_when_clean_and_pushed(self):
        run_git(["checkout", "-b", "some/stale/branch"], cwd=self.checkout)

        result = self.run_preflight()

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)
        branch = run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.checkout
        ).stdout.strip()
        self.assertEqual(branch, "main")
        status = run_git(["status", "--porcelain"], cwd=self.checkout)
        self.assertEqual(status.stdout, "")

    def test_reclaims_branch_squash_merged_under_a_combined_commit(self):
        # DAN-272: a branch with two commits, squash-merged on GitHub into
        # ONE commit on main. `git cherry` compares each commit's own
        # patch-id against main one at a time, so neither of this branch's
        # commits patch-matches the combined squash commit even though
        # nothing is actually missing from main - confirmed live on this
        # repo's own agent/beatrice/DAN-193-rule34us-hydrus-checks and
        # agent/beatrice/DAN-217-is-ancestor-squash-fix branches, both fully
        # merged yet unclearable by git cherry alone. Before this fix, the
        # branch was left untouched by the reclaim loop and then tripped the
        # "local branches have commits not on any remote" abort below it,
        # even though the content had, in fact, shipped.
        branch = "agent/x/DAN-9-squash-fix"
        run_git(["checkout", "-q", "-b", branch], cwd=self.checkout)
        (self.checkout / "a.txt").write_text("a\n")
        run_git(["add", "a.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-9): part one"], cwd=self.checkout)
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-9): part two"], cwd=self.checkout)

        # The branch itself is never pushed - only the squashed PR reached
        # origin, as a brand-new commit with no parent in common with it.
        run_git(["checkout", "-q", "main"], cwd=self.checkout)
        (self.checkout / "a.txt").write_text("a\n")
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "a.txt", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-9): part one and two (#42)"], cwd=self.checkout)
        run_git(["push", "origin", "main"], cwd=self.checkout)

        gh_bin = self.write_fake_gh(
            list_stdout='[{"number": 42}]', view_stdout='{"state": "MERGED"}'
        )
        # The throwaway checkout has no `ops/` package of its own (it is
        # deliberately not the real repo - see the module docstring), so
        # `python3 -m ops.ancestry_guard` needs PYTHONPATH pointed at this
        # repo to find it, same as the real preflight script finds it via
        # its own `cd "$(git rev-parse --show-toplevel)"` into a checkout
        # that actually contains `ops/`.
        pythonpath = os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
        )
        env = {**os.environ, "AGENT_PREFLIGHT_GH_BIN": gh_bin, "PYTHONPATH": pythonpath}

        # Sanity check the trap is real before asserting the fix works.
        cherry = run_git(["cherry", "origin/main", branch], cwd=self.checkout)
        self.assertTrue(
            all(line.startswith("+") for line in cherry.stdout.splitlines() if line),
            f"test setup sanity check: git cherry should fail to clear either "
            f"commit individually, got: {cherry.stdout!r}",
        )

        result = self.run_preflight(env=env)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        )
        self.assertNotEqual(
            exists.returncode, 0, f"{branch} should have been reclaimed, not left for the abort check"
        )

    def test_reclaims_shipped_branch_locked_by_a_clean_abandoned_worktree(self):
        # DAN-387: a branch verified shipped by ops.ancestry_guard still
        # survives `git branch -D` when it's checked out in a *different*
        # linked worktree - `git branch -D` refuses that regardless of
        # content, so the old code silently left the branch in place and
        # the next abort check blamed "commits not on any remote" instead
        # of naming the real obstacle. If that worktree is clean, the lock
        # is pure leftover state and should be cleared automatically.
        branch = "agent/x/DAN-11-worktree-locked"
        run_git(["checkout", "-q", "-b", branch], cwd=self.checkout)
        (self.checkout / "a.txt").write_text("a\n")
        run_git(["add", "a.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-11): part one"], cwd=self.checkout)
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-11): part two"], cwd=self.checkout)
        run_git(["checkout", "-q", "main"], cwd=self.checkout)

        (self.checkout / "a.txt").write_text("a\n")
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "a.txt", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-11): part one and two (#43)"], cwd=self.checkout)
        run_git(["push", "origin", "main"], cwd=self.checkout)

        worktree_dir = self.tmp / "abandoned-worktree"
        run_git(["worktree", "add", str(worktree_dir), branch], cwd=self.checkout)
        # Sanity check the lock is real before asserting the fix clears it.
        delete_attempt = run_git(["branch", "-D", branch], cwd=self.checkout, check=False)
        self.assertNotEqual(
            delete_attempt.returncode, 0,
            "test setup sanity check: git branch -D should refuse while the "
            "branch is checked out in another worktree",
        )

        gh_bin = self.write_fake_gh(
            list_stdout='[{"number": 43}]', view_stdout='{"state": "MERGED"}'
        )
        pythonpath = os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
        )
        env = {**os.environ, "AGENT_PREFLIGHT_GH_BIN": gh_bin, "PYTHONPATH": pythonpath}

        result = self.run_preflight(env=env)

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        )
        self.assertNotEqual(
            exists.returncode, 0,
            f"{branch} should have been reclaimed once its locking worktree was removed",
        )
        self.assertFalse(
            worktree_dir.exists(),
            "the abandoned, clean worktree should have been removed",
        )

    def test_leaves_shipped_branch_locked_by_a_dirty_worktree_and_names_it(self):
        # The flip side: the same shipped-but-worktree-locked branch, except
        # the worktree itself has uncommitted changes. Nothing may be
        # discarded, so both the worktree and the branch must survive, and
        # the abort message must name the real obstacle (the dirty
        # worktree) rather than folding it into the generic "commits not on
        # any remote" line.
        branch = "agent/x/DAN-12-worktree-locked-dirty"
        run_git(["checkout", "-q", "-b", branch], cwd=self.checkout)
        (self.checkout / "a.txt").write_text("a\n")
        run_git(["add", "a.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-12): part one"], cwd=self.checkout)
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-12): part two"], cwd=self.checkout)
        run_git(["checkout", "-q", "main"], cwd=self.checkout)

        (self.checkout / "a.txt").write_text("a\n")
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "a.txt", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-12): part one and two (#44)"], cwd=self.checkout)
        run_git(["push", "origin", "main"], cwd=self.checkout)

        worktree_dir = self.tmp / "dirty-worktree"
        run_git(["worktree", "add", str(worktree_dir), branch], cwd=self.checkout)
        (worktree_dir / "uncommitted.txt").write_text("dirty\n")

        gh_bin = self.write_fake_gh(
            list_stdout='[{"number": 44}]', view_stdout='{"state": "MERGED"}'
        )
        pythonpath = os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
        )
        env = {**os.environ, "AGENT_PREFLIGHT_GH_BIN": gh_bin, "PYTHONPATH": pythonpath}

        result = self.run_preflight(env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ABORT", result.stderr)
        self.assertIn("dirty linked worktree", result.stderr)
        self.assertIn(branch, result.stderr)
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        )
        self.assertEqual(
            exists.returncode, 0,
            f"{branch} must survive while its worktree is dirty",
        )
        self.assertTrue(worktree_dir.exists(), "the dirty worktree must not be touched")
        self.assertEqual(
            (worktree_dir / "uncommitted.txt").read_text(), "dirty\n",
            "the dirty worktree's contents must not be touched",
        )

    def test_leaves_shipped_branch_locked_by_a_worktree_lock_and_names_it(self):
        # DAN-481: the same shipped-but-worktree-locked branch as DAN-387,
        # except the worktree is clean but administratively locked via
        # `git worktree lock` (e.g. to protect an in-progress run from
        # accidental `git worktree remove`). Before this fix, a clean
        # worktree always took the "safe to remove" branch, `git worktree
        # remove` failed on the lock independent of dirtiness, and the
        # branch was left in place without ever being named - the abort
        # below fell through to the generic "commits not on any remote"
        # line, sending whoever's debugging it hunting for lost/unpushed
        # work instead of pointing at `git worktree unlock`.
        branch = "agent/x/DAN-13-worktree-admin-locked"
        run_git(["checkout", "-q", "-b", branch], cwd=self.checkout)
        (self.checkout / "a.txt").write_text("a\n")
        run_git(["add", "a.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-13): part one"], cwd=self.checkout)
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-13): part two"], cwd=self.checkout)
        run_git(["checkout", "-q", "main"], cwd=self.checkout)

        (self.checkout / "a.txt").write_text("a\n")
        (self.checkout / "b.txt").write_text("b\n")
        run_git(["add", "a.txt", "b.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-13): part one and two (#45)"], cwd=self.checkout)
        run_git(["push", "origin", "main"], cwd=self.checkout)

        worktree_dir = self.tmp / "locked-worktree"
        run_git(["worktree", "add", str(worktree_dir), branch], cwd=self.checkout)
        run_git(["worktree", "lock", str(worktree_dir), "--reason", "simulated admin lock"], cwd=self.checkout)
        # Sanity check the worktree is clean - this is the locked-but-clean
        # case, distinct from DAN-387's locked-and-dirty case.
        status = run_git(["status", "--porcelain"], cwd=worktree_dir)
        self.assertEqual(status.stdout, "", "test setup sanity check: worktree must be clean")

        gh_bin = self.write_fake_gh(
            list_stdout='[{"number": 45}]', view_stdout='{"state": "MERGED"}'
        )
        pythonpath = os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
        )
        env = {**os.environ, "AGENT_PREFLIGHT_GH_BIN": gh_bin, "PYTHONPATH": pythonpath}

        result = self.run_preflight(env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ABORT", result.stderr)
        self.assertIn("locked", result.stderr)
        self.assertIn(branch, result.stderr)
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        )
        self.assertEqual(
            exists.returncode, 0,
            f"{branch} must survive while its worktree is locked",
        )
        self.assertTrue(worktree_dir.exists(), "the locked worktree must not be touched")

    def test_does_not_reclaim_branch_with_no_matching_pr(self):
        # The flip side of the squash-merge case: a branch git cherry can't
        # clear AND GitHub has no PR for (DAN-272's agent/virgil/DAN-213-cont
        # shape - a second, still-open round of work on a ticket whose first
        # round already shipped separately). This must still hit the abort
        # check below, not be silently discarded.
        branch = "agent/x/DAN-10-still-open"
        run_git(["checkout", "-q", "-b", branch], cwd=self.checkout)
        (self.checkout / "c.txt").write_text("c\n")
        run_git(["add", "c.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-10): part one"], cwd=self.checkout)
        (self.checkout / "d.txt").write_text("d\n")
        run_git(["add", "d.txt"], cwd=self.checkout)
        run_git(["commit", "-m", "fix(DAN-10): part two"], cwd=self.checkout)
        run_git(["checkout", "-q", "main"], cwd=self.checkout)

        gh_bin = self.write_fake_gh(list_stdout="[]", view_stdout="{}")
        # The throwaway checkout has no `ops/` package of its own (it is
        # deliberately not the real repo - see the module docstring), so
        # `python3 -m ops.ancestry_guard` needs PYTHONPATH pointed at this
        # repo to find it, same as the real preflight script finds it via
        # its own `cd "$(git rev-parse --show-toplevel)"` into a checkout
        # that actually contains `ops/`.
        pythonpath = os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")])
        )
        env = {**os.environ, "AGENT_PREFLIGHT_GH_BIN": gh_bin, "PYTHONPATH": pythonpath}

        result = self.run_preflight(env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ABORT", result.stderr)
        exists = run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.checkout,
            check=False,
        )
        self.assertEqual(
            exists.returncode, 0, f"{branch} carries unmerged work and must not be deleted"
        )


if __name__ == "__main__":
    unittest.main()
