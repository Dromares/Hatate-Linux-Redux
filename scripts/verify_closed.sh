#!/usr/bin/env bash
#
# Standing presence/absence sweep for recently-closed tickets.
#
# For each given ticket id, checks whether any commit reachable from
# <remote>/<branch> mentions it. Flags any closed ticket with zero
# matching commits so a human can check it by hand. Born from catching
# DAN-175 and DAN-177 both marked `done` with their code not on `main` -
# a `git log origin/main --grep=<id>` sweep caught both in one pass, and
# that check should not depend on someone remembering to run it by hand.
#
# SCOPE CAVEAT - read before trusting a clean run:
#   This is a PRESENCE/ABSENCE check, not a correctness check. It answers
#   only "does some commit on <remote>/<branch> mention this ticket id in
#   its message?" It would NOT have caught DAN-152's false pass, where
#   the code was on `main` but a fourth acceptance criterion was unmet -
#   a `--grep` hit proves a commit mentions the id, not that the ticket's
#   full scope landed or is correct.
#
#   This repo's history also mixes true merges and squashes, and a squash
#   merge's title is not required to carry the ticket id at all (e.g. the
#   agent/virgil/DAN-128-... branch merged without one). So:
#     - A match is weak evidence of presence, not proof that branch's
#       full content landed intact.
#     - No match is weak evidence of absence, not proof the ticket's work
#       is missing - it may just be a squash that dropped the id, or a
#       ticket with no associated code (governance/process tickets).
#   Treat every line below as a pointer to check by hand, not a verdict.
#
#   Matching looks only at each commit's SUBJECT line, never its full
#   body. Commit bodies routinely prose-mention other ticket ids (this
#   file's own DAN-187 merge commit discusses DAN-152 as a worked
#   example above) - a full-message grep would let such a mention in a
#   newer, unrelated commit permanently shadow the real, older commit
#   for that id, since matches are reported newest-first.
#
# DO NOT substitute a bare `git merge-base --is-ancestor <sha-or-branch>
# <remote>/<branch>` for this script as a merge check (DAN-217). That
# command is immune to squash merges in the wrong direction: a squash
# replays a branch's content as a brand-new commit with different
# parents, so the original branch tip is a permanent non-ancestor of
# `main` even after it ships - `--is-ancestor` reported DAN-192 "not
# merged" via that exact command the same day it was squash-merged as
# `8c32ff6`. This script survives squashes because it greps the target
# ref's own commit log instead of testing ancestry against a pre-squash
# SHA. If you need a direct branch-vs-ref check instead of a ticket-id
# grep (e.g. "did this exact branch's content land, under any SHA"), use
# `git cherry <ref> <branch>` - patch-equivalence, not ancestry - which
# has the same squash immunity for that narrower question.
#
# That same bare command has a second, sharper trap once the PR's
# "delete branch" option has run (the repo's default): the feature
# branch ref is gone, so `--is-ancestor <branch-or-sha> <remote>/<branch>`
# doesn't cleanly answer "false" - it hard-fails with `fatal: Not a valid
# object name` (or `Not a valid commit name` for a bare SHA no longer
# fetchable) and a non-zero exit that is indistinguishable, by exit code
# alone, from the "not merged" case. Reproduced on DAN-193's branch
# (`agent/beatrice/DAN-193-rule34us-hydrus-checks`, squash-merged as
# `57f679c` via PR #69, branch deleted on merge): `git merge-base
# --is-ancestor origin/agent/beatrice/DAN-193-rule34us-hydrus-checks
# origin/main` exits 128 with `fatal: Not a valid object name`, not the
# exit-1 "not an ancestor" a caller might expect to branch on. This
# script never resolves the feature-branch ref at all - it only reads
# `$REMOTE/$BRANCH`'s own log - so a deleted branch ref cannot affect it
# either way.
#
# MERGED-PR EVIDENCE FALLBACK (DAN-1457): merge/verify/refresh child tickets
# usually carry no commit of their own - the work shipped under the PARENT's
# id - so the subject grep reports a false NONE FOUND (103 of them in the
# DAN-1442 sweep, each hand-checked). When the grep misses, the script reads
# the ticket's own description and comments and counts it as PASS only if one
# line of it names BOTH a PR number and a commit SHA and the target ref agrees:
#   - the ref has a commit for that PR: a squash subject ending "(#N)" or a
#     "Merge pull request #N from ..." merge commit (call it M), and
#   - the cited SHA is M itself or one of the commits M brought in
#     (`git rev-list M^1..M`), which also means it is an ancestor of the ref.
# This is deliberately not a bare `--is-ancestor <sha> <ref>`: any old SHA is
# an ancestor of main, so a ticket that cites an unrelated base commit would
# false-PASS. Anchoring the SHA to the PR's own commit keeps false-done at 0.
# A squash-merged PR whose evidence cites only the pre-squash branch tip, or a
# ticket with no PR+SHA in its text, still reads NONE FOUND (check by hand).
# Evidence comes from the Paperclip API when PAPERCLIP_API_URL/API_KEY are set,
# or from $VERIFY_CLOSED_EVIDENCE_DIR/<TICKET>.txt when that is set (tests,
# offline use). With neither, the fallback is skipped.
#
# Usage:
#   scripts/verify_closed.sh TICKET_ID [TICKET_ID...]
#   scripts/verify_closed.sh --last N
#   scripts/verify_closed.sh --help
#
# Options:
#   -n, --last N      Auto-pull the N most recently-closed ("done") ticket
#                      identifiers from the Paperclip API instead of taking
#                      them as arguments. Requires PAPERCLIP_API_URL,
#                      PAPERCLIP_API_KEY, and PAPERCLIP_COMPANY_ID in the
#                      environment (set automatically inside a Paperclip
#                      run). Not available outside one.
#   -r, --remote NAME Git remote to check against (default: origin).
#   -b, --branch NAME Branch to check against (default: main).
#   -h, --help         Show this help.
#
# Output: one line per ticket -
#   TICKET  PASS  <sha>  <subject>
#   TICKET  PASS (merged PR #N, evidence sha <sha>)  <sha>  <subject>
#   TICKET  NONE FOUND (may be a squash without the id - check by hand)
#
# Exit status: 0 if every ticket has at least one matching commit, 1 if
# any ticket has none. Nonzero means "go check these by hand," not
# "these are confirmed missing."

