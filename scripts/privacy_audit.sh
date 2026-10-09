#!/usr/bin/env bash
#
# DAN-714: scripted close-out privacy re-verification harness.
#
# Captures the DAN-668 pre-publication audit method as a repeatable check,
# so re-verifying a tree before a repo-visibility flip takes minutes, not
# another multi-hour manual pass. This is the audit's PROCEDURE as code -
# it does not re-adjudicate DAN-668's findings, and it does not remediate
# anything itself.
#
# HARD CONSTRAINT THIS SCRIPT OBEYS: it never hardcodes a real PII literal
# (the board's OS username, personal email, or real name). Those values
# are supplied at runtime only, via --patterns-file (an untracked file
# you point at) or --safe-domains-file. A committed scanner that bakes in
# what it scans for is itself a leak (this is exactly what DAN-668's first
# report draft got sent back for). Every built-in pattern below is
# SHAPE-based (a generic "/home/<anything>" path, an email-address shape,
# a generic noreply-github pattern) - none of them name this repo's real
# board identity.
#
# WHY THE A1 (COMMIT-METADATA) CHECK EXISTS AND WHY IT DOESN'T GATE BY
# DEFAULT: author/committer name+email on every commit is the one leak
# class that a working-tree grep and every secret scanner (gitleaks,
# detect-secrets) ALL miss - none of them look at commit metadata. A
# "scanner clean" result from either tool says nothing about this count.
# DAN-668 found a real personal
# email on 86/265 commits this way; no content scanner would ever have
# caught it. This check always runs and always reports its count. It does
# NOT fail the overall exit code by default, because remediating it needs
# a board-level history decision (mailmap rewrite + force-push, or a
# fresh-history squash) that is explicitly out of scope for this ticket
# (DAN-714) and for DAN-668 itself - see --fail-on-metadata if you need a
# strict gate once that decision has been executed. A clean exit from
# this script's DEFAULT invocation is NOT a claim that commit metadata is
# clean; read the printed count.
#
# Usage:
#   scripts/privacy_audit.sh [options] [<commit-ish>]
#
# <commit-ish> defaults to HEAD. Use --repo to point at a different git
# checkout (e.g. a freshly cloned fresh-history publish candidate) without
# switching this checkout's HEAD.
#
# Options:
#   --repo <path>             Git repository to audit (default: the repo
#                              containing this script's current checkout).
#   --history                 Also scan the FULL object database -
#                              reachable AND unreachable/deleted blobs -
#                              for the same shape patterns, and (if
#                              gitleaks is installed) run gitleaks in its
#                              git-history mode. This is the mode that
#                              found DAN-668's A5 (a deleted-but-historical
#                              file). Skippable: under a fresh-history
#                              publish there is exactly one commit and no
#                              unreachable objects, so this mode is
#                              trivially satisfied and need not be run.
#   --patterns-file <path>    Untracked file of exact, high-confidence
#                              literal patterns to treat as PII wherever
#                              found (tracked tree, history, nowhere
#                              excluded for being "just a test fixture").
#                              One `LABEL<TAB>REGEX` pair per line (POSIX
#                              extended regex), blank lines and lines
#                              starting with `#` ignored. Put the board's
#                              real username, real name, and real email
#                              local-part here at runtime. NEVER commit
#                              this file.
#   --safe-domains-file <path> Override the built-in list of commit-email
#                              domain/address patterns treated as safe
#                              (non-PII) identities for the A1 check - one
#                              POSIX extended regex per line. Defaults
#                              cover the GitHub-issued noreply address
#                              shape and this company's own synthetic
#                              agent-identity domains, none of which are
#                              personal data.
#   --fail-on-metadata         Make a nonzero A1 commit-metadata count
#                              fail the overall exit code. Off by default
#                              - see the long comment above.
#   --fail-on-assets           Make the presence of any review-flagged
#                              image/binary asset fail the overall exit
#                              code. Off by default: this check cannot OCR
#                              an image, so by design it only ever
#                              produces an honest "needs a human look",
#                              never a scanner-verified pass.
#   --show-matches             Print the raw matched file:line content
#                              instead of a redacted placeholder. For
#                              local remediation use only - never paste
#                              this output into a comment, commit message,
#                              or any other committed artifact. The A1
#                              check's emails are ALWAYS local-part-masked
#                              regardless of this flag; that check exists
#                              specifically to report a real personal
#                              address, and this script will not become a
#                              second place that address is reproduced.
#   -h, --help                 Show this help and exit 0.
#
# Exit status: 0 only if every check that ran, and that is configured to
# gate the result, found nothing. Non-zero otherwise. Checks that merely
# report (A1 by default, asset review by default) still print their
# findings either way - see the attestation block's RAN/SKIPPED lines and
# read them; a 0 exit here is not an unconditional "nothing to see."

