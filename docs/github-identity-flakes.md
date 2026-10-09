# GitHub identity flakes: how to tell "just me" from "everyone"

Four tickets — [DAN-93](https://github.com/Dromares/Hatate-Linux-Redux),
[DAN-101](https://github.com/Dromares/Hatate-Linux-Redux),
[DAN-174](https://github.com/Dromares/Hatate-Linux-Redux), and
[DAN-259](https://github.com/Dromares/Hatate-Linux-Redux) — have all been
opened for the same underlying fault: the per-run managed GitHub identity
Paperclip vends to an agent run fails to vend intermittently. Three of the
four occurrences were misdiagnosed as a missing or wrong-scoped credential
and escalated to the board. Two asked the board for a fix that provably
cannot work. **The credentials are not the defect. Diagnosability is.**
[DAN-265](https://github.com/Dromares/Hatate-Linux-Redux) built the tool and
wrote this page so occurrence #5 costs one command instead of a board
round-trip.

**[DAN-549](https://github.com/Dromares/Hatate-Linux-Redux) replaced the
script's original approach.** Through DAN-265, the script had no structured
signal of its own — every failure surfaced as the same opaque
`github_identity_unavailable` string, so the script inferred a verdict
indirectly (a `FETCH_HEAD` positive control, plus a human-supplied
`--mcp-status` flag). DAN-549 found that the broker's own credentials
endpoint, called correctly (see "Header contract" below), already returns a
structured verdict that distinguishes a dead broker, a rejected capability,
no provisioned identity, and another run holding the lease — the four
things the opaque string used to conflate. The script now calls that
endpoint directly and classifies the HTTP result; it no longer needs the
positive control or an `--mcp-status` input to reach a verdict. Both historical
mechanisms are kept below for context (and because MCP corroboration is
still useful by hand in the two outcomes where it helps), but they are no
longer part of how the script decides.

**The CLI (`git`/`gh`) transport is not the only path to GitHub.** It is
provisioned independently of the GitHub MCP tools and has been observed to
fail (and recover) on its own schedule — see "Two independent transports"
below.

Run this the moment `git fetch`/`push`, `gh`, or a GitHub MCP tool call fails
with anything that smells like an auth problem:

```bash
scripts/check_github_identity.sh
```

Read its output and its exit code (below) before concluding anything, filing
a ticket, or asking the board for a scope/connection change. The rest of this
page is the reasoning the script encodes, for when you need the full case —
or when the script itself needs to change.

## Header contract (DAN-549)

The script's probe is a `POST` to
`${PAPERCLIP_GITHUB_BROKER_URL%/}/runtime-tools/github/credentials` with
`-d '{}'` and two headers that are **not interchangeable**:

| Header | Value |
|---|---|
| `authorization` | `Bearer $PAPERCLIP_API_KEY` — this run's own API key |
| `x-paperclip-github-capability` | `$PAPERCLIP_GITHUB_BROKER_TOKEN` — the broker capability token |

Putting the broker token in `authorization` (or the API key in the
capability header) produces a uniform
`401 {"error":"Agent token did not verify; obtain fresh credentials and
retry"}` on **every** path, including nonexistent ones, because auth runs
before routing. DAN-549 nearly wrote that up as a signing-secret mismatch
before realizing the request itself was malformed — the same
validation-before-authorization shape documented elsewhere for this
company's own API. If you ever see a uniform 401 across unrelated paths,
suspect the request before the platform.

Never print either header value — both are secrets. The script (and this
page) only ever surface `status`/`reason`/`source`/the HTTP status code.

## The four facts that keep getting relitigated

1. **"Install it at org scope" is a no-op; never recommend it.**
   `resolveManagedGitHubCredential` selects connection grants with
   `or(kind == "agent", kind == "user")`. The board's add-installation
   endpoint hard-codes `kind: "organization"` when provisioning a new
   connection. An organization-kind grant is **invisible** to the resolver —
   it will never be selected, no matter how it's installed or re-installed.
   DAN-93 asked the board to do exactly this, then retracted the request
   after reading the resolver source. That retraction is the single most
   valuable artifact in this whole saga; don't make the board redo the work
   it already proved pointless.

2. **`source: "personal"` is not a rejection and carries no scope
   information.** The credentials endpoint's `identitySource` field defaults
   to `"personal"` whenever no *dedicated* (agent-kind) grant exists —
   including when the resolver found no candidate grant at all. Three
   tickets read `"personal"` as "the broker is refusing a personal-scope
   credential" and built a scope-defect diagnosis on top of it. It is a
   default label for "nothing dedicated," not a verdict about what was
   tried or refused. Treat it as noise for diagnosis purposes; the script
   prints it anyway, next to an explicit disclaimer, so nobody re-derives
   this the hard way.

   **Nuance added by DAN-549:** this does not mean `source: "personal"` is
   never worth acting on — it means it is never evidence of a *scope
   defect* on its own. Paired with `status: "unavailable"` (i.e. no
   dedicated grant was found for this identity context at all), it is a
   legitimate board action: provision or reconnect a managed identity.
   What's still wrong is reading the bare string as "the broker rejected a
   personal credential" and proposing a connection/grant change in
   response — that reading has been wrong three times. Reading it as "no
   dedicated grant exists; someone with provisioning access needs to make
   one" is what DAN-549 confirmed live, and it's what the script's
   `NO-IDENTITY` output now says.

3. **`connections_search` reporting `state: "ready"` describes
   *installation*, not *vending*.** Whether a GitHub connection is
   installed for this company/agent is a different question from whether
   *this specific run* was handed a usable, authenticated identity for it.
   The two disagree routinely — `ready` plus a failing credentials probe is
   the normal shape of this flake, not evidence of a second bug.

4. **A negative result is not a company-wide outage until you've run the
   positive control.** The decisive test is always: *while my run fails, is
   any other run succeeding?* Only a demonstrated "nobody is succeeding"
   justifies an infrastructure escalation. DAN-174 established this
   requirement; DAN-259 is the ticket that skipped it and nearly became a
   fifth confirmed-wrong board escalation before the CEO ran the control
   personally and reversed it.

## The positive control, and a correction to how it was described (historical)

**Superseded by DAN-549.** This section describes the heuristic the script
used *before* it had a structured verdict to call — a `FETCH_HEAD`-watching
positive control standing in for a real answer to "is my identity broken,
or is everyone's?" The script no longer runs this; the broker's credentials
endpoint answers that question directly and precisely (`LEASE-BUSY` vs
`NO-IDENTITY` vs `BROKER-DOWN` vs `CAPABILITY-REJECTED`, see the exit-code
table below). Kept here for institutional memory and because the same
"did the file change vs does it hold fresh content" distinction could be
useful again for some *other* shared-state flake this repo hits later.

The mechanism: the shared checkout's `.git/FETCH_HEAD` is written by
`scripts/agent-preflight.sh`'s `git fetch origin main` on every single agent
heartbeat that reaches it, regardless of which isolated worktree that run
later moves its real work into (every run's *preflight* fetch happens in the
shared checkout, before isolation). So the shared checkout's `FETCH_HEAD` is
a shared, continuously-refreshed record of *other* runs' fetch attempts
against the same broker you're failing against right now.

**Correction (DAN-265):** DAN-259's closing comment claimed "a failed fetch
does not write FETCH_HEAD," and this ticket's own description inherited that
claim as settled fact ("DAN-259 verified that"). It is **not quite right**,
and the imprecision matters enough to fix here rather than let it stand
uncorrected in a file people are told to trust blindly:

Direct experiment against this environment's actual git (2.56.0), across
three independent failure modes — a bad remote path, an unreachable host,
and the exact `fatal: could not read Username for 'https://github.com':
terminal prompts disabled` error this flake produces — all showed the same
thing: **a failed `git fetch` also rewrites `FETCH_HEAD`.** It truncates any
existing content to **empty** (0 bytes) and updates the mtime. It does not
leave a previously-populated `FETCH_HEAD` untouched.

What *is* true, and is the part worth keeping: **only a successful fetch
writes non-empty content** — a real object id followed by `branch '<name>'
of <url>`. A failed fetch never produces that. So the valid signal was never
"did the file change" (every failing run's preflight changes it too, and
would make this probe claim "just me" even during a genuine company-wide
outage — the exact false-negative-on-escalation failure mode this whole
saga is trying to eliminate). The valid signal is **"does it now hold fresh,
non-empty content."** `scripts/check_github_identity.sh` implements the
corrected version; this correction has also been posted on DAN-259 directly
since that ticket's text is what the false version would otherwise keep
propagating from.

If you ever see a different git version behave differently here (e.g.
leaving stale content untouched on failure instead of truncating it), that's
a reason to re-verify this page and the script together, not to trust either
one over fresh evidence from your own run.

## Two independent transports (found live, during this ticket)

Everything in the sections above — the broker probe, `source`, `identitySource`,
`connections_search`, and the `FETCH_HEAD` positive control — only ever
observes the **CLI/git transport**: `git fetch`/`push` and `gh`, all routed
through `$PAPERCLIP_GITHUB_BROKER_URL` via the wrapper scripts in
`$PAPERCLIP_GITHUB_LAUNCHER_DIR`. The **GitHub MCP tools** (`get-me`,
`create-branch`, `push-files`, `create-pull-request`, ...) are a **separate
transport** with its own, independently-provisioned credential.

This is not theoretical. During this very ticket's implementation, the CLI
transport failed for roughly 45 minutes straight — every `git push` attempt,
across two separate runs and a bounded 8-attempt retry loop, failed with
`No managed GitHub identity is available for this run` — while the CEO used
the **MCP transport** against this same repo in that window without incident
(reading PR state, check-run conclusions, and mergeability). A diagnostic
that concludes "everyone is down" from the CLI signal alone, while a working
MCP path exists the whole time, manufactures exactly the board round-trip
this ticket exists to prevent.

**Why the script can't just probe the MCP transport itself and be done with
it:** minting or health-checking that connection's credential
(`/api/agents/me/connections/{id}/token`,
`/api/tool-connections/{id}/health-check`) requires board-level auth
(`BoardSessionAuth`/`BoardApiKeyAuth` in the live OpenAPI spec) that an agent
run's `RuntimeToolsBearerAuth` token does not carry. Only the agent itself,
making an actual MCP tool call, can observe that transport's health.

**Since DAN-549, this is corroboration you add by hand, not an input the
script consumes.** Before DAN-549, the script accepted `--mcp-status
ok|fail` because the CLI-side signal alone couldn't tell `EVERY-RUN` (real
outage) apart from `CLI-DOWN-MCP-UP` (just this transport). The broker's
structured response now tells `BROKER-DOWN` and `CAPABILITY-REJECTED` apart
from `NO-IDENTITY` and `LEASE-BUSY` directly, so the script no longer needs
that external input to reach a verdict. If the verdict is `BROKER-DOWN` or
`CAPABILITY-REJECTED` and you want to know whether it's CLI-transport-
specific, make one read-only MCP call yourself (e.g. `get-me`) and mention
the result when you escalate — it changes how you phrase the escalation,
not whether the script will give you a verdict.

## Reading `scripts/check_github_identity.sh`'s exit codes (DAN-549)

| Exit | Meaning | What to do |
|---|---|---|
| `0` | **HEALTHY** — HTTP 200, `status: "available"`. | Nothing. Proceed normally; push/fetch and the GitHub MCP tools should both work. |
| `1` | **NO-IDENTITY** — HTTP 200, `status` is not `"available"` (e.g. `"unavailable"`). No managed identity was vended for this run. | **Not agent-fixable.** Do not propose a connection/grant change (fact 1). Escalate to the **board** to provision/reconnect a managed identity — when `source: "personal"`, that's exactly what it means (fact 2's nuance). |
| `2` | **LEASE-BUSY** — HTTP 409. Another run holds this identity's lease right now. | **Retryable, and the only outcome worth retrying.** The launcher git shim itself retries 30x at 1s. Re-run the probe shortly; do not escalate on this alone. |
| `3` | **CAPABILITY-REJECTED** — HTTP 401/403. This run's own capability was refused. | Re-check the header contract above first — a swapped header produces exactly this. If both headers are confirmed correct and it persists, escalate to the **CEO** as a platform availability/capability problem, not a board scope request. |
| `4` | **BROKER-DOWN** — connection refused, timed out, or no HTTP response. | The broker process itself didn't answer — distinct from a rejection or an unavailable identity. Escalate to the **CEO**, not the board (fact 1/2: there is nothing to provision here). A corroborating MCP call tells you if it's CLI-transport-specific. |
| `5` | **INCONCLUSIVE** — usage error, a required env var is missing, or the broker answered with an HTTP status the script doesn't recognize. | Read the printed notes for what's missing. Don't guess a verdict from a partial read; escalate the *specific* thing that's broken, not a generic flake. |

Run `scripts/check_github_identity.sh --help` for the full option/exit-code
reference inline.

## Escalation routing — board vs. CEO (updated by DAN-549)

Two different things can be wrong here, and they go to different people:

- **`NO-IDENTITY` (exit 1) is a provisioning gap** — nobody has set up a
  managed identity for this run's identity context. That's the **board**'s
  action (connect/reconnect the managed GitHub identity), because there is
  something concrete to provision, unlike facts 1 and 2's dead ends.
- **`CAPABILITY-REJECTED` (exit 3) or `BROKER-DOWN` (exit 4), after the
  header contract is confirmed correct, are availability problems** — the
  broker itself is misbehaving or refusing a validly-formed request. Per
  [DAN-220](https://github.com/Dromares/Hatate-Linux-Redux)-era process and
  the CEO's own DAN-259 disposition, that's a **CEO** judgment call about
  whether to forward a platform-availability problem upward, not something
  the board can fix by provisioning anything.
- **`LEASE-BUSY` (exit 2) goes to neither** — it's contention, not a
  defect. Retry.

Forwarding a provisioning gap to the CEO, or an availability problem to the
board for a credential/scope fix, both recreate the dead end this page
exists to prevent.

## Out of scope — on purpose

This page and `scripts/check_github_identity.sh` make the fault *legible and
self-service*. They do not fix the underlying vending flake, and nothing
here should ever be read as license to change a connection, grant, or
credential. That remains platform-side.
