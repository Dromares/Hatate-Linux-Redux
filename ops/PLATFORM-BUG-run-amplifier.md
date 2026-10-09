# Platform bug report — the run amplifier (DAN-94)

**Filed:** 2026-09-30 · **Origin:** DAN-94 · **Board decision:** fix both amplifier stages
**Evidence base:** 400 heartbeat runs, company `fc05af1b`, 2026-09-26 → 2026-09-30
**Reproduce:** `python3 ops/limit-triage.py` from the project root (read-only, stdlib only)

This is the escalation the board authorised. Nothing here is fixable from inside the company: all
three defects live in the platform's retry scheduler, wake dispatcher, and config contract, and the
OpenAPI surface (685 paths) exposes no field for any of them.

**Read defect 2 before acting on defect 3.** The board approved "enforce the wake cooldown" on a
premise that later testing falsified. The goal is unchanged; the mechanism named in the approval was
wrong, and building to it would implement the wrong fix.

---

## Defect 1 — `access`/`limit` failures are classified `transient_failure` and retried with no backoff

**Severity: high. This is the one that costs us runs.**

`scheduledRetryReason: transient_failure` is assigned to both terminal fault classes. The retry is
scheduled off the parent's *finish* time with effectively no delay:

```
retry-storm evidence (transient_failure retries: n=30)
median gap: 0.162s   min: 0.082s   max: 26.788s
```

Measured against the actual outage length, this is the whole bug:

| access band (UTC) | duration | dead runs | retried at |
|---|---|---|---|
| 09-30 03:47:08 → 04:02:43 | **935s** | 17 | ~0.162s intervals |
| 09-30 04:26:42 → 04:28:05 | 82s | 4 | ~0.162s intervals |
| 09-30 20:11:35 → 20:13:03 | 88s | 2 | ~0.162s intervals |
| 09-29 19:59:02 → 19:59:11 | 9s | 3 | ~0.162s intervals |

**A gate that was shut for 935 seconds was probed at 0.162-second intervals.** Real backoff would
have turned 17 dead runs into roughly 3 probes. Neither fault class is transient in the sense the
classifier assumes:

- `limit` = a 5-hour quota window. Resets on a wall-clock boundary, never in 0.162s.
- `access` = an account-wide gate closure lasting 9–935s (see below). Self-heals, but not instantly.

**Asks:** (a) stop classifying either fault as `transient_failure`; (b) exponential backoff with
jitter, floored well above 1s; (c) for `limit`, back off to the window boundary rather than guessing.

### Supporting characterisation of the `access` fault

Not a bug report in itself — included so the backoff policy is designed against the real shape.
All 24 `access` deaths fall into exactly four contiguous bands (>15min gap splits a band). In every
band: **zero successful runs started, by any agent.** Two of four bands span two agents on two
different models simultaneously (Cloud/`opus-5` and Virgil/`sonnet-5`). Cost billed in-band is $0
except one run that billed $5.18 and died mid-conversation — **the gate also closes on runs already
in flight**, so a retry policy that only guards run start is insufficient.

A fault that refuses two agents and two models at once, bills nothing, lasts 9–935s, and reopens
with no human action is **account-level and shared**. It is *not* an expired credential: it
self-heals. Re-authentication is the wrong remediation and the runbook now says so.

**Mechanism remains unidentified, and the provider-side log is a dead end.** `resultJson` is only
`{"summary": "ACP agent reported a terminal access failure."}`; `stderrExcerpt` and `stdoutExcerpt`
are null; the raw ndjson behind `logRef` carries no HTTP status, no 429/quota text, no stack trace.
**Ask:** surface the provider's HTTP status and response body on terminal ACP failures. We cannot
diagnose this class of fault from the current telemetry, and that gap is itself the blocker.

---

## Defect 2 — `runtimeConfig.heartbeat` is undocumented passthrough, and its scope is per-issue

**Severity: medium (correctness of the contract). This defect explains four revisions of our own
misdiagnosis, so it is worth more than its severity suggests.**

The agent `PATCH /api/agents/{id}` schema for `runtimeConfig` declares exactly two properties —
`aiConnection` and `debug`. Occurrences across the whole 685-path spec:

```
cooldownSec                       0
maxConcurrentRuns                 0
intervalSec                       0
wakeOnDemand                      0
skipTimerWhenNoActionableWork     0
```

The entire `heartbeat` block is accepted, stored, and readable back, but appears nowhere in the API
contract. Parts of it are clearly honoured (timer interval; per-issue serialisation), so this is not
"silently ignored" — it is **unspecified**, which is worse, because the scope cannot be inferred.

We then measured the scope, and it is **per-issue, not per-agent**:

