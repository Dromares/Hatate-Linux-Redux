#!/usr/bin/env bash
#
# The one sanctioned way to merge a PR on this repo.
#
# scripts/check_merge_base.sh is a correct staleness gate, but a script an
# agent has to remember to run *before* a separate `gh pr merge` call is
# the exact failure class DAN-213 exists to close - the merge still
# happens even if that reminder gets skipped. This wrapper makes the
# check an inseparable part of the merge action itself: there is no
# argument list that merges without it passing first. Skipping the gate
# now requires deliberately going around this script (calling
# `gh pr merge` directly) instead of just forgetting a documented step.
# See CLAUDE.md "Before merging" and DAN-213.
#
# Usage:
#   scripts/merge_pr.sh <pr-number> [extra gh pr merge args...]
#
# Merge strategy defaults to `--merge` (this repo's historical convention
# - see `git log --merges`). Pass -m/--merge, --squash, or --rebase to
# override, plus any other `gh pr merge` flag (e.g. --admin, --subject).
#
# Exit status: 0 on successful merge, 1 if the PR is stale (not merged),
# 2 on usage/setup error, or gh's own exit status if the merge call fails.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

pr_number="${1:-}"
if [[ -z "$pr_number" || "$pr_number" == "-h" || "$pr_number" == "--help" ]]; then
  echo "Usage: scripts/merge_pr.sh <pr-number> [extra gh pr merge args...]" >&2
  exit 2
fi
shift

head_sha="$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)"
[[ -n "$head_sha" ]] || {
  echo "ERROR: could not resolve head SHA for PR #$pr_number (bad number, or gh auth issue)." >&2
  exit 2
}

echo "Checking staleness of PR #$pr_number (head $head_sha) against origin/main..."
if ! scripts/check_merge_base.sh "$head_sha"; then
  echo "ABORT: PR #$pr_number is stale. Not merging. Merge/rebase main into the" >&2
  echo "branch, let CI go green again, and re-run this script." >&2
  exit 1
fi

strategy_given=0
for arg in "$@"; do
  case "$arg" in
    -m|--merge|-s|--squash|-r|--rebase) strategy_given=1 ;;
  esac
done

merge_args=("$@")
if [[ "$strategy_given" -eq 0 ]]; then
  merge_args=("--merge" "${merge_args[@]}")
fi

echo "OK: PR #$pr_number is up to date with origin/main. Merging..."
exec gh pr merge "$pr_number" "${merge_args[@]}"
