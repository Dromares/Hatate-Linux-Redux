#!/usr/bin/env bash
#
# Single-shot liveness probe for the per-run managed GitHub identity.
#
# Calls the exact broker endpoint the launcher git shim
# ($PAPERCLIP_GITHUB_LAUNCHER_DIR/git) calls before every push/fetch, and
# classifies the HTTP result directly instead of relying on indirect
# signals. Before DAN-549, this script took `--mcp-status ok|fail` as an
# INPUT because it had no structured signal of its own - every failure
# surfaced as the same opaque `github_identity_unavailable` / "No managed
# GitHub identity is available for this run" string, whether the cause
# was a dead broker, a rejected capability, no provisioned identity, or
# another run holding the lease. The broker's own credentials endpoint
# already distinguishes all four of those; this script just calls it
# properly and reads the answer. See docs/github-identity-flakes.md for
# the fuller writeup (DAN-93, DAN-101, DAN-174, DAN-259, DAN-265, DAN-549).
#
# HEADER CONTRACT - get this wrong and every path 401s, including ones
# that have nothing to do with auth (verified live on DAN-549, nearly
# misread as a signing-secret mismatch before realizing the request was
# just malformed):
#
#   authorization: Bearer $PAPERCLIP_API_KEY                      <- this run's own API key
#   x-paperclip-github-capability: $PAPERCLIP_GITHUB_BROKER_TOKEN  <- the broker capability token
#
# These two are NOT interchangeable. Putting the broker token in
# `authorization` (or vice versa) produces a uniform
# `401 {"error":"Agent token did not verify; obtain fresh credentials and
# retry"}` on every path, including nonexistent ones, because auth runs
# before routing. Never print either value - both are secrets. This
# script only ever echoes status/reason/source/HTTP code.
#
# MCP is a separate transport (see "Two independent transports" in the
# runbook) with its own, independently-provisioned credential, and it has
# been observed to keep working through a CLI-transport outage. This
# script cannot probe it (minting/health-checking that credential needs
# board-level auth an agent run's token does not carry), so it is not
# part of this script's verdict. If this script reports BROKER-DOWN or
# CAPABILITY-REJECTED, a corroborating read-only MCP call (e.g. `get-me`)
# made by hand tells you whether that is CLI-transport-specific or wider -
# but it is corroboration, not an input this script consumes.
#
# Usage:
#   scripts/check_github_identity.sh [-h|--help]
#
# Exit codes (five real outcomes, plus one honest "can't tell"):
#   0  HEALTHY             - HTTP 200, status: "available". Push/fetch
#                            will work; the launcher shim's env carries
#                            the token.
#   1  NO-IDENTITY         - HTTP 200, status: "unavailable" (or any
#                            non-"available" value). No managed identity
#                            was vended for this run. NOT agent-fixable -
#                            see the printed board action.
#   2  LEASE-BUSY          - HTTP 409. Another run holds this identity's
#                            lease right now. The only outcome actually
#                            worth retrying - the launcher shim itself
#                            retries 30x at 1s.
#   3  CAPABILITY-REJECTED - HTTP 401/403. This run's own capability was
#                            refused. Check both headers above before
#                            believing it's a platform defect - a swapped
#                            header produces exactly this.
#   4  BROKER-DOWN         - connection refused, timed out, or no HTTP
#                            response at all. Distinct from all of the
#                            above: the broker process itself did not
#                            answer, as opposed to answering with a
#                            rejection or an unavailable identity.
#   5  INCONCLUSIVE        - usage error, required env vars are missing,
#                            or the broker answered with an HTTP status
#                            this script doesn't recognize. Says what's
#                            missing; never guesses a verdict.
set -euo pipefail

print_help() {
  sed -n '3,70p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      print_help
      exit 0
      ;;
    *)
      echo "ERROR: unexpected argument: $1 (see --help)" >&2
      exit 5
      ;;
  esac
done

# Checked explicitly (rather than `: "${VAR:?msg}"`) so a missing env var
# is INCONCLUSIVE (5), matching the exit-code table above, instead of
# bash's own parameter-expansion failure, which exits 1 - indistinguishable
# from NO-IDENTITY on this script's own documented contract.
missing=()
[ -n "${PAPERCLIP_GITHUB_BROKER_URL:-}" ] || missing+=("PAPERCLIP_GITHUB_BROKER_URL")
[ -n "${PAPERCLIP_API_KEY:-}" ] || missing+=("PAPERCLIP_API_KEY")
[ -n "${PAPERCLIP_GITHUB_BROKER_TOKEN:-}" ] || missing+=("PAPERCLIP_GITHUB_BROKER_TOKEN")
if [ "${#missing[@]}" -gt 0 ]; then
  echo "INCONCLUSIVE: required env var(s) not set: ${missing[*]}." >&2
  echo "This script only makes sense inside a Paperclip agent run." >&2
  exit 5
fi

# --- the probe: the same call the launcher git shim makes ---------------
# Never echo PAPERCLIP_API_KEY or PAPERCLIP_GITHUB_BROKER_TOKEN - both are
# secrets. Only status/reason/source/http-code (below) are printed.