set -euo pipefail

die() {
  echo "ERROR: $*" >&2
  exit 2
}

usage() {
  sed -n '2,/^set -euo pipefail/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

# ---- built-in shape-based patterns (no PII literals) ----------------------

HOME_PATH_PATTERN='/home/[A-Za-z0-9._-]+'
EMAIL_PATTERN='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'

# Test/fixture paths are excluded from the FATAL scope of the generic
# home-path/email heuristics and instead reported as REVIEW. This mirrors
# DAN-668's own adjudication: every historical hit of this shape outside a
# real leak turned out to be a synthetic test fixture - a one-letter
# placeholder username under a fake cache directory, deliberately not
# reproduced verbatim here so this comment doesn't trip the very pattern
# it describes - and treating those as fatal would make the tracked-tree
# check permanently red for reasons that have nothing to do with a real
# leak.
TEST_PATHSPECS=(':!tests/*' ':!**/test_*.py' ':!**/*_test.py' ':!**/conftest.py')

# Default A1 safe-identity patterns: the GitHub-issued noreply address
# shape (not personal data - GitHub assigns it), and this company's own
# synthetic agent-identity domains (also not personal data - no real
# person owns a @dante.inferno/@dantesinferno.local/@dantes-inferno.local
# address). Anything else is a candidate real personal identity.
DEFAULT_SAFE_DOMAIN_PATTERNS=(
  '^[0-9]+\+[A-Za-z0-9._-]+@users\.noreply\.github\.com$'
  '^noreply@github\.com$'
  '@dante\.inferno$'
  '@dantesinferno\.local$'
  '@dantes-inferno\.local$'
)

IMAGE_EXT_PATTERN='\.(png|jpe?g|gif|bmp|ico|webp)$'

# ---- argument parsing -------------------------------------------------------

REPO=""
HISTORY=0
PATTERNS_FILE=""
SAFE_DOMAINS_FILE=""
FAIL_ON_METADATA=0
FAIL_ON_ASSETS=0
SHOW_MATCHES=0
TARGET_REF="HEAD"
TARGET_SET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a path argument"
      REPO="$2"; shift 2 ;;
    --history)
      HISTORY=1; shift ;;
    --patterns-file)
      [[ $# -ge 2 ]] || die "--patterns-file requires a path argument"
      PATTERNS_FILE="$2"; shift 2 ;;
    --safe-domains-file)
      [[ $# -ge 2 ]] || die "--safe-domains-file requires a path argument"
      SAFE_DOMAINS_FILE="$2"; shift 2 ;;
    --fail-on-metadata)
      FAIL_ON_METADATA=1; shift ;;
    --fail-on-assets)
      FAIL_ON_ASSETS=1; shift ;;
    --show-matches)
      SHOW_MATCHES=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; break ;;
    -*)
      die "unknown option: $1" ;;
    *)
      [[ "$TARGET_SET" -eq 0 ]] || die "unexpected extra argument: $1"
      TARGET_REF="$1"; TARGET_SET=1; shift ;;
  esac
done

if [[ -n "$PATTERNS_FILE" ]]; then
  [[ -f "$PATTERNS_FILE" ]] || die "--patterns-file '$PATTERNS_FILE' does not exist"
fi
if [[ -n "$SAFE_DOMAINS_FILE" ]]; then
  [[ -f "$SAFE_DOMAINS_FILE" ]] || die "--safe-domains-file '$SAFE_DOMAINS_FILE' does not exist"
fi

if [[ -z "$REPO" ]]; then
  REPO="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || die "not inside a git repository; pass --repo <path>"
fi
[[ -d "$REPO/.git" || -f "$REPO/.git" ]] || die "'$REPO' is not a git repository"

TARGET_SHA="$(git -C "$REPO" rev-parse --verify --quiet "${TARGET_REF}^{commit}")" \
  || die "ref '$TARGET_REF' does not resolve to a commit in '$REPO'"

