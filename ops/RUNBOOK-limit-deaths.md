# Runbook: acpx_turn_failed deaths

The tool this runbook describes is `ops/limit-triage.py` **in this repo** — the
single canonical copy (DAN-1130 merged the former workspace-only original and
the DAN-926 rewrite). Run it from the repo root.

## Discriminator rule

`errorCode=acpx_turn_failed` covers two unrelated faults. Read the `error`
string, not just the code:

- `"...terminal limit failure."`  -> **QUOTA EXHAUSTED**. Wait for the 5h
  window boundary; reduce burn. This resolves itself.
- `"...terminal access failure."` -> a **turn-level** fault on a session that
  already authenticated successfully (the ACP session was established, the
  usage channel was live) before the turn died. It is **not an expired
  credential** in the common case. **What causes it is not yet established**
  — DAN-103's "contention/saturation" theory was tested in DAN-105 and does
  not hold (see below). Do not assume "re-authenticate" from this string
  alone — check the by-agent/by-window signature below first.

**`acpx_turn_failed` vs `acpx_session_init_failed` — these are not the same
fault.** Authentication happens before a session exists, so a genuinely
rejected credential shows up as `errorCode=acpx_session_init_failed` (a
distinct code from a distinct stage), not as an `access` death under
`acpx_turn_failed`. If you're chasing a credential problem, filter on
`errorCode=acpx_session_init_failed`, not on the `access failure` string.

### Is this `access` death actually a credential problem?

**DAN-106 retracts the DAN-103 per-agent/5h-window discriminator below.**
That rule said: access deaths concentrated on one agent, and/or interleaved
with successes/spend in the same 5h window, means "not a credential fault."
Both halves of that rule are base-rate artifacts, not evidence:

- **"Concentrated on one agent" measures who was awake, not who was
  affected.** One agent (Cloud) simply runs far more often than the others.
  Grouping the 24 known access deaths into contiguous time bands (gap>15min
  splits a band) produces exactly 3 bands, and **two of the three span two
  different agents on two different models running *at the same time*** —
  e.g. `Cloud` and `Virgil`, on `claude-opus-5` and `claude-sonnet-5`
  simultaneously, both dying inside the same 82-second span. A per-agent
  count over a whole day makes that look "concentrated on Cloud" purely
  because Cloud had more total runs that day, in and out of the bands.
- **"Interleaved with successes/spend in the window" is a granularity
  error.** The 5h window is roughly **20x too coarse** for this fault: one
  band lasted 935 seconds and killed every run that started inside it, but
  the 5h bucket around it also contains hours of unrelated normal traffic,
  so the window shows successes and real spend even though **zero runs
  succeeded inside the band itself**. A quiet window (few total runs) just
  happens to let the band fill more of it, which is why an earlier pass
  read the 09-29 15:00 window as a cleaner "spread + no successes" case —
  it wasn't a different signature, it was a quieter one.

**The replacement rule triages by band, not by 5h window or by agent
identity.** `ops/limit-triage.py` groups all `access` deaths into contiguous
time bands (default gap 900s / 15min splits one band from the next) and, for
each band, reports the agents and models active *inside that exact span*
(not the surrounding window) plus the in-band success count:

- **Zero in-band successes, spanning more than one agent or more than one
  model** -> **account-wide gate closure.** Not per-agent, not fixable by
  re-running "the agent that died" — there wasn't a single affected agent,
  everyone running in that span died. This self-heals; back off past the
  band. Do **not** re-authenticate.
- **Zero in-band successes, and only one agent was awake at all during the
  band** -> same shape, but **under-determined**: with only one agent in
  the sample there is no second agent available to have *not* been
  affected, so this cannot be distinguished from a genuinely per-agent
  fault. Say so; do not guess credential either way.