probe_curl_rc=0
probe_response="$(
  curl -s -w '\n%{http_code}' --max-time 15 -X POST \
    -H "authorization: Bearer $PAPERCLIP_API_KEY" \
    -H "x-paperclip-github-capability: $PAPERCLIP_GITHUB_BROKER_TOKEN" \
    -H 'content-type: application/json' \
    -d '{}' \
    "${PAPERCLIP_GITHUB_BROKER_URL%/}/runtime-tools/github/credentials" \
    2>/dev/null
)" || probe_curl_rc=$?

http_status="$(tail -n1 <<<"$probe_response")"
probe_body="$(sed '$d' <<<"$probe_response")"

if [ "$probe_curl_rc" -ne 0 ] || [ -z "$http_status" ] || [ "$http_status" = "000" ]; then
  echo "BROKER-DOWN: could not reach $PAPERCLIP_GITHUB_BROKER_URL at all" \
       "(curl exit $probe_curl_rc, http ${http_status:-none})."
  echo "This is distinct from a rejected capability or an unavailable" \
       "identity - the broker process itself did not answer. Treat as a" \
       "platform-availability problem, not an identity-vending question," \
       "and escalate it as one (to the CEO, not the board - there is no" \
       "connection/grant to provision here)."
  echo "Corroboration you can add by hand: a read-only GitHub MCP call" \
       "(e.g. \`get-me\`) uses a separate, independently-provisioned" \
       "credential and has been seen to keep working through a CLI-" \
       "transport outage. If it succeeds, this is CLI-transport-specific," \
       "not a full outage - say so when you escalate."
  exit 4
fi

status_value=""
source_value=""
reason_value=""
if [ -n "$probe_body" ]; then
  # Flat, known response shape - parsed with python3 (already a hard
  # dependency of this project) rather than adding jq. Any field this
  # can't find just comes back empty; printed as "(unknown)" below.
  status_value="$(python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
print(data.get("status") or "")
' "$probe_body" 2>/dev/null || true)"
  source_value="$(python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
print(data.get("source") or "")
' "$probe_body" 2>/dev/null || true)"
  reason_value="$(python3 -c '
import json, sys
try:
    data = json.loads(sys.argv[1])
except ValueError:
    sys.exit(0)
print(data.get("reason") or "")
' "$probe_body" 2>/dev/null || true)"
fi

echo "Broker credential probe: http_status=$http_status status=${status_value:-(unknown)} source=${source_value:-(unknown)} reason=${reason_value:-(none)}"
echo

case "$http_status" in
  401|403)
    echo "CAPABILITY-REJECTED: the broker refused this run's own" \
         "capability (http $http_status)."
    echo "Before treating this as a platform defect, re-check the two" \
         "headers above are in the right slots - 'authorization' carries" \
         "PAPERCLIP_API_KEY, 'x-paperclip-github-capability' carries" \
         "PAPERCLIP_GITHUB_BROKER_TOKEN. Swapping them produces exactly" \
         "this uniform rejection on every path, including nonexistent" \
         "ones, because auth runs before routing (DAN-549). If both" \
         "headers are confirmed correct and this persists, escalate as a" \
         "platform availability/capability-provisioning problem."
    exit 3
    ;;
  409)
    echo "LEASE-BUSY: another run currently holds this identity's lease" \
         "(http 409)."
    echo "This is the one outcome that is actually worth retrying - it is" \
         "not an outage and not a missing identity, just contention. The" \
         "launcher git shim already retries 30x at 1s; re-running this" \
         "probe shortly is reasonable too. Do not escalate on this alone."
    exit 2
    ;;
  200)
    if [ "$status_value" = "available" ]; then
      echo "HEALTHY: this run's own GitHub identity vended successfully."
      echo "Push/fetch through git, or the GitHub MCP tools, should work;" \
           "the launcher shim's env carries the token names it needs."
      exit 0
    fi
    echo "NO-IDENTITY: broker responded (http 200) but status=" \
         "'${status_value:-(unknown)}', not 'available' - no managed" \
         "GitHub identity was vended for this run."
    echo "This is NOT agent-fixable. Do not propose a connection/grant" \
         "change (see docs/github-identity-flakes.md fact 1: org-scope" \
         "installs are invisible to the resolver and provably cannot" \
         "help)."
    if [ "$source_value" = "personal" ]; then
      echo "source=personal here: no dedicated agent/org-kind grant was" \
           "found for this identity context, so the resolver fell back to" \
           "its personal-tier default label (fact 2). Board action:" \
           "provision or reconnect a managed GitHub identity for this" \
           "agent/company - an agent cannot do this itself. Escalate to" \
           "the board with this output; do not retry blindly."
    else
      echo "Board action: provision a managed GitHub identity for this" \
           "run's identity context - an agent cannot do this itself."
    fi
    exit 1
    ;;
  *)
    echo "INCONCLUSIVE: broker answered with an http status this script" \
         "doesn't recognize ($http_status), not one of 200/401/403/409."
    echo "Do not guess a verdict from this; re-run, and if it repeats," \
         "escalate the specific status code rather than a generic flake."
    exit 5
    ;;
esac