| test | result |
|---|---|
| Dante overlapping same-agent run pairs | 17 |
| …of those, **same-issue** overlaps | **0** |
| …**different-issue** overlaps | **17** |
| Cooldown breaches, cross-issue | 26 |
| Cooldown breaches, **same-issue** | **19** |

`maxConcurrentRuns: 1` serialises runs *within* an issue and places no ceiling on an agent across
issues. Same for `cooldownSec`. Both read as agent-level settings and neither is.

**Asks:** (a) document the `heartbeat` block in the OpenAPI schema; (b) state the scope of
`cooldownSec` and `maxConcurrentRuns` explicitly in the field description; (c) reject unknown keys
under `runtimeConfig` rather than storing them, so a typo'd knob fails loudly instead of appearing
to work.

### Real per-issue cooldown breaches (a genuine, smaller bug)

Of the 19 same-issue breaches against a configured `cooldownSec: 60`, **13 are retries and 6 are
fresh dispatches.** The retry half is Defect 1 — the retry scheduler ignores the cooldown entirely.
The fresh half is its own small dispatcher bug, worst case two runs on the same issue **0.22s
apart**, neither a retry, both `automation`, both succeeding:

```
Cloud  gap=0.22s  21:41:50 -> 21:41:50  same issue  automation->automation  retryOf=.  succeeded
```

**Ask:** apply `cooldownSec` to retry scheduling as well as fresh dispatch, and de-duplicate
same-issue wakes arriving inside the cooldown.

---

## Defect 3 — there is no agent-level dispatch ceiling (feature gap, not an unenforced setting)

**This supersedes the framing the board approved. Please read the correction.**

DAN-94 revisions 3–4 reported "8 fresh starts in 52s against `maxConcurrentRuns: 1` /
`cooldownSec: 60`" and concluded the wake cooldown was not being enforced. **That conclusion is
wrong.** Breaking the burst down by issue:

```
03:59:43  automation  issue=da0fbb65  fresh
03:59:50  automation  issue=18098faa  fresh
03:59:54  automation  issue=e6ed0885  fresh
03:59:59  assignment  issue=761fda87  fresh
04:00:04  assignment  issue=3f693e29  fresh
04:00:08  timer       issue=None      fresh
04:00:12  automation  issue=93fbacb5  fresh
  -> 7 distinct issues across 8 fresh starts; one fresh start per issue
```

Given per-issue scoping (Defect 2), **the per-issue cooldown was respected by every one of those
starts.** Nothing was violated. The burst is the correct behaviour of a dispatcher that has no
concept of an agent-level ceiling: with *N* issues assigned, an agent can be woken *N* times in
seconds, and no available setting bounds that.

So the amplifier's first stage is a **missing feature**, not a broken one. "Enforce the wake
cooldown" cannot be implemented — there is nothing unenforced. The equivalent fix is to **add** a
per-agent dispatch ceiling (max concurrent runs per agent, and/or a token-bucket wake rate limit
across all that agent's issues), with excess wakes coalesced rather than dropped so no issue is
starved.

The board's intent — stop one fault becoming sixteen runs — is unchanged and this serves it. Only
the mechanism changed.

---

## Falsified-claims log

Kept because DAN-46 closed on an unchecked diagnosis and recurred. Six claims tested, six dead.

| # | claim | what killed it |
|---|---|---|
| 1 | `access failure` = expired Anthropic login | self-heals in 9–935s with no human action |
| 2 | Per-window spend predicts deaths | $145.74 window: 10 deaths; $85.95 window: 20 |
| 3 | `access` deaths are concurrency contention | 0.04 mean concurrent runs at start vs 1.34 baseline |
| 4 | Per-agent concentration ⇒ not a credential fault | base-rate artifact; Virgil dies in-band too |
| 5 | `freshSession=true` is an access fingerprint | also true for 141/141 successful runs |
| 6 | The wake storm breached `cooldownSec`/`maxConcurrentRuns` | per-issue scope; 7 distinct issues, 1 start each |

Claim 6 was ours, was current as of revision 4, and was the stated basis of an approved board
decision. It is corrected here rather than quietly dropped.

## What we already did on our side

Dante `opus-5` → `sonnet-5` (landed 09-30 03:32Z). Cloud heartbeat `intervalSec` 300 → 1800,
`cooldownSec` 10 → 60, `skipTimerWhenNoActionableWork` on. Extra usage deliberately left
org-disabled per board decision, so there is no overflow lane when a quota window ends — which is
why Defect 1's backoff matters more here than it would elsewhere.

Limit deaths have stopped since the 09-30 01:00 window (last one: 1 death, Cloud). **We are not
claiming credit for that.** Traffic collapsed over the same period — the two most recent windows
carry 3 and 20 runs against 66–73 in the deadly ones — so the model switch and the traffic drop are
confounded and cannot be separated from this data.
