#!/usr/bin/env bash
#
# DAN-651: local CI-matrix attestation harness.
#
# GitHub Actions is down on this repo for billing reasons (DAN-647), and
# only the board can restore it. Until it is back, this script is the
# substitute merge gate: a third-agent-run, machine-greppable attestation
# that a specific commit passes what .github/workflows/tests.yml's `test`
# and `lint` jobs check, taking the place of a green GitHub Actions run.
#
# Why `uv python install` and not just the system interpreter: this host's
# only system Python is 3.14, which is neither version CI gates (3.10,
# 3.13). Testing only under 3.14 would attest nothing about the actual
# supported matrix, so this script provisions the exact two CPython
# versions tests.yml's `test` matrix uses and runs the suite under each,
# in its own throwaway venv.
#
# RESIDUAL GAP - read before trusting this as equivalent to CI: CI runs on
# `ubuntu-latest` and installs Qt's runtime libraries (libegl1, libgl1,
# libxkbcommon-x11-0, libdbus-1-3, and the libxcb-* family) via `apt`
# before every test leg (see tests.yml). This host is Arch-family; it has
# whatever Qt runtime libraries already happen to be installed, not a
# fresh ubuntu-latest image, and this script makes no attempt to
# reconcile the two. This gate attests the INTERPRETER MATRIX (3.10,
# 3.13) against this host's existing OS libraries. It is not an OS-level
# equivalence check, and a PyQt6/Qt regression that depends on exact
# ubuntu-latest library versions can still slip past it.
#
# What this does NOT do: modify .github/workflows/tests.yml. That
# definition is fine; the account billing is what's broken. This script
# exists so "I ran the tests locally" is a verifiable claim instead of an
# unverifiable one while that's true - not to replace CI once it's back.
#
# Usage:
#   scripts/ci_local_matrix.sh <sha>
#
# <sha> is the commit this attestation is FOR. The script refuses to run
# unless the current checkout's HEAD already resolves to exactly that
# commit AND the working tree is clean (`git status --porcelain` empty) -
# the attestation must name the exact commit that will land, not "close
# enough" or a dirty approximation of it. Run this from the worktree/
# checkout that already has the commit under test checked out.
#
# Exit status: 0 only if every leg (both interpreter test runs, ruff,
# mypy) passes. Non-zero otherwise. There is no partial-green outcome:
# a failing leg still prints the full attestation block (showing exactly
# what failed), then the script exits non-zero.

set -euo pipefail

PYTHON_VERSIONS=("3.10" "3.13")
# Matches tests.yml's `lint` job, which runs on 3.13.
LINT_PYTHON_VERSION="3.13"
MYPY_TARGETS=(core gui workers tools ops main.py)

die() {
  echo "ERROR: $*" >&2
  exit 2
}

