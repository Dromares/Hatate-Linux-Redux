"""DAN-116: the done/origin-main ancestry guard's content-aware classifier.

Ancestry alone (`git merge-base --is-ancestor <sha> origin/main`) reports a
false positive on every squash-merged PR: the squash produces a brand new
commit, so the source commit is never an ancestor of `main` again, even
though the change shipped. DAN-114 hit this on DAN-81 and DAN-112 hit it
again on DAN-95/DAN-96's own recovery (both squash-merged as PR #37).

Every repo fixture here is a synthetic, throwaway git repository built in a
tmpdir - not the real Hatate-Linux-Redux history - so these tests are
deterministic, need no network, and do not depend on any branch in this
repo surviving cleanup.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.ancestry_guard import (
    branch_ship_verdict,
    classify_ship_status,
    commit_exists,
    content_grep_hits,
    extract_pr_numbers,
    extract_shas,
    extract_ticket_ids,
    gh_pr_numbers_for_branch,
    gh_pr_state,
    github_pr_state_lookup_factory,
    is_ancestor,
)


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: str) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "root")


def _commit(repo: str, message: str, filename: str, content: str) -> str:
    path = Path(repo) / filename
    path.write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _naive_ancestry_only_verdict(repo: str, shas: list[str], ref: str) -> bool:
    """What a guard with only DAN-116's original sketch would report.

    Stands in for the "naive" tool the issue warns against: ancestry and
    nothing else. Used to prove the trap is real, not just asserted away.
    """
    return any(is_ancestor(repo, sha, ref) for sha in shas)


class AncestryPrimitivesTests(unittest.TestCase):
    def test_is_ancestor_true_for_commit_on_branch(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            sha = _commit(repo, "DAN-1: add thing", "a.txt", "a")
            self.assertTrue(is_ancestor(repo, sha, "main"))

    def test_is_ancestor_false_for_commit_not_on_branch(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "feature")
            sha = _commit(repo, "DAN-2: add thing", "b.txt", "b")
            _git(repo, "checkout", "-q", "main")
            self.assertFalse(is_ancestor(repo, sha, "main"))

    def test_commit_exists_true_and_false(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            sha = _commit(repo, "DAN-3: add thing", "c.txt", "c")
            self.assertTrue(commit_exists(repo, sha))
            self.assertFalse(commit_exists(repo, "deadbeefcafefacade1234567890abcdefabcd"))


class ExtractRefsTests(unittest.TestCase):
    def test_extract_shas_ignores_hex_lookalike_words(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            real_sha = _commit(repo, "DAN-4: add thing", "d.txt", "d")
            text = (
                f"see {real_sha} for the fix - decaf, facade, deadbeef, "
                "cafebabe and deedface are just words, not commits"
            )
            self.assertEqual(extract_shas(repo, text), [real_sha])

    def test_extract_shas_empty_text(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            self.assertEqual(extract_shas(repo, ""), [])
            self.assertEqual(extract_shas(repo, None), [])  # type: ignore[arg-type]

    def test_extract_pr_numbers(self) -> None:
        text = "Opened PR #37: fix(DAN-95,DAN-96); see also pull/12 and (#8) above"
        self.assertEqual(extract_pr_numbers(text), [8, 12, 37])

    def test_extract_pr_numbers_none(self) -> None:
        self.assertEqual(extract_pr_numbers("no references here"), [])


class ClassifyShipStatusTests(unittest.TestCase):
    """The corpus: DAN-114's six audited branches plus DAN-95/DAN-96, each
    reconstructed as a minimal synthetic shape rather than asserted as prose.
    """

    def test_no_shas_and_no_grep_hit_is_orphaned(self) -> None:
        # classify_ship_status is the low-level primitive: given nothing but
        # an issue id and no SHAs, it still tries content grep before giving
        # up. Whether "nothing to check at all" should even reach this
        # function is the sweep layer's call (see evaluate_target), not
        # this one's.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            verdict = classify_ship_status(repo, "DAN-99", [], ref="main")
            self.assertFalse(verdict.shipped)
            self.assertEqual(verdict.reason, "orphaned")

    def test_no_shas_but_content_grep_hit_is_shipped(self) -> None:
        # DAN-40/DAN-66/DAN-68 shape, and this guard's own first false
        # positive: the issue text names a PR but no literal commit SHA
        # could be resolved. Ancestry has nothing to check, so this must
        # fall through to content grep rather than default to "unshipped".
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _commit(repo, "feat(DAN-68): auto-decode the encoded slash (#21)", "n.txt", "n")
            verdict = classify_ship_status(repo, "DAN-68", [], ref="main")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "content_grep")

    def test_exact_commit_on_main_is_shipped(self) -> None:
        # DAN-18 / DAN-37 / DAN-52 / DAN-68 shape: the referenced commit is
        # itself an ancestor of main. No squash involved.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            sha = _commit(repo, "DAN-18: ship it", "e.txt", "e")
            verdict = classify_ship_status(repo, "DAN-18", [sha], ref="main")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "ancestor")

    def test_zero_ahead_commit_with_no_surviving_ref_is_shipped(self) -> None:
        # DAN-65 shape: 0-ahead, and by the time anyone checks, the ref
        # itself may be gone (pruned branch). Ancestry still answers this
        # correctly as long as *a* SHA is known, since 0-ahead by
        # construction means it's already on main.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            sha = _commit(repo, "DAN-65: ship it", "f.txt", "f")
            verdict = classify_ship_status(repo, "DAN-65", [sha], ref="main")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "ancestor")

    def test_squash_merge_is_shipped_via_content_grep(self) -> None:
        # DAN-81 shape, and DAN-95/DAN-96's own recovery shape: the source
        # branch commit is never an ancestor of main (squash produces a new
        # commit), but the issue id shows up in the squashed commit message
        # that IS on main.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/dante/DAN-81-fix")
            source_sha = _commit(repo, "fix(DAN-81): strip namespace tag", "g.txt", "g")
            _git(repo, "checkout", "-q", "main")
            _commit(
                repo,
                "fix(DAN-81): strip both halves of a namespace:tag line (#30)",
                "g.txt",
                "g",
            )

            # The trap: ancestry alone says this was never shipped.
            self.assertFalse(is_ancestor(repo, source_sha, "main"))
            self.assertFalse(_naive_ancestry_only_verdict(repo, [source_sha], "main"))

            # The fix: content landed under a different commit, so this is
            # NOT an orphan.
            verdict = classify_ship_status(repo, "DAN-81", [source_sha], ref="main")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "content_grep")

    def test_genuinely_unshipped_commit_is_orphaned(self) -> None:
        # DAN-95/DAN-96 shape BEFORE recovery: commit exists only on an
        # unmerged branch, and nothing on main mentions the issue at all.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/virgil/DAN-95-fix")
            source_sha = _commit(
                repo, "fix(DAN-95): surface hydrus tag lookup failures", "h.txt", "h"
            )
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "unrelated: something else entirely", "i.txt", "i")

            verdict = classify_ship_status(repo, "DAN-95", [source_sha], ref="main")
            self.assertFalse(verdict.shipped)
            self.assertEqual(verdict.reason, "orphaned")

    def test_pr_state_lookup_confirms_shipped_when_grep_would_miss(self) -> None:
        # Merged-PR state from the GitHub API is checked before falling back
        # to grep, covering a squash whose final commit message happens not
        # to mention the issue id at all (e.g. a generic PR title).
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/dante/DAN-50-fix")
            source_sha = _commit(repo, "DAN-50: fix the thing", "j.txt", "j")
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "Merge pull request #50", "j.txt", "j2")

            verdict = classify_ship_status(
                repo,
                "DAN-50",
                [source_sha],
                ref="main",
                pr_state_lookup=lambda: "MERGED",
            )
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "pr_merged")

    def test_pr_state_lookup_open_falls_through_to_grep_miss_and_stays_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/dante/DAN-51-fix")
            source_sha = _commit(repo, "DAN-51: fix the thing", "k.txt", "k")
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "unrelated commit", "l.txt", "l")

            verdict = classify_ship_status(
                repo,
                "DAN-51",
                [source_sha],
                ref="main",
                pr_state_lookup=lambda: "OPEN",
            )
            self.assertFalse(verdict.shipped)
            self.assertEqual(verdict.reason, "orphaned")


class DecoyShaDoesNotMaskAnOrphanTests(unittest.TestCase):
    """The masking case this guard's own live dry run caught: an issue's
    comment cites a real, unrelated commit for context (a base-ref or
    "current main is now X" note), which is trivially an ancestor of every
    later main. Treating that citation as the issue's own fix would let a
    genuinely unshipped issue read as "shipped" - the DAN-95/DAN-96 failure
    mode, recurring one layer down in the guard meant to catch it.
    """

    def test_decoy_base_ref_sha_does_not_falsely_confirm_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            # A commit that exists and IS an ancestor of main, but has
            # nothing to do with DAN-200 - the kind of SHA an unrelated
            # status comment cites as "cut fresh from origin/main (<sha>)".
            decoy_sha = _commit(repo, "unrelated: bump vendored fixture", "z.txt", "z")

            # DAN-200's own fix commit lives only on an unmerged branch.
            _git(repo, "checkout", "-q", "-b", "agent/x/DAN-200-fix")
            real_fix_sha = _commit(repo, "fix(DAN-200): the actual change", "y.txt", "y")
            _git(repo, "checkout", "-q", "main")

            verdict = classify_ship_status(
                repo, "DAN-200", [decoy_sha, real_fix_sha], ref="main"
            )
            self.assertFalse(
                verdict.shipped,
                "a decoy SHA mentioned in passing must not mask a genuine orphan",
            )
            self.assertEqual(verdict.reason, "orphaned")

    def test_relevant_shas_filters_out_unrelated_commit(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            decoy_sha = _commit(repo, "unrelated: bump vendored fixture", "z2.txt", "z2")
            own_sha = _commit(repo, "fix(DAN-201): the actual change", "y2.txt", "y2")
            from ops.ancestry_guard import relevant_shas

            self.assertEqual(
                relevant_shas(repo, "DAN-201", [decoy_sha, own_sha]), [own_sha]
            )


class DecoyCommitBodyDoesNotMaskAnOrphanTests(unittest.TestCase):
    """The same masking failure as `DecoyShaDoesNotMaskAnOrphanTests`, one
    layer further down: `git log --grep` matches the full commit message,
    body included. A later, unrelated commit can narrate a past issue in
    its body ("DAN-113's commit explains that DAN-95/DAN-96 needed a guard
    without DAN-113 being either issue's fix") without that issue's fix
    ever landing anywhere. Caught live against this repo's real history on
    2026-09-30: `content_grep_hits` picked DAN-113's commit as "evidence"
    for DAN-95/DAN-96 over the real fix commit, purely because it was the
    more recent grep hit - the DAN-95/DAN-96 failure mode, recurring inside
    the guard meant to catch it.
    """

    def test_incidental_body_mention_does_not_confirm_shipped(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            # DAN-999's own fix is never committed anywhere. A later,
            # unrelated commit's *body* mentions it only in passing while
            # describing why a guard was added - same shape as DAN-113's
            # real commit body narrating DAN-95/DAN-96.
            _commit(
                repo,
                "feat(DAN-1000): unrelated guard\n\n"
                "Earlier, DAN-999 was supposed to fix the thing but never "
                "landed, so add a guard instead.",
                "guard.txt",
                "guard",
            )

            verdict = classify_ship_status(repo, "DAN-999", [], ref="main")
            self.assertFalse(
                verdict.shipped,
                "an incidental body mention in an unrelated commit must not "
                "be read as proof the issue shipped",
            )
            self.assertEqual(verdict.reason, "orphaned")

    def test_grep_prefers_the_commit_whose_own_subject_names_the_issue(self) -> None:
        # Real DAN-95/DAN-96 shape: a genuine fix commit (subject names the
        # issue) coexists with a later, unrelated commit whose *body* also
        # mentions the issue in passing. The evidence cited must be the real
        # fix, not whichever commit happens to be more recent.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _commit(
                repo,
                "fix(DAN-95,DAN-96): surface Hydrus and booru tag-fetch failures",
                "fix.txt",
                "fix",
            )
            _commit(
                repo,
                "feat(DAN-113): add mandatory pre-flight guard\n\n"
                "DAN-95/DAN-96 landed directly in the shared checkout and "
                "were never pushed.",
                "guard.txt",
                "guard",
            )

            verdict = classify_ship_status(repo, "DAN-95", [], ref="main")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "content_grep")
            self.assertTrue(
                any("fix(DAN-95,DAN-96)" in line for line in verdict.evidence),
                f"evidence must cite the real fix commit, not the decoy: {verdict.evidence}",
            )


class Dan95Dan96EndToEndReconstructionTests(unittest.TestCase):
    """The one end-to-end proof the issue asks for: flags DAN-95/DAN-96 as
    orphaned against the pre-recovery ref, and stops flagging them against
    the post-recovery ref - reconstructed with synthetic SHAs standing in
    for the real f021b55 (DAN-95) and 32cd052 (DAN-96), since pinning the
    real, mutable agent branches in a unit test would make this test's
    survival depend on nobody ever pruning them.
    """

    def test_orphaned_before_recovery_shipped_after(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)

            _git(repo, "checkout", "-q", "-b", "agent/virgil/DAN-95-hydrus-tag-lookup-silent-failure")
            dan95_sha = _commit(
                repo, "fix(DAN-95): surface hydrus tag lookup failures", "hydrus.txt", "1"
            )
            dan96_sha = _commit(
                repo, "fix(DAN-96): surface booru tag fetch failures", "booru.txt", "1"
            )

            _git(repo, "checkout", "-q", "main")
            _commit(repo, "unrelated work while the branch sat unpushed", "other.txt", "1")
            pre_recovery_tip = _git(repo, "rev-parse", "HEAD")

            # Before DAN-112 lands: neither fix is an ancestor of main, and
            # main's log says nothing about either issue yet.
            for identifier, sha in (("DAN-95", dan95_sha), ("DAN-96", dan96_sha)):
                verdict = classify_ship_status(repo, identifier, [sha], ref=pre_recovery_tip)
                self.assertFalse(
                    verdict.shipped, f"{identifier} should be orphaned before recovery"
                )
                self.assertEqual(verdict.reason, "orphaned")

            # DAN-112 recovers both onto a fresh branch off main and lands
            # them as one squash-merged PR - same shape as the real PR #37.
            _commit(
                repo,
                "fix(DAN-95,DAN-96): surface Hydrus and booru tag-fetch failures (#37)",
                "hydrus.txt",
                "2",
            )
            post_recovery_tip = _git(repo, "rev-parse", "HEAD")

            # The original branch commits are still non-ancestors - a
            # squash merge never changes that - so this is the exact trap
            # the guard exists to not fall into.
            self.assertFalse(is_ancestor(repo, dan95_sha, post_recovery_tip))
            self.assertFalse(is_ancestor(repo, dan96_sha, post_recovery_tip))

            # After DAN-112 lands: both are shipped via content grep.
            for identifier, sha in (("DAN-95", dan95_sha), ("DAN-96", dan96_sha)):
                verdict = classify_ship_status(repo, identifier, [sha], ref=post_recovery_tip)
                self.assertTrue(
                    verdict.shipped, f"{identifier} should stop being flagged after recovery"
                )
                self.assertEqual(verdict.reason, "content_grep")


def _fake_gh(tmpdir: str, *, stdout: str = "", returncode: int = 0) -> str:
    """Write a stub `gh` executable that records its argv and prints `stdout`.

    The real `gh` in this environment is a broker wrapper that re-asserts
    its own PATH entry on every invocation (see DAN-213), so PATH-shadowing
    a fake `gh` does not work. `gh_pr_state`/`github_pr_state_lookup_factory`
    take an explicit `gh_bin` override for exactly this reason - these tests
    use that override rather than touching PATH.
    """
    path = Path(tmpdir) / "fake-gh"
    path.write_text(
        "#!/bin/sh\n"
        f"echo '{stdout}'\n"
        f"exit {returncode}\n"
    )
    path.chmod(0o755)
    return str(path)


class GhPrStateTests(unittest.TestCase):
    def test_returns_state_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh(tmp, stdout='{"state": "MERGED"}')
            self.assertEqual(
                gh_pr_state("Dromares/Hatate-Linux-Redux", 77, gh_bin=gh_bin), "MERGED"
            )

    def test_returns_none_on_nonzero_exit(self) -> None:
        # A plumbing failure (gh missing, PR not found, auth error) must
        # read as "no evidence", never as a confident "not merged".
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh(tmp, stdout="not found", returncode=1)
            self.assertIsNone(gh_pr_state("Dromares/Hatate-Linux-Redux", 404, gh_bin=gh_bin))

    def test_returns_none_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh(tmp, stdout="not json")
            self.assertIsNone(gh_pr_state("Dromares/Hatate-Linux-Redux", 1, gh_bin=gh_bin))

    def test_invokes_per_pr_view_not_a_bulk_list(self) -> None:
        # DAN-266: the bulk `gh pr list` / "list pull requests" endpoint was
        # observed serving a stale `merged` field hours after the real
        # merge. Pin the subprocess call shape so a future edit can't drift
        # back onto the bulk endpoint silently.
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout='{"state": "MERGED"}', stderr=""
            )
            gh_pr_state("Dromares/Hatate-Linux-Redux", 77, gh_bin="gh")
            (call_args,), _ = run.call_args
            self.assertEqual(
                call_args,
                ["gh", "pr", "view", "77", "--repo", "Dromares/Hatate-Linux-Redux", "--json", "state"],
            )


class GithubPrStateLookupFactoryTests(unittest.TestCase):
    def test_factory_builds_a_zero_arg_lookup_for_the_given_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh(tmp, stdout='{"state": "MERGED"}')
            factory = github_pr_state_lookup_factory("Dromares/Hatate-Linux-Redux", gh_bin=gh_bin)
            lookup = factory(77)
            self.assertEqual(lookup(), "MERGED")

    def test_wired_into_classify_ship_status_confirms_shipped(self) -> None:
        # End-to-end: a squash whose own commit subject never mentions the
        # issue (content_grep would miss it) is still confirmed shipped via
        # the per-PR state lookup, exactly the DAN-266 gap this closes.
        with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as tmp:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/x/DAN-300-fix")
            source_sha = _commit(repo, "fix(DAN-300): the actual change", "pr300.txt", "p")
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "Merge pull request #300 from agent/x/DAN-300-fix", "pr300.txt", "p2")

            gh_bin = _fake_gh(tmp, stdout='{"state": "MERGED"}')
            factory = github_pr_state_lookup_factory("Dromares/Hatate-Linux-Redux", gh_bin=gh_bin)

            verdict = classify_ship_status(
                repo, "DAN-300", [source_sha], ref="main", pr_state_lookup=factory(300)
            )
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "pr_merged")


class ContentGrepHitsTests(unittest.TestCase):
    def test_hits_and_no_hits(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _commit(repo, "fix(DAN-7): something", "m.txt", "m")
            self.assertTrue(content_grep_hits(repo, "DAN-7", ref="main"))
            self.assertEqual(content_grep_hits(repo, "DAN-404", ref="main"), [])


class ExtractTicketIdsTests(unittest.TestCase):
    def test_extracts_ticket_from_branch_name(self) -> None:
        self.assertEqual(
            extract_ticket_ids("agent/virgil/DAN-213-cont"), ["DAN-213"]
        )

    def test_dedupes_and_preserves_first_appearance_order(self) -> None:
        self.assertEqual(
            extract_ticket_ids("DAN-2 mentioned after DAN-1, then DAN-1 again"),
            ["DAN-2", "DAN-1"],
        )

    def test_no_hyphen_between_letters_and_digits_does_not_match(self) -> None:
        # virgil-dan213-local (DAN-272): the hyphen sits between words, not
        # between the letter run and the digit run, so this must not be
        # mistaken for a ticket id - the branch genuinely carries no
        # ticket-shaped identifier to check.
        self.assertEqual(extract_ticket_ids("virgil-dan213-local"), [])


def _fake_gh_pr_list(tmpdir: str, *, stdout: str = "[]", returncode: int = 0) -> str:
    return _fake_gh(tmpdir, stdout=stdout, returncode=returncode)


class GhPrNumbersForBranchTests(unittest.TestCase):
    def test_returns_numbers_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh_pr_list(tmp, stdout='[{"number": 42}, {"number": 7}]')
            self.assertEqual(
                gh_pr_numbers_for_branch("Dromares/Hatate-Linux-Redux", "some-branch", gh_bin=gh_bin),
                [42, 7],
            )

    def test_returns_empty_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh_pr_list(tmp, stdout="error", returncode=1)
            self.assertEqual(
                gh_pr_numbers_for_branch("Dromares/Hatate-Linux-Redux", "some-branch", gh_bin=gh_bin),
                [],
            )

    def test_returns_empty_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gh_bin = _fake_gh_pr_list(tmp, stdout="not json")
            self.assertEqual(
                gh_pr_numbers_for_branch("Dromares/Hatate-Linux-Redux", "some-branch", gh_bin=gh_bin),
                [],
            )

    def test_returns_empty_when_gh_binary_is_missing(self) -> None:
        # A missing `gh` must read as "no evidence" (fail closed, don't
        # reclaim), never crash the caller - scripts/agent-preflight.sh
        # calls this on every heartbeat's pre-flight check.
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "no-such-gh-binary")
            self.assertEqual(
                gh_pr_numbers_for_branch("Dromares/Hatate-Linux-Redux", "some-branch", gh_bin=missing),
                [],
            )

    def test_invokes_head_based_list_not_a_merged_state_filter(self) -> None:
        # DAN-266: a `--state merged` filter relies on the same bulk-list
        # fields observed serving a stale `merged` state. This only uses
        # the bulk list to discover PR numbers for the exact head branch;
        # `gh_pr_state`'s per-PR lookup is what decides merged state.
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="[]", stderr=""
            )
            gh_pr_numbers_for_branch("Dromares/Hatate-Linux-Redux", "agent/x/DAN-9", gh_bin="gh")
            (call_args,), _ = run.call_args
            self.assertEqual(
                call_args,
                [
                    "gh", "pr", "list", "--repo", "Dromares/Hatate-Linux-Redux",
                    "--head", "agent/x/DAN-9", "--state", "all", "--json", "number",
                    "-L", "10",
                ],
            )


class BranchShipVerdictTests(unittest.TestCase):
    """DAN-272: can scripts/agent-preflight.sh safely reclaim a local branch
    `git cherry` cannot clear? All cases here mock the two `gh` calls
    directly rather than spinning up a fake binary, since the behavior under
    test is `branch_ship_verdict`'s own decision logic, not subprocess
    plumbing (already covered by `GhPrNumbersForBranchTests`/`GhPrStateTests`).
    """

    def test_no_pr_on_github_is_not_shipped(self) -> None:
        with mock.patch("ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[]):
            verdict = branch_ship_verdict("unused-repo-path", "agent/x/DAN-9")
            self.assertFalse(verdict.shipped)
            self.assertEqual(verdict.reason, "no_signal")

    def test_merged_pr_is_shipped(self) -> None:
        with mock.patch(
            "ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[74]
        ), mock.patch("ops.ancestry_guard.gh_pr_state", return_value="MERGED"):
            verdict = branch_ship_verdict("unused-repo-path", "agent/beatrice/DAN-217-is-ancestor-squash-fix")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "pr_merged")

    def test_open_pr_is_not_shipped(self) -> None:
        with mock.patch(
            "ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[76]
        ), mock.patch("ops.ancestry_guard.gh_pr_state", return_value="OPEN"):
            verdict = branch_ship_verdict("unused-repo-path", "agent/virgil/DAN-213-merge-wrapper")
            self.assertFalse(verdict.shipped)
            self.assertEqual(verdict.reason, "orphaned")

    def test_any_merged_pr_among_several_is_shipped(self) -> None:
        with mock.patch(
            "ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[10, 11]
        ), mock.patch(
            "ops.ancestry_guard.gh_pr_state", side_effect=["CLOSED", "MERGED"]
        ):
            verdict = branch_ship_verdict("unused-repo-path", "some-branch")
            self.assertTrue(verdict.shipped)
            self.assertEqual(verdict.reason, "pr_merged")

    def test_ticket_id_content_grep_alone_would_have_been_a_false_positive(self) -> None:
        # The exact DAN-272 trap: agent/virgil/DAN-213-cont carries a SECOND,
        # still-unmerged DAN-213 fix, but DAN-213's FIRST fix already shipped
        # separately - so content_grep_hits(repo, "DAN-213", ref) finds a
        # real hit. If branch_ship_verdict used that signal (like
        # classify_ship_status does for issues), it would wrongly call this
        # branch shipped and let the guard delete genuinely unmerged work.
        # Asserting against the real content-grep signal here, not a mock,
        # is the point: this proves the trap is real, not just asserted away.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _commit(repo, "feat(DAN-213): staleness gate for the merge path (#73)", "a.txt", "a")
            self.assertTrue(
                content_grep_hits(repo, "DAN-213", ref="main"),
                "test setup sanity check: ticket-id content grep must find the unrelated, already-shipped fix",
            )

            with mock.patch("ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[]):
                verdict = branch_ship_verdict(repo, "agent/virgil/DAN-213-cont")
            self.assertFalse(
                verdict.shipped,
                "a branch with no PR of its own must not be shipped just because its "
                "ticket id matches an unrelated, separately-shipped fix",
            )

    def test_cli_branch_shipped_exit_code_and_json(self) -> None:
        from io import StringIO

        from ops.ancestry_guard import main

        with mock.patch(
            "ops.ancestry_guard.gh_pr_numbers_for_branch", return_value=[74]
        ), mock.patch("ops.ancestry_guard.gh_pr_state", return_value="MERGED"):
            out = StringIO()
            with mock.patch("sys.stdout", out):
                exit_code = main(["branch-shipped", "unused-repo-path", "agent/x/DAN-9"])
            self.assertEqual(exit_code, 0)
            self.assertIn('"shipped": true', out.getvalue())


if __name__ == "__main__":
    unittest.main()