- **One or more in-band successes** -> **genuinely per-agent**, and this
  would be new. Every band observed as of DAN-106 has zero in-band
  successes. If this shows up, call it out loudly instead of folding it
  into the account-wide case — it means some other agent kept succeeding
  while this one kept dying in the same span, which is the actual
  per-agent signature the old rule was trying (and failing) to detect.

The three known bands, reproduced by the current tool:

```
09-29 19:59:02 -> 19:59:11   dur=9s    deaths=3   agents=[Cloud]          models=[claude-fable-5]
  -> under-determined (sole agent awake)
09-30 03:47:08 -> 04:02:43   dur=935s  deaths=17  agents=[Cloud,Virgil]   models=[claude-opus-5,claude-sonnet-5]
  -> account-wide gate closure
09-30 04:26:42 -> 04:28:05   dur=82s   deaths=4   agents=[Cloud,Virgil]   models=[claude-opus-5,claude-sonnet-5]
  -> account-wide gate closure
```

Also retracted: DAN-105 reported that 23/24 access deaths shared
`freshSession=true, costUsd=0`. That is not a fingerprint — `freshSession`
is `true` on every successful run in the same sample too (141/141); it is a
constant of how sessions are created, not a signal correlated with the
fault. Do not use it as a discriminator. `costUsd=0` on most access deaths
is real, but it is a **consequence** of dying within seconds of session
init, not a cause. One band member is the genuine exception worth keeping:
the 03:47:08 run inside the 935s band billed **$5.18** and died
mid-conversation, after real work — the gate closed on an in-flight run,
not only on fresh ones.

DAN-105's contention/saturation theory (from DAN-103) still does not hold —
that part of the investigation stands. Access deaths happen when nothing
else is in flight for that agent:

| outcome        | n   | mean concurrent runs at start | had >=1 overlap |
|----------------|-----|-------------------------------|------------------|
| access death   | 24  | 0.04                          | 4%               |
| limit death    | 23  | 0.52                          | 39%              |
| everything else| 153 | 1.34                          | 82%              |