[[ $# -eq 1 ]] || die "usage: $0 <sha>"
ATTEST_REF="$1"

command -v uv >/dev/null 2>&1 || die "uv is required (expected at /usr/bin/uv or on PATH) but was not found."

REPO_ROOT="$(git rev-parse --show-toplevel)" || die "not inside a git repository."
cd "$REPO_ROOT"

ATTEST_SHA="$(git rev-parse --verify --quiet "${ATTEST_REF}^{commit}")" \
  || die "ref '$ATTEST_REF' does not resolve to a commit in this checkout."

HEAD_SHA="$(git rev-parse HEAD)"
[[ "$HEAD_SHA" == "$ATTEST_SHA" ]] \
  || die "HEAD ($HEAD_SHA) does not match the attested commit ($ATTEST_SHA). Check out $ATTEST_REF in this worktree first; this script will not check it out for you."

DIRTY="$(git status --porcelain)"
[[ -z "$DIRTY" ]] \
  || die "working tree is dirty; refusing to attest an uncommitted/mixed state:
$DIRTY"

BRANCH="$(git symbolic-ref --short -q HEAD || echo "(detached HEAD)")"

git fetch origin main --quiet 2>/dev/null \
  || die "could not fetch origin/main; refusing to compute a possibly-stale merge-base."
ORIGIN_MAIN_SHA="$(git rev-parse origin/main)"
MERGE_BASE_SHA="$(git merge-base origin/main "$ATTEST_SHA")"
BEHIND_MAIN="$(git rev-list --count "${MERGE_BASE_SHA}..${ORIGIN_MAIN_SHA}")"
if [[ "$MERGE_BASE_SHA" == "$ORIGIN_MAIN_SHA" ]]; then
  MERGE_BASE_SUMMARY="up_to_date (merge-base=origin/main=${ORIGIN_MAIN_SHA})"
else
  MERGE_BASE_SUMMARY="stale (merge-base=${MERGE_BASE_SHA}, origin/main is ${BEHIND_MAIN} commit(s) ahead)"
fi

WORKDIR="$(mktemp -d -t ci-local-matrix.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

OVERALL_STATUS=0
declare -A RESOLVED_VERSION
declare -A TEST_STATUS
declare -A TEST_DETAIL

for version in "${PYTHON_VERSIONS[@]}"; do
  echo "== Python $version: provisioning via uv ==" >&2
  if ! uv python install "$version" >&2; then
    RESOLVED_VERSION["$version"]="UNAVAILABLE"
    TEST_STATUS["$version"]="FAIL"
    TEST_DETAIL["$version"]="uv could not provision CPython $version on this host - not run, not silently substituted with another interpreter."
    OVERALL_STATUS=1
    continue
  fi

  venv_dir="$WORKDIR/venv-$version"
  uv venv --python "$version" "$venv_dir" --quiet \
    || die "uv venv failed for Python $version."

  resolved="$("$venv_dir/bin/python" -V 2>&1)"
  RESOLVED_VERSION["$version"]="$resolved"

  # shellcheck disable=SC1091
  source "$venv_dir/bin/activate"
  if ! uv pip install -q -r requirements.txt -r requirements-dev.txt; then
    TEST_STATUS["$version"]="FAIL"
    TEST_DETAIL["$version"]="dependency install failed - see stderr above."
    OVERALL_STATUS=1
    deactivate
    continue
  fi

  log="$WORKDIR/run_tests-$version.log"
  echo "== Python $version ($resolved): running ./run_tests.sh ==" >&2
  export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"
  if ./run_tests.sh >"$log" 2>&1; then
    TEST_STATUS["$version"]="PASS"
  else
    TEST_STATUS["$version"]="FAIL"
    OVERALL_STATUS=1
  fi
  ran_line="$(grep -E '^Ran [0-9]+ tests? in' "$log" | tail -1 || true)"
  result_line="$(grep -E '^(OK|FAILED)' "$log" | tail -1 || true)"
  if [[ -n "$ran_line" ]]; then
    TEST_DETAIL["$version"]="${ran_line}; ${result_line:-no OK/FAILED summary line found}"
  else
    TEST_DETAIL["$version"]="run_tests.sh exited before reaching the test run (see $log for the full log, discarded on exit) - check for a lint failure inside run_tests.sh."
  fi
  deactivate
  echo "$(tail -5 "$log")" >&2
done

# One lint pass overall, under the same interpreter tests.yml's `lint` job
# uses, mirroring that job rather than duplicating per matrix leg.
LINT_STATUS="SKIPPED"
RUFF_STATUS="SKIPPED"
MYPY_STATUS="SKIPPED"
if [[ "${RESOLVED_VERSION[$LINT_PYTHON_VERSION]:-}" != "UNAVAILABLE" && -d "$WORKDIR/venv-$LINT_PYTHON_VERSION" ]]; then
  # shellcheck disable=SC1091
  source "$WORKDIR/venv-$LINT_PYTHON_VERSION/bin/activate"

  echo "== ruff check . ==" >&2
  if ruff check . >&2; then
    RUFF_STATUS="PASS"
  else
    RUFF_STATUS="FAIL"
    OVERALL_STATUS=1
  fi

  echo "== mypy ${MYPY_TARGETS[*]} ==" >&2
  if mypy "${MYPY_TARGETS[@]}" >&2; then
    MYPY_STATUS="PASS"
  else
    MYPY_STATUS="FAIL"
    OVERALL_STATUS=1
  fi

  deactivate
else
  RUFF_STATUS="FAIL"
  MYPY_STATUS="FAIL"
  OVERALL_STATUS=1
fi

OVERALL_LABEL="PASS"
[[ "$OVERALL_STATUS" -eq 0 ]] || OVERALL_LABEL="FAIL"

TIMESTAMP_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
HOST_OS="$(uname -srm)"
HOST_OS_RELEASE="$( ( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" ) || echo "unknown")"

echo
echo "===== CI-LOCAL-MATRIX-ATTESTATION ====="
echo "sha: $ATTEST_SHA"
echo "branch: $BRANCH"
echo "merge_base_origin_main: $MERGE_BASE_SUMMARY"
for version in "${PYTHON_VERSIONS[@]}"; do
  slug="${version//./}"
  echo "python_${slug}_requested: $version"
  echo "python_${slug}_resolved: ${RESOLVED_VERSION[$version]:-UNAVAILABLE}"
  echo "python_${slug}_tests: ${TEST_STATUS[$version]:-FAIL} (${TEST_DETAIL[$version]:-not run})"
done
echo "ruff: $RUFF_STATUS"
echo "mypy: $MYPY_STATUS"
echo "host_os: $HOST_OS ($HOST_OS_RELEASE) - NOTE: CI runs ubuntu-latest; this is Arch-family. Interpreter matrix only, not an OS equivalence check."
echo "timestamp_utc: $TIMESTAMP_UTC"
echo "overall: $OVERALL_LABEL"
echo "===== END ATTESTATION ====="

exit "$OVERALL_STATUS"