WORKDIR="$(mktemp -d -t privacy-audit.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

# ---- helpers -----------------------------------------------------------------

redact_grep_output() {
  # Input lines look like "<sha>:<path>:<line>:<content>". Default: drop
  # the content field. --show-matches keeps it (local debugging only).
  if [[ "$SHOW_MATCHES" -eq 1 ]]; then
    cat
  else
    cut -d: -f1-3
  fi
}

mask_email() {
  # Always masks the local part, regardless of --show-matches - see the
  # header comment on why A1 never reproduces the literal address.
  local email="$1" local_part domain
  local_part="${email%@*}"
  domain="${email#*@}"
  if [[ -z "$local_part" ]]; then
    echo "***@${domain}"
  else
    echo "${local_part:0:1}***@${domain}"
  fi
}

is_safe_email() {
  local email="$1" pattern
  for pattern in "${SAFE_DOMAIN_PATTERNS[@]}"; do
    [[ "$email" =~ $pattern ]] && return 0
  done
  return 1
}

# git-grep exit codes: 0 = match found, 1 = no match, >1 = real error.
grep_tree() {
  # grep_tree <pattern> <out-file> [pathspec ...]
  local pattern="$1" outfile="$2"
  shift 2
  set +e
  git -C "$REPO" grep -a -n -E "$pattern" "$TARGET_SHA" -- "$@" >"$outfile"
  local status=$?
  set -e
  [[ "$status" -le 1 ]] || die "git grep failed (exit $status) for pattern: $pattern"
  wc -l <"$outfile" | tr -d ' '
}

# ---- load overrides ------------------------------------------------------

SAFE_DOMAIN_PATTERNS=("${DEFAULT_SAFE_DOMAIN_PATTERNS[@]}")
if [[ -n "$SAFE_DOMAINS_FILE" ]]; then
  SAFE_DOMAIN_PATTERNS=()
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    SAFE_DOMAIN_PATTERNS+=("$line")
  done <"$SAFE_DOMAINS_FILE"
fi

