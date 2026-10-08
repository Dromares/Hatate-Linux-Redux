#!/usr/bin/env bash
#
# Staleness gate for the merge path.
#
# Two individually-green PRs can still break `main` on contact when each
# was tested against a `main` that has since moved - the collision
# "Require branches to be up to date before merging" exists to prevent.
# That branch-protection setting is unavailable on this repo's GitHub plan
# (private repo, free tier - confirmed via `gh api
# repos/Dromares/Hatate-Linux-Redux/branches/main/protection` returning
# 403 "Upgrade to GitHub Pro or make this repository public"), so the
# only enforcement point left is the merge path itself: this script, run
# both in CI (.github/workflows/tests.yml, on pull_request) and by hand
# as the last step before merging (see CLAUDE.md).
#
# This is a PRESENCE check for one specific fact: is <remote>/<branch>
# (default origin/main) an ancestor of the ref being checked? It does not
# re-run tests and does not know anything about the *content* of the
# commits main gained - only that the branch under test has them. A
# green "up to date" result plus green CI is what makes the merge safe;
# either alone is not.
#
# Usage:
#   scripts/check_merge_base.sh [ref]
#   scripts/check_merge_base.sh --help
#
# Options:
#   -r, --remote NAME Git remote to check against (default: origin).
#   -b, --branch NAME Branch to check against (default: main).
#   -h, --help         Show this help.
#
# [ref] defaults to HEAD. Pass a branch name, tag, or SHA to check
# something other than the current checkout.
#
# Exit status: 0 if <remote>/<branch> is an ancestor of [ref] (i.e. [ref]
# is up to date), 1 if it is not, 2 on usage/setup error.

set -euo pipefail

print_help() {
  sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

REMOTE="origin"
BRANCH="main"
REF=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_help
      exit 0
      ;;
    -r|--remote)
      [[ $# -ge 2 ]] || { echo "ERROR: --remote requires a value." >&2; exit 2; }
      REMOTE="$2"
      shift 2
      ;;
    -b|--branch)
      [[ $# -ge 2 ]] || { echo "ERROR: --branch requires a value." >&2; exit 2; }
      BRANCH="$2"
      shift 2
      ;;
    -*)
      echo "ERROR: unknown option: $1 (see --help)" >&2
      exit 2
      ;;
    *)
      [[ -z "$REF" ]] || { echo "ERROR: only one ref may be given." >&2; exit 2; }
      REF="$1"
      shift
      ;;
  esac
done

[[ -n "$REF" ]] || REF="HEAD"

cd "$(git rev-parse --show-toplevel)"

if ! git fetch "$REMOTE" "$BRANCH" --quiet 2>/dev/null; then
  # A stale local view of $REMOTE/$BRANCH can report PASS when the real
  # branch has since moved past it - falling back to "last known" turns
  # a network blip into a silent false green. This is the one gate DAN-213
  # left standing in for branch protection, so a failed fetch must be red,
  # not a shrug.
  echo "ERROR: could not fetch $REMOTE/$BRANCH; refusing to check a possibly-stale local ref." >&2
  exit 2
fi

base_ref="$REMOTE/$BRANCH"
git rev-parse --verify --quiet "$base_ref" >/dev/null || {
  echo "ERROR: ref $base_ref not found (bad --remote/--branch, or never fetched)." >&2
  exit 2
}

ref_sha="$(git rev-parse --verify --quiet "$REF" 2>/dev/null || true)"
[[ -n "$ref_sha" ]] || {
  echo "ERROR: ref $REF not found." >&2
  exit 2
}

base_sha="$(git rev-parse "$base_ref")"

if git merge-base --is-ancestor "$base_sha" "$ref_sha"; then
  echo "PASS: $REF is up to date with $base_ref ($base_sha)."
  exit 0
fi

behind_count="$(git rev-list --count "$ref_sha..$base_sha")"
echo "FAIL: $REF is STALE - $base_ref has $behind_count commit(s) not in $REF:" >&2
git log "$ref_sha..$base_sha" --format='  %h %s' >&2
echo "Merge/rebase $base_ref into $REF and re-run before merging." >&2
exit 1
