#!/usr/bin/env bash
#
# Merge a PR with the backup GitHub token (GITHUB_BACKUP_TOKEN) while the
# managed identity is unavailable. Wraps scripts/merge_pr.sh; see
# ops/RUNBOOK-github-backup-token.md (DAN-1145, DAN-1284) for when this is
# permitted at all. Run the section-1 probe first - this script does not.
#
# Usage:
#   scripts/merge_pr_backup.sh <pr-number> [extra merge_pr.sh args...]
#
# Why a wrapper: under the Paperclip launcher the `gh`/`git` shims strip
# GH_TOKEN and GIT_CONFIG_*, and the run's BASH_ENV hook re-prepends the shim
# directory to PATH in every non-interactive bash script (including
# merge_pr.sh). So `GH_TOKEN=... scripts/merge_pr.sh N` never reaches the real
# gh. This script (1) drops the shim directory from PATH, (2) hands git a
# one-shot credential helper through the environment, (3) fetches the PR head
# so merge_pr.sh's staleness gate can resolve its SHA locally, and (4) runs
# merge_pr.sh unmodified with BASH_ENV unset.
#
# The token is never echoed, never put in argv, a remote URL or git config on
# disk, and is exported to nothing but the final merge_pr.sh process: GH_TOKEN
# is a prefix assignment on `exec`, and the git helper reads
# $GITHUB_BACKUP_TOKEN from the environment at call time.
#
# Exit status: merge_pr.sh's (0 merged, 1 stale, 2 usage/setup, or gh's own).

set -euo pipefail
set +x

pr_number="${1:-}"
if [[ -z "$pr_number" || "$pr_number" == "-h" || "$pr_number" == "--help" ]]; then
  echo "Usage: scripts/merge_pr_backup.sh <pr-number> [extra merge_pr.sh args...]" >&2
  exit 2
fi
if ! [[ "$pr_number" =~ ^[0-9]+$ ]]; then
  echo "ERROR: PR number must be numeric, got '$pr_number'." >&2
  exit 2
fi
shift

if [[ -z "${GITHUB_BACKUP_TOKEN:-}" ]]; then
  echo "ERROR: GITHUB_BACKUP_TOKEN is not set on this seat." >&2
  exit 2
fi

# Drop every launcher shim directory from PATH.
clean_path=""
IFS=: read -ra path_entries <<<"$PATH"
for entry in "${path_entries[@]}"; do
  case "$entry" in
    "${PAPERCLIP_GITHUB_LAUNCHER_DIR:-/nonexistent}"|*/paperclip-github-runtime/*) continue ;;
  esac
  clean_path="${clean_path:+$clean_path:}$entry"
done
export PATH="$clean_path"
unset BASH_ENV

for tool in gh git; do
  resolved="$(command -v "$tool" || true)"
  if [[ -z "$resolved" || "$resolved" == */paperclip-github-runtime/* ]]; then
    echo "ERROR: could not resolve a real '$tool' outside the launcher shim (got '${resolved:-nothing}')." >&2
    exit 2
  fi
done

# One-shot git credential helper, delivered via env so no file is touched.
# The first entry resets any inherited helper list; the second reads the
# token from the environment when git calls it.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0=
export GIT_CONFIG_KEY_1=credential.helper
export GIT_CONFIG_VALUE_1='!f() { echo username=x-access-token; echo "password=$GITHUB_BACKUP_TOKEN"; }; f'
export GIT_TERMINAL_PROMPT=0

cd "$(git rev-parse --show-toplevel)"

# merge_pr.sh resolves the PR head SHA through gh, then asks git about it.
# A clone that never fetched the PR head fails that lookup with "ref not
# found", so make sure the head object is local first.
git fetch --quiet origin "refs/pull/$pr_number/head"

GH_TOKEN="$GITHUB_BACKUP_TOKEN" exec scripts/merge_pr.sh "$pr_number" "$@"