PATTERN_LABELS=()
PATTERN_REGEXES=()
if [[ -n "$PATTERNS_FILE" ]]; then
  while IFS=$'\t' read -r label regex; do
    [[ -z "$label" || "$label" == \#* ]] && continue
    [[ -n "$regex" ]] || die "--patterns-file line for label '$label' has no REGEX field (expected LABEL<TAB>REGEX)"
    PATTERN_LABELS+=("$label")
    PATTERN_REGEXES+=("$regex")
  done <"$PATTERNS_FILE"
fi

OVERALL_STATUS=0

# ---- 1. tracked-tree scan ------------------------------------------------

HOME_FATAL_FILE="$WORKDIR/home_fatal.txt"
HOME_REVIEW_FILE="$WORKDIR/home_review.txt"
EMAIL_FATAL_FILE="$WORKDIR/email_fatal.txt"
EMAIL_REVIEW_FILE="$WORKDIR/email_review.txt"

HOME_FATAL_COUNT="$(grep_tree "$HOME_PATH_PATTERN" "$HOME_FATAL_FILE" . "${TEST_PATHSPECS[@]}")"
HOME_REVIEW_COUNT="$(grep_tree "$HOME_PATH_PATTERN" "$HOME_REVIEW_FILE" 'tests/*' '**/test_*.py' '**/*_test.py' '**/conftest.py')"
EMAIL_FATAL_COUNT="$(grep_tree "$EMAIL_PATTERN" "$EMAIL_FATAL_FILE" . "${TEST_PATHSPECS[@]}")"
EMAIL_REVIEW_COUNT="$(grep_tree "$EMAIL_PATTERN" "$EMAIL_REVIEW_FILE" 'tests/*' '**/test_*.py' '**/*_test.py' '**/conftest.py')"

[[ "$HOME_FATAL_COUNT" -eq 0 ]] || OVERALL_STATUS=1
[[ "$EMAIL_FATAL_COUNT" -eq 0 ]] || OVERALL_STATUS=1

# --patterns-file literal matches: fatal everywhere, no test-dir carve-out
# (an exact, operator-supplied literal has no ambiguity to adjudicate).
PATTERN_FATAL_COUNT=0
PATTERN_FINDINGS_FILE="$WORKDIR/pattern_findings.txt"
: >"$PATTERN_FINDINGS_FILE"
if [[ "${#PATTERN_LABELS[@]}" -gt 0 ]]; then
  for i in "${!PATTERN_LABELS[@]}"; do
    label="${PATTERN_LABELS[$i]}"
    regex="${PATTERN_REGEXES[$i]}"
    tmpfile="$WORKDIR/pattern_${i}.txt"
    count="$(grep_tree "$regex" "$tmpfile" .)"
    if [[ "$count" -gt 0 ]]; then
      PATTERN_FATAL_COUNT=$((PATTERN_FATAL_COUNT + count))
      {
        echo "-- pattern '$label' ($count hit(s)) --"
        redact_grep_output <"$tmpfile" | sed 's/^/FATAL: /'
      } >>"$PATTERN_FINDINGS_FILE"
      OVERALL_STATUS=1
    fi
  done
fi

# ---- 2. full-history blob scan (--history only) ---------------------------

HISTORY_STATUS="SKIPPED (pass --history to enable; trivially satisfied under a fresh-history publish with no unreachable objects)"
HISTORY_COUNT=0
HISTORY_FINDINGS_FILE="$WORKDIR/history_findings.txt"
: >"$HISTORY_FINDINGS_FILE"
if [[ "$HISTORY" -eq 1 ]]; then
  HISTORY_SCRIPT="$WORKDIR/history_scan.py"
  cat >"$HISTORY_SCRIPT" <<'PYEOF'
import re
import subprocess
import sys

repo, show_matches = sys.argv[1], sys.argv[2] == "1"
patterns = [("home_path", re.compile(rb"/home/[A-Za-z0-9._-]+"))]
patterns.append(("email", re.compile(
    rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)))
extra_file = sys.argv[3] if len(sys.argv) > 3 else ""
if extra_file:
    with open(extra_file, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            label, _, regex = line.rstrip("\n").partition("\t")
            if not regex:
                continue
            patterns.append((f"pattern:{label}", re.compile(regex.encode())))

check = subprocess.run(
    ["git", "-C", repo, "cat-file", "--batch-all-objects",
     "--batch-check=%(objectname) %(objecttype)"],
    capture_output=True, check=True,
)
blob_ids = []
for raw in check.stdout.splitlines():
    parts = raw.split()
    if len(parts) == 2 and parts[1] == b"blob":
        blob_ids.append(parts[0])

findings = {}
if blob_ids:
    proc = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    stdin_data = b"\n".join(blob_ids) + b"\n"
    out, _ = proc.communicate(stdin_data)
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        oid, _otype, size_s = out[pos:header_end].split()
        size = int(size_s)
        content_start = header_end + 1
        content = out[content_start:content_start + size]
        pos = content_start + size + 1
        for label, pat in patterns:
            m = pat.search(content)
            if m:
                findings.setdefault(label, []).append(
                    (oid.decode(), m.group(0).decode(errors="replace"))
                )

total = sum(len(v) for v in findings.values())
print(f"TOTAL={total}")
for label, hits in findings.items():
    for oid, matched in hits:
        if show_matches:
            print(f"{label}\t{oid}\t{matched}")
        else:
            print(f"{label}\t{oid}\t<redacted>")
PYEOF
  HISTORY_RAW="$WORKDIR/history_raw.txt"
  python3 "$HISTORY_SCRIPT" "$REPO" "$SHOW_MATCHES" "$PATTERNS_FILE" >"$HISTORY_RAW"
  HISTORY_COUNT="$(head -1 "$HISTORY_RAW" | sed 's/TOTAL=//')"
  tail -n +2 "$HISTORY_RAW" >"$HISTORY_FINDINGS_FILE"
  if [[ "$HISTORY_COUNT" -gt 0 ]]; then
    HISTORY_STATUS="RAN - FINDINGS($HISTORY_COUNT) in unreachable/reachable object scan"
    OVERALL_STATUS=1
  else
    HISTORY_STATUS="RAN - CLEAN (reachable + unreachable blobs)"
  fi
fi

# ---- 3. commit-metadata scan (A1) -----------------------------------------

METADATA_FILE="$WORKDIR/metadata_findings.txt"
: >"$METADATA_FILE"
METADATA_COUNT=0
while IFS=$'\t' read -r sha ae ce; do
  flagged=0
  is_safe_email "$ae" || flagged=1
  is_safe_email "$ce" || flagged=1
  if [[ "$flagged" -eq 1 ]]; then
    METADATA_COUNT=$((METADATA_COUNT + 1))
    short="${sha:0:12}"
    masked_ae="$(mask_email "$ae")"
    masked_ce="$(mask_email "$ce")"
    echo "$short author=$masked_ae committer=$masked_ce" >>"$METADATA_FILE"
  fi
done < <(git -C "$REPO" log "$TARGET_SHA" --format='%H%x09%ae%x09%ce')

if [[ "$FAIL_ON_METADATA" -eq 1 && "$METADATA_COUNT" -gt 0 ]]; then
  OVERALL_STATUS=1
fi

# ---- 4. secret backstop -----------------------------------------------------

TREE_DIR="$WORKDIR/tree"
mkdir -p "$TREE_DIR"
git -C "$REPO" archive --format=tar "$TARGET_SHA" | tar -x -C "$TREE_DIR"

SECRET_TREE_STATUS="SKIPPED (neither gitleaks nor uv/uvx is available - no secret backstop ran. This is reported, not silently passed.)"
if command -v gitleaks >/dev/null 2>&1; then
  GITLEAKS_REPORT="$WORKDIR/gitleaks-tree.json"
  set +e
  gitleaks detect --no-banner --no-git --source "$TREE_DIR" \
    --report-format json --report-path "$GITLEAKS_REPORT" >"$WORKDIR/gitleaks-tree.log" 2>&1
  gl_status=$?
  set -e
  if [[ "$gl_status" -eq 0 ]]; then
    SECRET_TREE_STATUS="RAN (gitleaks) - CLEAN"
  else
    SECRET_TREE_STATUS="RAN (gitleaks) - FINDINGS (see $GITLEAKS_REPORT)"
    OVERALL_STATUS=1
  fi
elif command -v uvx >/dev/null 2>&1; then
  DS_REPORT="$WORKDIR/detect-secrets-tree.json"
  set +e
  uvx detect-secrets scan "$TREE_DIR" >"$DS_REPORT" 2>"$WORKDIR/detect-secrets-tree.log"
  ds_status=$?
  set -e
  if [[ "$ds_status" -ne 0 ]]; then
    SECRET_TREE_STATUS="RAN (detect-secrets) - TOOL ERROR (exit $ds_status, see $WORKDIR/detect-secrets-tree.log)"
    OVERALL_STATUS=1
  else
    ds_count="$(python3 -c "import json,sys; d=json.load(open('$DS_REPORT')); print(sum(len(v) for v in d.get('results', {}).values()))" 2>/dev/null || echo "unknown")"
    if [[ "$ds_count" == "0" ]]; then
      SECRET_TREE_STATUS="RAN (detect-secrets) - CLEAN"
    else
      SECRET_TREE_STATUS="RAN (detect-secrets) - FINDINGS($ds_count) (see $DS_REPORT)"
      OVERALL_STATUS=1
    fi
  fi
fi

SECRET_HISTORY_STATUS="SKIPPED (pass --history and install gitleaks to enable; detect-secrets scans a filesystem snapshot, not git log, so it cannot provide history-mode coverage)"
if [[ "$HISTORY" -eq 1 ]]; then
  if command -v gitleaks >/dev/null 2>&1; then
    GITLEAKS_HIST_REPORT="$WORKDIR/gitleaks-history.json"
    set +e
    gitleaks detect --no-banner --source "$REPO" --log-opts="--all" \
      --report-format json --report-path "$GITLEAKS_HIST_REPORT" >"$WORKDIR/gitleaks-history.log" 2>&1
    glh_status=$?
    set -e
    if [[ "$glh_status" -eq 0 ]]; then
      SECRET_HISTORY_STATUS="RAN (gitleaks git --all) - CLEAN"
    else
      SECRET_HISTORY_STATUS="RAN (gitleaks git --all) - FINDINGS (see $GITLEAKS_HIST_REPORT)"
      OVERALL_STATUS=1
    fi
  else
    SECRET_HISTORY_STATUS="SKIPPED (--history was requested but gitleaks is not installed; detect-secrets cannot cover git history, so this check did not run - not silently passed)"
  fi
fi

# ---- 5. binary/asset review -------------------------------------------------

ASSET_FILE="$WORKDIR/assets.txt"
git -C "$REPO" ls-tree -r --name-only "$TARGET_SHA" | grep -iE "$IMAGE_EXT_PATTERN" >"$ASSET_FILE" || true
ASSET_COUNT="$(wc -l <"$ASSET_FILE" | tr -d ' ')"
if [[ "$FAIL_ON_ASSETS" -eq 1 && "$ASSET_COUNT" -gt 0 ]]; then
  OVERALL_STATUS=1
fi

# ---- report ------------------------------------------------------------------

OVERALL_LABEL="PASS"
[[ "$OVERALL_STATUS" -eq 0 ]] || OVERALL_LABEL="FAIL"
TIMESTAMP_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

echo "===== PRIVACY-AUDIT-ATTESTATION ====="
echo "target_sha: $TARGET_SHA"
echo "repo: $REPO"
echo "history_mode: $([[ "$HISTORY" -eq 1 ]] && echo enabled || echo "skipped (pass --history to enable)")"
echo
echo "tracked_tree_home_path: RAN - fatal_scope_findings=$HOME_FATAL_COUNT review_scope_findings(test dirs)=$HOME_REVIEW_COUNT"
if [[ "$HOME_FATAL_COUNT" -gt 0 ]]; then redact_grep_output <"$HOME_FATAL_FILE" | sed 's/^/  FATAL: /'; fi
if [[ "$HOME_REVIEW_COUNT" -gt 0 ]]; then redact_grep_output <"$HOME_REVIEW_FILE" | sed 's/^/  REVIEW (test fixture, not gating): /'; fi
echo "tracked_tree_email: RAN - fatal_scope_findings=$EMAIL_FATAL_COUNT review_scope_findings(test dirs)=$EMAIL_REVIEW_COUNT"
if [[ "$EMAIL_FATAL_COUNT" -gt 0 ]]; then redact_grep_output <"$EMAIL_FATAL_FILE" | sed 's/^/  FATAL: /'; fi
if [[ "$EMAIL_REVIEW_COUNT" -gt 0 ]]; then redact_grep_output <"$EMAIL_REVIEW_FILE" | sed 's/^/  REVIEW (test fixture, not gating): /'; fi
if [[ "${#PATTERN_LABELS[@]}" -gt 0 ]]; then
  echo "tracked_tree_patterns: RAN (${#PATTERN_LABELS[@]} pattern(s) from --patterns-file) - findings=$PATTERN_FATAL_COUNT"
  [[ "$PATTERN_FATAL_COUNT" -gt 0 ]] && sed 's/^/  /' <"$PATTERN_FINDINGS_FILE"
else
  echo "tracked_tree_patterns: SKIPPED (no --patterns-file supplied - exact board-identity literals were NOT checked for)"
fi
echo
echo "history_blob_scan: $HISTORY_STATUS"
if [[ "$HISTORY_COUNT" -gt 0 ]]; then sed 's/^/  /' <"$HISTORY_FINDINGS_FILE"; fi
echo
echo "commit_metadata_a1: RAN - count=$METADATA_COUNT (gates exit only with --fail-on-metadata, currently $([[ "$FAIL_ON_METADATA" -eq 1 ]] && echo enabled || echo disabled))"
echo "  NOTE: this is the one check a tracked-tree grep and every secret scanner (gitleaks/detect-secrets) miss - a scanner-clean result says nothing about this count."
if [[ "$METADATA_COUNT" -gt 0 ]]; then sed 's/^/  /' <"$METADATA_FILE"; fi
echo
echo "secret_backstop_tree: $SECRET_TREE_STATUS"
echo "secret_backstop_history: $SECRET_HISTORY_STATUS"
echo
echo "asset_review: RAN - $ASSET_COUNT image asset(s) flagged for human eyeball review (not OCR'd; gates exit only with --fail-on-assets, currently $([[ "$FAIL_ON_ASSETS" -eq 1 ]] && echo enabled || echo disabled))"
if [[ "$ASSET_COUNT" -gt 0 ]]; then sed 's/^/  /' <"$ASSET_FILE"; fi
echo
echo "timestamp_utc: $TIMESTAMP_UTC"
echo "overall: $OVERALL_LABEL"
echo "===== END ATTESTATION ====="

exit "$OVERALL_STATUS"
