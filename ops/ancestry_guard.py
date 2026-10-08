"""Content-aware ship-status classifier for the done/origin-main ancestry guard.

DAN-116: a `done` issue whose fix never shipped must reopen itself. DAN-95 and
DAN-96 were written, closed, and stayed live in the product because nothing
ever checked that their commits actually reached `origin/main`.

The obvious check - ``git merge-base --is-ancestor <sha> origin/main`` - is
not enough on its own. A squash-merged PR leaves its source commit a
permanent non-ancestor of `main`: the squash produces a brand new commit with
different parents, so every squash-merge in the repo's history reads as an
orphan under ancestry alone. DAN-114 hit exactly this on DAN-81 (ancestry
said NO, the file diff against the squashed commit was empty) and DAN-112
confirmed it again on DAN-95/DAN-96 themselves (recovered via a fresh
cherry-picked branch, then squash-merged as PR #37 - the original commits
are still non-ancestors of `origin/main` today, on purpose).

So a negative ancestry result is a *lead*, not a verdict. It only becomes an
"orphaned" verdict once two independent content checks also come back empty:
a merged-PR state from the GitHub API (if available) and the issue
identifier appearing in a commit message actually on the target ref.

DAN-272 reuses the per-PR GitHub merged-state signal for a second purpose:
deciding whether a *local branch* (as opposed to a closed issue) is safe for
scripts/agent-preflight.sh to reclaim, via `branch_ship_verdict` below.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

_SHA_CANDIDATE_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
_PR_NUMBER_RE = re.compile(r"(?:\bPR\s*#|\bpull/|\(#)(\d+)\b", re.IGNORECASE)
_TICKET_ID_RE = re.compile(r"\b([A-Za-z]{2,10}-[0-9]+)\b")


class GitError(RuntimeError):
    """A git subprocess call failed in a way that isn't a normal miss."""


def _run_git(repo: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
    )


def is_ancestor(repo: str, sha: str, ref: str = "origin/main") -> bool:
    """True if `sha` is an ancestor of `ref`, False if it definitely is not.

    Raises GitError for anything else (bad SHA, unknown ref, ...) so a
    plumbing failure is never silently read as "not shipped".
    """
    result = _run_git(repo, ["merge-base", "--is-ancestor", sha, ref])
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitError(
        f"git merge-base --is-ancestor {sha} {ref} failed unexpectedly "
        f"(exit {result.returncode}): {result.stderr.strip()}"
    )


def content_grep_hits(repo: str, pattern: str, ref: str = "origin/main") -> list[str]:
    """Commits on `ref` whose own *subject* mentions `pattern` (e.g. an issue id).

    `git log --grep` matches the full commit message, subject and body alike.
    A later, unrelated commit can narrate a past issue in its body ("DAN-999
    was supposed to fix this but never landed, so add a guard instead")
    without that issue's fix ever having landed anywhere. Matching only the
    subject keeps that kind of incidental mention from being read as proof
    the issue shipped - the same reasoning `relevant_shas` already applies
    to commit SHAs, applied here to the commit-message content check too.
    """
    result = _run_git(
        repo,
        ["log", ref, "--format=%h %s", "-i", f"--grep={pattern}"],
    )
    if result.returncode != 0:
        raise GitError(
            f"git log {ref} --grep={pattern} failed: {result.stderr.strip()}"
        )
    hits = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        _, _, subject = line.partition(" ")
        if pattern.lower() in subject.lower():
            hits.append(line)
    return hits


def commit_exists(repo: str, sha: str) -> bool:
    """True if `sha` resolves to a real commit object in `repo`."""
    result = _run_git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return result.returncode == 0