set -euo pipefail

print_help() {
  sed -n '3,109p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

REMOTE="origin"
BRANCH="main"
LAST_N=""
TICKETS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_help
      exit 0
      ;;
    -n|--last)
      [[ $# -ge 2 ]] || { echo "ERROR: --last requires a value." >&2; exit 2; }
      LAST_N="$2"
      shift 2
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
    --)
      shift
      TICKETS+=("$@")
      break
      ;;
    -*)
      echo "ERROR: unknown option: $1 (see --help)" >&2
      exit 2
      ;;
    *)
      TICKETS+=("$1")
      shift
      ;;
  esac
done

if [[ -n "$LAST_N" && ${#TICKETS[@]} -gt 0 ]]; then
  echo "ERROR: pass either --last N or explicit ticket ids, not both." >&2
  exit 2
fi

if [[ -n "$LAST_N" ]]; then
  [[ "$LAST_N" =~ ^[0-9]+$ && "$LAST_N" -gt 0 ]] || {
    echo "ERROR: --last requires a positive integer, got: $LAST_N" >&2
    exit 2
  }
  : "${PAPERCLIP_API_URL:?--last requires PAPERCLIP_API_URL in the environment (run inside Paperclip, or pass ticket ids directly)}"
  : "${PAPERCLIP_API_KEY:?--last requires PAPERCLIP_API_KEY in the environment (run inside Paperclip, or pass ticket ids directly)}"
  : "${PAPERCLIP_COMPANY_ID:?--last requires PAPERCLIP_COMPANY_ID in the environment (run inside Paperclip, or pass ticket ids directly)}"

  api_base="${PAPERCLIP_API_URL%/}"
  api_base="${api_base%/api}"

  mapfile -t TICKETS < <(
    curl -sf -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
      "$api_base/api/companies/$PAPERCLIP_COMPANY_ID/issues?status=done" \
    | python3 -c '
import json, sys
issues = json.load(sys.stdin)
issues.sort(key=lambda i: i.get("completedAt") or "", reverse=True)
for issue in issues[: int(sys.argv[1])]:
    ident = issue.get("identifier")
    if ident:
        print(ident)
' "$LAST_N"
  )
fi

if [[ ${#TICKETS[@]} -eq 0 ]]; then
  echo "ERROR: no ticket ids given and --last not used." >&2
  print_help
  exit 2
fi

cd "$(git rev-parse --show-toplevel)"

if ! git fetch "$REMOTE" "$BRANCH" --quiet 2>/dev/null; then
  echo "WARNING: could not fetch $REMOTE/$BRANCH; checking the local ref as last known." >&2
fi

ref="$REMOTE/$BRANCH"
git rev-parse --verify --quiet "$ref" >/dev/null || {
  echo "ERROR: ref $ref not found (bad --remote/--branch, or never fetched)." >&2
  exit 2
}

# Print "<pr> <merge-sha> <cited-sha> <subject>" if TICKET's own text names a
# PR and a SHA that agree with $ref (see MERGED-PR EVIDENCE FALLBACK), else
# nothing. Never fails the script: any lookup problem means "no evidence".
merged_pr_evidence() {
  local id="$1" text=""
  if [[ -n "${VERIFY_CLOSED_EVIDENCE_DIR:-}" ]]; then
    [[ -f "$VERIFY_CLOSED_EVIDENCE_DIR/$id.txt" ]] && text="$(cat "$VERIFY_CLOSED_EVIDENCE_DIR/$id.txt")"
  elif [[ -n "${PAPERCLIP_API_URL:-}" && -n "${PAPERCLIP_API_KEY:-}" ]]; then
    local base="${PAPERCLIP_API_URL%/}"
    base="${base%/api}"
    text="$(
      for path in "" "/comments"; do
        curl -sf -H "Authorization: Bearer $PAPERCLIP_API_KEY" "$base/api/issues/$id$path" || true
      done | python3 -I -c '
import json, sys
dec = json.JSONDecoder(strict=False)
raw, pos = sys.stdin.read(), 0
while pos < len(raw):
    while pos < len(raw) and raw[pos].isspace():
        pos += 1
    if pos >= len(raw):
        break
    obj, pos = dec.raw_decode(raw, pos)
    for item in obj if isinstance(obj, list) else [obj]:
        print(item.get("description") or item.get("body") or "")
' 2>/dev/null || true
    )"
  fi
  [[ -n "$text" ]] || return 0
  printf '%s' "$text" | python3 -I -c '
import re, subprocess, sys
ref = sys.argv[1]
text = sys.stdin.read()

def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)

# PR number and SHA must sit on the same line, so a stray "#2" in one
# paragraph and an unrelated hash in another cannot pair up.
pairs = []
for line in text.splitlines():
    line_prs = set(re.findall(r"(?:#|pull/)(\d+)\b", line))
    line_shas = set(re.findall(r"\b[0-9a-f]{7,40}\b", line))
    if line_prs and line_shas:
        pairs.append((line_prs, line_shas))
if not pairs:
    sys.exit(0)
prs = sorted(set().union(*(p for p, _ in pairs)), key=int)
shas = sorted(set().union(*(s for _, s in pairs)))
resolved = {}
for sha in shas:
    full = git("rev-parse", "--verify", "--quiet", sha + "^{commit}").stdout.strip()
    if full:
        resolved[sha] = full
if not resolved:
    sys.exit(0)
log = git("log", ref, "--format=%H %s").stdout.splitlines()
for pr in prs:
    pat = re.compile(r"\(#%s\)$|^Merge pull request #%s from " % (pr, pr))
    for line in log:
        m, _, subject = line.partition(" ")
        if not pat.search(subject):
            continue
        brought = set(git("rev-list", m + "^1.." + m).stdout.split()) | {m}
        for sha, full in resolved.items():
            paired = any(pr in p and sha in s for p, s in pairs)
            if paired and full in brought and git("merge-base", "--is-ancestor", full, ref).returncode == 0:
                print(pr, m[:7], sha, subject)
                sys.exit(0)
' "$ref" 2>/dev/null || true
}

exit_status=0
for id in "${TICKETS[@]}"; do
  match="$(git log "$ref" --format='%h %s' | grep -E "\\b${id}\\b" | head -n1 || true)"
  if [[ -n "$match" ]]; then
    sha="${match%% *}"
    subject="${match#* }"
    printf '%-14s PASS  %s  %s\n' "$id" "$sha" "$subject"
  elif evidence="$(merged_pr_evidence "$id")" && [[ -n "$evidence" ]]; then
    read -r pr msha esha subject <<<"$evidence"
    printf '%-14s PASS (merged PR #%s, evidence sha %s)  %s  %s\n' "$id" "$pr" "$esha" "$msha" "$subject"
  else
    printf '%-14s NONE FOUND (may be a squash without the id - check by hand)\n' "$id"
    exit_status=1
  fi
done

exit "$exit_status"