What DAN-105 found instead is a **start-rate burst**: many fresh runs for
one agent landing within a short span (e.g. 8 starts in 52s from 3 different
`invocationSource` values, none of them retries — see
`ops/limit-triage.py`'s start-rate burst section, unchanged by DAN-106).
Whether the burst *causes* the access deaths, or both share a common
trigger, is still not established — treat "rate" as a lead, not a
conclusion. A DAN-105 log pull for a representative access death
(`903e540d-67b9-4c97-b739-4f4863daa71d`, corroborated on
`f431b343-f53e-409f-8d0a-1da19d5b4e0b` from the same burst) found no further
diagnostic detail: the raw provider log is a session that initializes
cleanly, reports one successful `usage_update` (0 cost), and then
immediately emits `{"type":"acpx.error", ..., "message":"ACP agent reported
a terminal access failure."}` with no HTTP status, no rate-limit-specific
string, and no stack trace underneath it — that message *is* the full
provider-side detail available.

`ops/limit-triage.py` implements the band rule above and prints a verdict
per band — do not eyeball it from the raw per-window counts, since (per
DAN-106) the same access-death count in a window can hide a short,
account-wide band or a longer, genuinely ambiguous one depending on what was
running inside the band itself.

## `cooldownSec` / `maxConcurrentRuns` are scoped per-issue, not per-agent (DAN-117)

**This was measured, not read from a contract.** The agent `PATCH /api/agents/{id}`
schema declares exactly two `runtimeConfig` properties — `aiConnection` and
`debug`. The whole `heartbeat` block (`cooldownSec`, `maxConcurrentRuns`,
`intervalSec`, `wakeOnDemand`, `skipTimerWhenNoActionableWork`) is accepted,
stored, and readable back, but **appears zero times across the full 685-path
OpenAPI spec.** There is no field description to check the scope against, so
the only way to find it was to measure run behaviour directly:

| test | result |
|---|---|
| Dante overlapping same-agent run pairs | 17 |
| …of those, **same-issue** | **0** |
| …**different-issue** | **17** |
| Cooldown breaches, cross-issue | 26 |
| Cooldown breaches, **same-issue** | **19** |

Zero of 17 overlapping same-agent run pairs were same-issue — every overlap
was across different issues. That 0/17 is the basis for treating the scope as
per-issue rather than per-agent: `maxConcurrentRuns: 1` serialises runs
*within* an issue and places no ceiling on an agent across issues; same for
`cooldownSec`.

**Consequence for the start-rate burst report (DAN-105):** a burst of N fresh
starts for one agent inside the burst window is **not evidence of a config
breach** if those N starts land on N distinct issues — each one, individually,
respected its own issue's cooldown. That shape is the *absence of an
agent-level dispatch ceiling* (a missing feature: nothing bounds how many of
an agent's issues can wake it in the same few seconds), not `cooldownSec`
being ignored. `ops/limit-triage.py`'s burst report is issue-aware: it prints
`distinct_issues` and `max_starts_on_any_single_issue` per burst and keys the
verdict on those two numbers, not the raw start count. Only a **real
per-issue cooldown breach** — the same issue dispatched again before its own
agent's `cooldownSec` elapsed — gets flagged as a breach, and those pairs are
listed with their gap and whether the second start was a `retryOfRunId` of
the first (a retry ignoring cooldown is Defect 1 from
`ops/PLATFORM-BUG-run-amplifier.md`; a fresh dispatch landing inside cooldown
is a separate, smaller dispatcher bug — see that doc's Defect 2).

The triage script also prints a dedicated **same-issue cooldown breach**
section covering *all* consecutive same-issue starts per agent (not just
those inside a burst), split into retries vs. fresh dispatches, checked
against each agent's own `cooldownSec` — read live for the caller's own
agent record (`GET /api/agents/me`), since `GET /api/agents/{id}` for any
*other* agent silently returns `runtimeConfig: {}` (not a permission error)
and `GET /api/agents/{id}/configuration` demands a grant this tooling does
not hold. Other agents' `cooldownSec` therefore comes from a small
board-confirmed fallback table in the script (`FALLBACK_COOLDOWN_SEC`) —
Cloud 60s, Virgil and Dante 30s as of DAN-117 — not a live read; update that
table if those values change.

**DAN-118 correction to the burst verdict:** the burst report's `real_breach`
verdict originally fired on *any* same-issue repeat inside cooldown,
including a repeat that was itself a retry (`retryOfRunId` set) — that is
Defect 1 (the retry scheduler ignoring cooldown), not a per-issue dispatcher
breach, and blaming the dispatcher for it hid the one burst in the sample
that actually demonstrates the missing agent-level ceiling. `burst_verdict()`
now applies the same retry/fresh split `same_issue_cooldown_breaches()`
already used: only a *fresh* (non-retry) same-issue repeat inside cooldown
promotes a burst to `real_breach`. A burst whose only in-cooldown same-issue
repeats are retries reports `no_agent_ceiling` instead — many distinct
issues, one fresh start each, with the retry-caused repeats printed
separately and attributed to Defect 1.

## Running the triage

```
python3 ops/limit-triage.py [--limit N] [--json]   # default N=200
```

`--limit N` selects the sample the sections below are computed over: the
newest N runs across all seats (the tool fetches every seat's reachable
history through the per-agent filter, then cuts the newest N). The final
**incident report** (DAN-926) is the one section that ignores `--limit`: it
buckets every reachable failed run by its own `errorCode` and collapses
`retryOfRunId` chains, so a retried deterministic failure (e.g. `spawn E2BIG`)
counts once rather than once per retry — `rawRows` is shown beside
`incidents` for comparison. `--json` emits the incident summary, the access
bands with verdicts, and `exitCode`.

Reads `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `PAPERCLIP_COMPANY_ID` from
the environment (already set in any Paperclip agent run). Prints a per-5h-window
table (run count, successful-run count, `usageJson.costUsd` sum,
limit/access/init/other death counts (`init` is `acpx_session_init_failed`,
added in DAN-1130; the rest unchanged by DAN-106 and still the right
resolution for `limit` deaths), a by-agent breakdown and verdict for any
window with `limit` deaths, an **access-band report** (DAN-106): each
contiguous group of `access` deaths (gap>`--band-gap-sec`, default 900s,
splits a band) with start/end/duration, death count, the distinct agents and
models active *inside that span*, the in-band success count, in-band access
cost, time to the first success after the band, and a verdict per the
discriminator above — retry-storm gap stats, and (DAN-105) a per-agent
start-rate burst report: `--burst-window-sec` (default 60) and
`--burst-threshold` (default 6) control what counts as a burst — the
defaults were picked so the worst normal-load agent in a 200-run sample
(5 starts/60s) stays under threshold while the DAN-105 wake storm (8
starts/60s) trips it. Each burst line shows the agent, time span, start
count, the `invocationSource` mix, e.g. `automation=5, assignment=2,
timer=1`, and (DAN-117) `distinct_issues`/`max_starts_on_any_single_issue`
plus a verdict — see the per-issue-scoping section above for why the verdict
is keyed on those two numbers rather than the raw start count. A final
**same-issue cooldown breach report** (DAN-117) lists, per agent, every
consecutive same-issue start pair inside that agent's own `cooldownSec`,
split into retries vs. fresh dispatches.

Exit codes: `0` clean; `1` deaths present but self-resolving (quota
exhaustion, an account-wide access band, or an under-determined
single-agent access band); `2` the most recent access band has one or more
in-band successes — the genuinely-per-agent signature DAN-106 has not yet
observed, and it needs investigation rather than a back-off. The start-rate
burst and same-issue cooldown-breach sections (DAN-105/DAN-117) do not
affect the exit code — they are informational.

## What to do in each case

**`access` deaths in an account-wide or under-determined band (the common
case — see discriminator above):**
1. Do not re-authenticate anything; there is no credential fault to fix —
   DAN-106 retracted the per-agent/5h-window rule that made this look like a
   per-agent problem in the first place.
2. Root cause of the gate closure itself is not established — DAN-105 ruled
   out contention/saturation and DAN-106 ruled out per-agent attribution,
   but neither found a replacement cause. Back off and re-run past the band;
   check `ops/limit-triage.py`'s start-rate burst output to see if a burst
   coincided.
3. Retry backoff itself is platform-side, not something to fix from this
   runbook or the triage script (see DAN-94/DAN-103/DAN-105/DAN-106 for that
   thread).

**`access` deaths in a band with >=1 in-band success (per_agent_new — not
yet observed as of DAN-106):**
1. This is new: some other run succeeded while this agent's runs kept dying
   in the same span, which is the actual per-agent signature the retired
   rule was trying to detect. Investigate that agent/session specifically
   rather than assuming it self-heals.
2. Do not reflexively re-authenticate either — confirm first whether the
   failing runs share a session, credential, or something else specific to
   that agent before treating it as a login problem.

**`limit` deaths, no `access` deaths:**
1. This is quota exhaustion on the shared 5h subscription window. It is
   expected under high burn and self-resolves at the window boundary.
2. If it's recurring, reduce concurrent agent load or defer non-critical
   work rather than escalating — there is no API to raise the quota or
   change retry policy (confirmed absent from the OpenAPI surface; do not
   go looking for one).

**Both `limit` and `access` in the same sample:** triage `limit` as quota
exhaustion (self-resolving, 5h window) and evaluate `access` independently
by band, per the discriminator above — they are unrelated faults and one is
not evidence for the other.

## Notes on the data

- **Cost does not reliably predict deaths.** The highest-cost window in a
  recent 200-run sample had only 2 limit deaths, while a lower-cost window
  had 20. Don't use a cost or token threshold as an early-warning signal for
  `limit` deaths. (In-band cost is still load-bearing evidence for
  individual access deaths — e.g. the $5.18 in-flight death above — it's
  just not a per-5h-window leading indicator.)
- **Retry-storm evidence:** for deaths with `retryOfRunId` set and
  `scheduledRetryReason=transient_failure`, the gap between the retry being
  *created* and its parent run *finishing* has a sub-second median (~0.1s).
  The platform re-schedules almost instantly; the actual re-run is delayed
  by queueing, not by the retry logic. DAN-105 found that retries are not
  what drives the access-death bands above — the low concurrency-at-start
  numbers mean this clustering is not multiple agents contending for a
  shared gate. Retry backoff is platform-side and out of scope for this
  runbook/script.
- `acpx_session_init_failed` (e.g. `spawn E2BIG`, or a rejected credential)
  is a different, unrelated fault at a different lifecycle stage (pre-session,
  vs. `acpx_turn_failed` which is post-session) — the triage script gives it
  its own `init` column so it neither pollutes the limit/access counts nor
  hides in `other`, and the incident report counts it per retry chain.

## Standing watch (DAN-118 — carries forward after DAN-94 closed)

DAN-94 closed by board decision on 2026-09-30: its AC4 ("N consecutive clean
windows") was retired because it gated on traffic volume this company has
never had, not on the defect actually being fixed. **AC4-style "N clean
windows" bars do not work here and should not be reinvented:** the company
has never had two consecutive `>=50`-run 5h windows, so the strict form of
the bar could never be met just by waiting, and the looser form passed
whenever the company happened to be idle — i.e. it measured silence, not
health. That reasoning cost DAN-94 two plan revisions; do not re-derive it.

With DAN-94 closed, no open issue is carrying "is this fault back?" —  that
job now lives here. An operator or a future heartbeat reopens/escalates on
any of these triggers, each stated against the last known-good baseline in
this sample:

1. **A `limit` death after >=15h of `limit` quiet.** The last `limit` death
   in the reference sample was `09-30 05:09:53Z`. A fresh one after a gap
   that long (versus the usual few-hour recurrence) is a shape change in the
   quota-exhaustion pattern, not routine burn — investigate before assuming
   it is the same self-resolving fault.
2. **An `access` band longer than the current 935s worst case.** All bands
   observed to date (9s, 935s, 82s, 88s) self-healed well under 26 minutes.
   A band that runs longer than the 935s outlier is new territory.
3. **Any band that does not self-heal.** All four observed bands recovered
   in 26–1547s with no human action (see `time_to_recovery` in the triage
   output). A band that stays shut — no successful run anywhere past it in a
   reasonable follow-up window — is the one signal that would genuinely mean
   a dead credential, and it is the *only* case where
   `POST /api/agents/{id}/claude-login` is the right response. Do not
   re-authenticate on a band that is still within its self-healing window;
   see "What to do in each case" above.
4. **`limit-triage.py` exiting `2`.** That is an `access` band with `>=1`
   in-band success — the genuinely-per-agent signature DAN-106 has never
   observed as of this writing. If it appears, it falsifies the account-wide
   shape the whole `access` discriminator rests on; treat it as a priority
   investigation, not routine triage output.
5. **`spawn E2BIG` / `acpx_session_init_failed` rising above its baseline.**
   The original "7 runs in one window" baseline counted retry rows; measured
   by incident (DAN-926) the 2026-09-29 cluster is 3 incidents / 7 rows, all
   against one issue. Read the `init` column and the incident report, and
   compare incidents to incidents — see `ops/PLATFORM-BUG-spawn-e2big.md`.

None of these triggers change `limit-triage.py`'s exit code semantics except
trigger 4, which the script already implements (exit `2`). Triggers 1, 2, 3,
and 5 are read from the triage output and the access-band `time_to_recovery`
field by inspection; they are not separately coded into the script's exit
status.