def commit_subject(repo: str, sha: str) -> str:
    """The one-line commit subject for `sha`."""
    result = _run_git(repo, ["log", "-1", "--format=%s", sha])
    if result.returncode != 0:
        raise GitError(f"git log -1 --format=%s {sha} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def relevant_shas(repo: str, issue_identifier: str, shas: Sequence[str]) -> list[str]:
    """`shas` whose own commit message actually claims `issue_identifier`.

    An issue's comment thread often cites a real, resolvable commit for a
    reason that has nothing to do with that issue's own fix - "cut fresh
    from origin/main (`43ae8fc`)", "Base read: origin/main = `cd62832`".
    Because any such base-ref SHA is, by construction, already an ancestor
    of every later `origin/main`, treating it as a candidate fix commit
    would let it satisfy the ancestor check by coincidence and silently
    mask a genuinely unshipped issue - the exact failure this guard exists
    to catch, just moved one level down. A decoy commit's own subject line
    was never going to mention this issue; every real fix commit in this
    repo's history does, by convention (``fix(DAN-nn): ...`` / ``DAN-nn:
    ...``).
    """
    return [s for s in shas if issue_identifier.lower() in commit_subject(repo, s).lower()]


def extract_shas(repo: str, text: str) -> list[str]:
    """Hex-looking tokens in `text` that resolve to a real commit in `repo`.

    Free-form issue/comment text contains plenty of 7+ character lowercase
    hex look-alikes ("cafe", "deadbeef", "facade"). Validating each candidate
    against the actual object store, rather than trusting the regex alone,
    is what keeps those out of the result.
    """
    candidates = set(_SHA_CANDIDATE_RE.findall(text or ""))
    return sorted(c for c in candidates if commit_exists(repo, c))


def extract_pr_numbers(text: str) -> list[int]:
    """PR numbers referenced in `text` (``PR #37``, ``pull/37``, ``(#37)``)."""
    return sorted({int(m.group(1)) for m in _PR_NUMBER_RE.finditer(text or "")})


def gh_pr_state(repo_slug: str, pr_number: int, *, gh_bin: str = "gh") -> Optional[str]:
    """The PR's `state` (``OPEN``/``CLOSED``/``MERGED``) via a single-PR lookup.

    Deliberately per-PR (``gh pr view``), never the bulk `gh pr list` /
    GitHub "list pull requests" endpoint: DAN-266 observed that bulk
    endpoint serving a stale `merged` field up to ~4 hours after a PR had
    actually merged, while the per-PR lookup matched the real merge-commit
    timestamp exactly. Returns None on any failure (gh missing, PR not
    found, malformed output) so a plumbing failure reads as "no evidence",
    never as "not merged".
    """
    result = subprocess.run(
        [gh_bin, "pr", "view", str(pr_number), "--repo", repo_slug, "--json", "state"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        state = json.loads(result.stdout).get("state")
    except json.JSONDecodeError:
        return None
    return state if isinstance(state, str) else None


def github_pr_state_lookup_factory(
    repo_slug: str, *, gh_bin: str = "gh"
) -> Callable[[int], Callable[[], Optional[str]]]:
    """Build the `pr_state_lookup_factory` callable `evaluate_target` expects.

    Each call is a fresh per-PR `gh pr view` - see `gh_pr_state` for why that
    matters instead of fetching a list once and indexing into it.
    """

    def factory(pr_number: int) -> Callable[[], Optional[str]]:
        return lambda: gh_pr_state(repo_slug, pr_number, gh_bin=gh_bin)

    return factory


@dataclass
class ShipVerdict:
    shipped: bool
    reason: str
    evidence: list[str] = field(default_factory=list)


def classify_ship_status(
    repo: str,
    issue_identifier: str,
    shas: Sequence[str],
    ref: str = "origin/main",
    pr_state_lookup: Optional[Callable[[], Optional[str]]] = None,
) -> ShipVerdict:
    """Decide whether a `done` issue's fix actually reached `ref`.

    Ancestry is checked first: it is the strongest, cheapest signal, and a
    positive result ends the check immediately. A negative result is not
    proof of anything by itself - see the module docstring - so it falls
    through to two content checks, either of which is sufficient to confirm
    the change landed under a different commit:

      1. a merged-PR state from the GitHub API, via `pr_state_lookup`
      2. the issue identifier appearing in a commit message already on `ref`

    Only when ancestry is negative for every known SHA *and* both content
    checks come back empty is the verdict "orphaned".

    `shas` is filtered through `relevant_shas` first, keeping only the ones
    whose own commit message actually claims `issue_identifier` - see that
    function's docstring for why. A SHA that fails this filter is dropped
    with the same evidence trail as having no SHA at all, rather than being
    allowed to short-circuit the ancestor check by coincidence.

    `shas` may be empty (after filtering or to start with) even when the
    caller has something worth checking - an issue can carry a PR reference
    with no extractable commit SHA (DAN-40, DAN-66, DAN-68 all did: the text
    names a PR number but never a literal hex SHA). An empty list skips
    straight to the PR/grep checks rather than giving up, since bailing out
    there produced exactly this guard's own false positives on its first
    live dry run.
    """
    evidence: list[str] = []
    own_shas = relevant_shas(repo, issue_identifier, shas)
    if shas and not own_shas:
        evidence.append(
            f"none of the referenced SHA(s) {list(shas)} look like {issue_identifier}'s "
            "own commit (their own commit subject doesn't mention it) - treated as "
            "unrelated citations, not a fix"
        )
    elif not shas:
        evidence.append("no commit SHA known for this issue (PR reference only, or unresolved)")

    for sha in own_shas:
        if is_ancestor(repo, sha, ref):
            evidence.append(f"{sha} is an ancestor of {ref}")
            return ShipVerdict(shipped=True, reason="ancestor", evidence=evidence)
        evidence.append(f"{sha} is NOT an ancestor of {ref}")

    if pr_state_lookup is not None:
        state = pr_state_lookup()
        evidence.append(f"PR state lookup returned: {state!r}")
        if state == "MERGED":
            return ShipVerdict(shipped=True, reason="pr_merged", evidence=evidence)

    hits = content_grep_hits(repo, issue_identifier, ref=ref)
    if hits:
        evidence.append(f"{issue_identifier} found in {ref} commit log: {hits[0]}")
        return ShipVerdict(shipped=True, reason="content_grep", evidence=evidence)
    evidence.append(f"no commit on {ref} mentions {issue_identifier}")

    return ShipVerdict(shipped=False, reason="orphaned", evidence=evidence)


def extract_ticket_ids(text: str) -> list[str]:
    """Issue identifiers (``DAN-213``, ``PAP-7``, ...) found in free text.

    Order is preserved (first appearance first, duplicates dropped) rather
    than sorted. Not currently used by `branch_ship_verdict` (see that
    function's docstring for why a ticket id alone is the wrong signal for a
    *branch*) but kept as a building block: it is still the right first step
    for anything that, unlike a branch, really does have one fix per ticket.
    """
    seen: dict[str, None] = {}
    for m in _TICKET_ID_RE.finditer(text or ""):
        seen.setdefault(m.group(1), None)
    return list(seen)


def gh_pr_numbers_for_branch(
    repo_slug: str, branch: str, *, gh_bin: str = "gh", limit: int = 10
) -> list[int]:
    """PR numbers on GitHub whose head branch is exactly `branch`, any state.

    Deliberately only a name-based lookup, not a merged-state filter: per
    `gh_pr_state`'s docstring (DAN-266), the bulk `gh pr list` endpoint's
    `state`/`merged` fields can lag the real merge by hours. This function
    only uses the bulk list to discover *which* PR numbers exist for this
    exact branch name; `branch_ship_verdict` below still does the per-PR
    `gh_pr_state` lookup to decide merged state. Returns `[]` on any failure
    (gh missing, auth error, malformed output, no network) so a plumbing
    failure reads as "no evidence", never as "not merged" or "merged".
    """
    try:
        result = subprocess.run(
            [
                gh_bin, "pr", "list", "--repo", repo_slug, "--head", branch,
                "--state", "all", "--json", "number", "-L", str(limit),
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item["number"] for item in data if isinstance(item, dict) and isinstance(item.get("number"), int)]


def branch_ship_verdict(
    repo: str,
    branch: str,
    *,
    repo_slug: str = "Dromares/Hatate-Linux-Redux",
    gh_bin: str = "gh",
) -> ShipVerdict:
    """Decide whether a local `branch`'s own content already shipped via a merged PR.

    DAN-272: `git cherry <ref> <branch>` (used by scripts/agent-preflight.sh
    to reclaim provably-redundant local branches) compares each of the
    branch's commits' patch-ids against `ref` one at a time. That misses the
    exact squash-merge trap this module already exists to handle: when a
    multi-commit branch is squashed into a single PR merge commit, the
    merged commit's diff is the *sum* of the branch's commits, so no
    individual commit's patch-id matches it and `git cherry` reports every
    commit as unmerged ('+') even though none of it is actually missing from
    `ref`. Confirmed live against this repo's own history: both
    `agent/beatrice/DAN-193-rule34us-hydrus-checks` and
    `agent/beatrice/DAN-217-is-ancestor-squash-fix` are fully squash-merged
    (PRs #69 and #74) yet `git cherry` cannot clear either one.

    This is deliberately **not** `content_grep_hits` keyed off a ticket id
    parsed from the branch name, unlike `classify_ship_status`. A ticket can
    get more than one real, separately-shipped-or-not round of work -
    confirmed live on this same repo's `agent/virgil/DAN-213-cont`: its own
    unique commit is a *second* DAN-213 fix still sitting in an open,
    unmerged PR (#76), but `content_grep_hits(repo, "DAN-213", ref)` still
    finds a hit, because DAN-213's *first* fix merged separately (PR #73).
    Ticket-id content grep would have reported this genuinely unshipped
    branch as shipped and let the guard delete it. A branch's identity is
    its exact head ref name, not the ticket id embedded in it, so the only
    signal checked here is GitHub's own per-PR merged state for a PR whose
    head branch is this exact name (`gh_pr_numbers_for_branch` +
    `gh_pr_state`, the same per-PR lookup `classify_ship_status` uses -
    never the bulk list's `merged`/`state` fields directly, per DAN-266).

    A branch with no PR on GitHub at all, or no merged PR, reports unshipped
    (fail closed, matching this guard's existing convention) - including
    when `gh` itself is unavailable, since `gh_pr_numbers_for_branch` and
    `gh_pr_state` both already treat a plumbing failure as "no evidence".
    """
    evidence: list[str] = []
    pr_numbers = gh_pr_numbers_for_branch(repo_slug, branch, gh_bin=gh_bin)
    if not pr_numbers:
        evidence.append(f"no PR found on {repo_slug} with head branch {branch!r}")
        return ShipVerdict(shipped=False, reason="no_signal", evidence=evidence)

    for pr_number in pr_numbers:
        state = gh_pr_state(repo_slug, pr_number, gh_bin=gh_bin)
        evidence.append(f"PR #{pr_number} (head={branch!r}) state={state!r}")
        if state == "MERGED":
            return ShipVerdict(shipped=True, reason="pr_merged", evidence=evidence)

    evidence.append(f"no merged PR found for head branch {branch!r}")
    return ShipVerdict(shipped=False, reason="orphaned", evidence=evidence)


def _cli_branch_shipped(args: argparse.Namespace) -> int:
    verdict = branch_ship_verdict(
        args.repo, args.branch, repo_slug=args.repo_slug, gh_bin=args.gh_bin
    )
    print(json.dumps({"shipped": verdict.shipped, "reason": verdict.reason, "evidence": verdict.evidence}))
    return 0 if verdict.shipped else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    branch_shipped = subparsers.add_parser(
        "branch-shipped",
        help="exit 0 if a local branch already shipped via a merged PR, else 1",
    )
    branch_shipped.add_argument("repo", help="path to the git checkout (unused by the GitHub lookup, kept for a consistent CLI shape and future local-only signals)")
    branch_shipped.add_argument("branch", help="local branch name to classify")
    branch_shipped.add_argument("--repo-slug", default="Dromares/Hatate-Linux-Redux")
    branch_shipped.add_argument("--gh-bin", default="gh")
    branch_shipped.set_defaults(func=_cli_branch_shipped)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
