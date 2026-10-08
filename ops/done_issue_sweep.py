"""Scheduled sweep for the done/origin-main ancestry guard (DAN-116).

Finds `done` issues whose referenced commit/PR never actually reached
`origin/main` and reports them - reopening to `blocked` when run with
`--apply`. This is a separate, NON-BLOCKING signal meant to run on a
schedule against already-closed issues. It must never be wired up as a CI
gate on an in-flight change: rate limits and transient GitHub API failures
would wedge the pipeline for something that isn't even about the current
change.

Default is dry-run (report only). `--apply` is what actually reopens an
issue, and it does so conservatively: an issue is only flagged when it
carries at least one commit SHA or PR reference that a human or agent can
verify by hand, and only when every content check in
`ops.ancestry_guard.classify_ship_status` agrees nothing landed. An issue
with no such reference is skipped, not flagged - "the guard couldn't find
anything to check" is not the same claim as "the guard checked and found
nothing".

DAN-266: the CLI now wires a real per-PR GitHub merged-state lookup
(`ops.ancestry_guard.github_pr_state_lookup_factory`, backed by `gh pr
view` - never the bulk `gh pr list`/"list pull requests" endpoint, which
was observed serving a stale `merged` field) into `classify_ship_status`'s
`pr_state_lookup` by default. Pass `--no-github` to fall back to
ancestry+grep only (e.g. no `gh`/network available in the run).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ops.ancestry_guard import (
    ShipVerdict,
    classify_ship_status,
    extract_pr_numbers,
    extract_shas,
    github_pr_state_lookup_factory,
    relevant_shas,
)

REOPEN_COMMENT_HEADER = (
    "Reopened by the done/origin-main ancestry guard (DAN-116): no commit or "
    "merged PR for this issue was found on `{ref}`."
)


@dataclass
class SweepTarget:
    issue_id: str
    identifier: str
    text: str  # title + description + comment bodies, for reference extraction


class PaperclipClient:
    """Thin HTTP wrapper around the Paperclip issues API.

    Kept separate from `run_sweep` so tests can swap in a fake that never
    touches the network.
    """

    def __init__(self, api_base: str, api_key: str, run_id: Optional[str] = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id

    def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        if self.run_id:
            req.add_header("X-Paperclip-Run-Id", self.run_id)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def list_done_issues(self, company_id: str, project_id: str) -> list[dict[str, Any]]:
        path = f"/api/companies/{company_id}/issues?projectId={project_id}&status=done"
        result = self._request("GET", path)
        return result.get("issues", []) if isinstance(result, dict) else result

    def list_comments(self, issue_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/api/issues/{issue_id}/comments")
        return result.get("comments", []) if isinstance(result, dict) else result

    def reopen_as_blocked(self, issue_id: str, comment: str) -> Any:
        return self._request(
            "PATCH",
            f"/api/issues/{issue_id}",
            {"status": "blocked", "comment": comment},
        )


def build_target(issue: dict[str, Any], comments: list[dict[str, Any]]) -> SweepTarget:
    parts = [issue.get("title", ""), issue.get("description", "")]
    parts.extend((c.get("body") or "") for c in comments)
    return SweepTarget(
        issue_id=issue["id"],
        identifier=issue.get("identifier", issue["id"]),
        text="\n".join(parts),
    )


def evaluate_target(
    repo: str,
    target: SweepTarget,
    ref: str = "origin/main",
    pr_state_lookup_factory: Optional[Callable[[int], Callable[[], Optional[str]]]] = None,
) -> Optional[ShipVerdict]:
    """Classify one target, or return None if it carries nothing to check.

    Gating requires at least one commit SHA that both resolves to a real
    object in `repo` AND whose own commit subject claims this issue
    (`relevant_shas`) - not merely any hex-looking token in the text. Two
    weaker signals were tried and both produced real false positives on
    this guard's own live dry runs against the board, which is why neither
    is a gate on its own:

      - a bare PR-number mention: DAN-40 and DAN-66 both quote *another*
        issue's PR number in passing (DAN-37's #13, DAN-68's #21) without
        ever landing code of their own.
      - any resolvable SHA, relevance unchecked: DAN-65's thread cites
        a713036 - a real commit, an ancestor of main, and entirely about
        DAN-58 - as an incidental status note. Treating it as DAN-65's own
        fix would have reported DAN-65 as confidently "shipped" (or, had
        the citation been a non-ancestor, confidently "orphaned") on
        evidence belonging to a different issue.

    An issue with no SHA passing that bar is skipped, not flagged: "the
    guard found nothing to check" is not the same claim as "the guard
    checked and found nothing landed". PR numbers are still forwarded to
    `pr_state_lookup_factory` as supplementary evidence once a relevant SHA
    has already earned the issue a real evaluation.
    """
    shas = extract_shas(repo, target.text)
    own_shas = relevant_shas(repo, target.identifier, shas)
    if not own_shas:
        return None

    pr_numbers = extract_pr_numbers(target.text)
    pr_lookup = None
    if pr_numbers and pr_state_lookup_factory is not None:
        pr_lookup = pr_state_lookup_factory(pr_numbers[0])

    return classify_ship_status(repo, target.identifier, own_shas, ref=ref, pr_state_lookup=pr_lookup)


def run_sweep(
    client: PaperclipClient,
    company_id: str,
    project_id: str,
    repo: str,
    ref: str = "origin/main",
    apply: bool = False,
    pr_state_lookup_factory: Optional[Callable[[int], Callable[[], Optional[str]]]] = None,
) -> list[dict[str, Any]]:
    report = []
    for issue in client.list_done_issues(company_id, project_id):
        comments = client.list_comments(issue["id"])
        target = build_target(issue, comments)
        verdict = evaluate_target(repo, target, ref=ref, pr_state_lookup_factory=pr_state_lookup_factory)
        if verdict is None:
            continue

        row = {
            "identifier": target.identifier,
            "issue_id": target.issue_id,
            "shipped": verdict.shipped,
            "reason": verdict.reason,
            "evidence": verdict.evidence,
        }
        report.append(row)

        if not verdict.shipped and apply:
            comment = REOPEN_COMMENT_HEADER.format(ref=ref) + "\n\n" + "\n".join(
                f"- {line}" for line in verdict.evidence
            )
            client.reopen_as_blocked(target.issue_id, comment)

    return report


def _normalize_api_base(raw: str) -> str:
    base = raw.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return base


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="path to the Hatate-Linux-Redux checkout")
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID"))
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="reopen orphaned issues to blocked; default is dry-run/report-only",
    )
    parser.add_argument("--api-base", default=os.environ.get("PAPERCLIP_API_URL"))
    parser.add_argument(
        "--repo-slug",
        default="Dromares/Hatate-Linux-Redux",
        help="GitHub owner/repo used for the per-PR merged-state lookup (DAN-266)",
    )
    parser.add_argument(
        "--no-github",
        action="store_true",
        help="skip the per-PR GitHub merged-state check and rely on ancestry+grep only "
        "(e.g. no gh/network available); default fetches real PR state via `gh pr view`",
    )
    args = parser.parse_args(argv)

    if not args.company_id:
        parser.error("--company-id is required (or set PAPERCLIP_COMPANY_ID)")
    if not args.api_base:
        parser.error("--api-base is required (or set PAPERCLIP_API_URL)")

    api_key = os.environ.get("PAPERCLIP_API_KEY")
    if not api_key:
        parser.error("PAPERCLIP_API_KEY must be set in the environment")

    client = PaperclipClient(
        _normalize_api_base(args.api_base), api_key, os.environ.get("PAPERCLIP_RUN_ID")
    )
    pr_state_lookup_factory = (
        None if args.no_github else github_pr_state_lookup_factory(args.repo_slug)
    )
    report = run_sweep(
        client,
        args.company_id,
        args.project_id,
        args.repo,
        ref=args.ref,
        apply=args.apply,
        pr_state_lookup_factory=pr_state_lookup_factory,
    )
    print(json.dumps(report, indent=2))

    orphans = [row for row in report if not row["shipped"]]
    if orphans:
        mode = "reopened" if args.apply else "dry-run, not reopened"
        print(f"\n{len(orphans)} orphaned issue(s) found ({mode})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
