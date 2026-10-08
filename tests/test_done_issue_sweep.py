"""DAN-116: sweep orchestration around the ancestry-guard classifier.

Uses a fake Paperclip client (no network, no real company data) to verify
the sweep's actual decisions: which issues get skipped as not-checkable,
which get reopened, and that dry-run never mutates anything even when it
finds an orphan.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from ops.done_issue_sweep import PaperclipClient, build_target, evaluate_target, run_sweep


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
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


class FakePaperclipClient(PaperclipClient):
    """Same interface as PaperclipClient, backed by in-memory fixtures.

    Never touches urllib - deliberately does not call `super().__init__`,
    so a bug that accidentally left a real network call in a method here
    would be a loud AttributeError/TypeError, not a silent hang.
    """

    def __init__(self, issues: list[dict[str, Any]], comments: dict[str, list[dict[str, Any]]]) -> None:
        self._issues = issues
        self._comments = comments
        self.reopened: list[tuple[str, str]] = []

    def list_done_issues(self, company_id: str, project_id: str) -> list[dict[str, Any]]:
        return list(self._issues)

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        return list(self._comments.get(issue_id, []))

    def reopen_as_blocked(self, issue_id: str, comment: str) -> Any:
        self.reopened.append((issue_id, comment))
        return {"id": issue_id, "status": "blocked"}


class BuildTargetTests(unittest.TestCase):
    def test_concatenates_title_description_and_comments(self) -> None:
        issue = {"id": "i1", "identifier": "DAN-1", "title": "T", "description": "D"}
        comments = [{"body": "c1"}, {"body": None}, {"body": "c2"}]
        target = build_target(issue, comments)
        self.assertEqual(target.identifier, "DAN-1")
        self.assertIn("T", target.text)
        self.assertIn("D", target.text)
        self.assertIn("c1", target.text)
        self.assertIn("c2", target.text)


class EvaluateTargetTests(unittest.TestCase):
    def test_returns_none_when_nothing_to_check(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            from ops.done_issue_sweep import SweepTarget

            target = SweepTarget(issue_id="i1", identifier="DAN-1", text="no refs here at all")
            self.assertIsNone(evaluate_target(repo, target, ref="main"))

    def test_returns_none_when_only_decoy_shas_are_present(self) -> None:
        # DAN-65 shape from the guard's own live dry run: the text mentions
        # a real, resolvable commit (a713036-style "as of this commit"
        # status note) that has nothing to do with this issue. Gating on
        # relevance, not mere resolvability, is what keeps this a skip
        # instead of a false verdict either way.
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            decoy_sha = _commit(repo, "test(DAN-58): fix an unrelated test", "decoy.txt", "d")
            from ops.done_issue_sweep import SweepTarget

            target = SweepTarget(
                issue_id="i65", identifier="DAN-65", text=f"verified as of {decoy_sha}"
            )
            self.assertIsNone(evaluate_target(repo, target, ref="main"))

    def test_evaluates_shipped_commit(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            sha = _commit(repo, "DAN-2: ship it", "a.txt", "a")
            from ops.done_issue_sweep import SweepTarget

            target = SweepTarget(issue_id="i2", identifier="DAN-2", text=f"landed as {sha}")
            verdict = evaluate_target(repo, target, ref="main")
            assert verdict is not None
            self.assertTrue(verdict.shipped)


class RunSweepTests(unittest.TestCase):
    def test_skips_issue_with_no_reference(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            client = FakePaperclipClient(
                issues=[{"id": "i1", "identifier": "DAN-1", "title": "no code here", "description": ""}],
                comments={},
            )
            report = run_sweep(client, "co", "proj", repo, ref="main", apply=True)
            self.assertEqual(report, [])
            self.assertEqual(client.reopened, [])

    def test_dry_run_reports_orphan_but_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)
            _git(repo, "checkout", "-q", "-b", "agent/x/DAN-9-fix")
            sha = _commit(repo, "fix(DAN-9): never merged", "b.txt", "b")
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "unrelated", "c.txt", "c")

            client = FakePaperclipClient(
                issues=[
                    {
                        "id": "i9",
                        "identifier": "DAN-9",
                        "title": "fix the thing",
                        "description": f"landed as {sha}",
                    }
                ],
                comments={},
            )
            report = run_sweep(client, "co", "proj", repo, ref="main", apply=False)
            self.assertEqual(len(report), 1)
            self.assertFalse(report[0]["shipped"])
            self.assertEqual(report[0]["identifier"], "DAN-9")
            self.assertEqual(client.reopened, [], "dry-run must never mutate the issue")

    def test_apply_reopens_only_the_orphan_not_the_shipped_one(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            _init_repo(repo)

            shipped_sha = _commit(repo, "DAN-10: ship it", "d.txt", "d")

            _git(repo, "checkout", "-q", "-b", "agent/x/DAN-11-fix")
            orphan_sha = _commit(repo, "fix(DAN-11): never merged", "e.txt", "e")
            _git(repo, "checkout", "-q", "main")
            _commit(repo, "unrelated", "f.txt", "f")

            client = FakePaperclipClient(
                issues=[
                    {
                        "id": "i10",
                        "identifier": "DAN-10",
                        "title": "shipped",
                        "description": f"landed as {shipped_sha}",
                    },
                    {
                        "id": "i11",
                        "identifier": "DAN-11",
                        "title": "orphan",
                        "description": f"landed as {orphan_sha}",
                    },
                ],
                comments={},
            )
            report = run_sweep(client, "co", "proj", repo, ref="main", apply=True)

            by_id = {row["identifier"]: row for row in report}
            self.assertTrue(by_id["DAN-10"]["shipped"])
            self.assertFalse(by_id["DAN-11"]["shipped"])

            self.assertEqual(len(client.reopened), 1)
            reopened_issue_id, comment = client.reopened[0]
            self.assertEqual(reopened_issue_id, "i11")
            self.assertIn(orphan_sha, comment)


if __name__ == "__main__":
    unittest.main()
