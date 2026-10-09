# PLATFORM BUG: `spawn E2BIG` on acpx session init

**Status:** root-caused, company-side mitigation delivered (DAN-926); the
actual fix is vendor-bundle work held on DAN-920 per the DAN-348
containment rule (nothing in this company's repo can touch it).

**Note on provenance.** DAN-920's description cites this file (and
`ops/limit-triage.py`) as already-existing evidence artifacts. Neither
existed anywhere in this repo's git history, on any branch, or as a work
product -- confirmed by searching all of `git log --all` 2026-10-09. The
most likely explanation is the DAN-883 run-scratch trap already on record
in AGENTS.md: a prior run wrote these under `$PAPERCLIP_RUN_SCRATCH_DIR`
(or similar), cited them as committed, and the directory was reclaimed at
the end of that run before anything landed in git. This is a fresh writeup,
not an edit of a lost one -- the evidence below was re-measured from
scratch on 2026-10-09, not recovered.

## Mechanism (corrected)

This is **not** an `ARG_MAX` problem. `ARG_MAX` (2 MiB on this host, via
`getconf ARG_MAX`) is the ceiling on *total* argv+env size, and a healthy
spawn uses about 15 KB of it -- 0.7%, roughly 140x headroom. No contributor
anyone has measured gets remotely close to that ceiling.

The real ceiling is Linux's **per-string** limit on any single argv or
envp entry:

```
MAX_ARG_STRLEN = 32 * PAGE_SIZE
```

4 KiB pages on this host -> **131,072 bytes**. `execve()` rejects the spawn
with `E2BIG` the instant any *one* string crosses that line, regardless of
how small the total argv+env is.

The string that crosses it is `PAPERCLIP_WAKE_PAYLOAD_JSON` -- the
JSON-serialized `contextSnapshot.paperclipWake` object the platform injects
into the spawned `claude` process's environment, carrying the per-run
continuation/wake state (comment history, child-issue summaries, execution
continuation messages, task markdown, etc.). It is structurally unbounded:
it grows with an issue's comment count and history depth, with no cap or
pagination on the server side.

**`--mcp-config` is non-causal for this fault.** It was the original
suspect (grows with connected tool integrations, also argv), but measured
directly on a healthy spawn it is 1,443 bytes -- 1.1% of the 131,072-byte
limit that actually fires. It would need to grow nearly 100x before it
could matter here. Withdrawn as a contributor to this specific fault; it
may still be worth moving off argv on general hygiene grounds, but that is
a separate, much lower-priority concern from this one.

## Reproduction

```
cat /proc/<pid>/environ | tr '\0' '\n' | grep '^PAPERCLIP_WAKE_PAYLOAD_JSON=' \
  | wc -c
python3 -c "import os; print(os.sysconf('SC_PAGE_SIZE') * 32)"   # 131072
```

Compare the two. A spawn that failed `E2BIG` can also be checked after the
fact without `/proc` access, since the platform persists the attempted
wake payload: `GET /api/heartbeat-runs/{id}` (the **per-run detail**
endpoint only -- the bulk `GET /api/companies/{id}/heartbeat-runs` list
returns a gutted `contextSnapshot` missing `paperclipWake` entirely, with
nothing in the response signalling the omission) returns
`contextSnapshot.paperclipWake`; serialize it the way the platform does and
measure:

```python
import json
payload = run["contextSnapshot"]["paperclipWake"]
size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
```

This measured within ~1.2% of the real env value on a live run (33,301
computed vs 32,897 actual) -- close enough to treat as ground truth.
`ops/payload_headroom.py` (DAN-926) automates exactly this, with a
proxy-based fallback for issues that have not yet produced a qualifying
run.

## Evidence

Three failing runs, Beatrice, 2026-10-08, chained by `retryOfRunId`
(`78587b49` -> `29d46b65` -> `d19b4c0d`), all `resultJson.phase:
"ensure_session"` -- i.e. the spawn itself, before the agent ever got a
turn:

| quantity | measured | vs limit |
|---|---|---|
| `contextSnapshot.paperclipWake` serialized | 132,721 / 132,212 / 132,535 bytes | 101.3% / 100.9% / 101.1% of 131,072 -- OVER |
| of which `executionContinuation.messages[]` (54/56/57 entries) | 113,763 bytes | 85.7% of the payload |
| `--mcp-config` argv string (healthy run) | 1,443 bytes | 1.1% of 131,072 -- non-causal |
| total argv+env (healthy run) | 14,982 bytes | 0.7% of `ARG_MAX` |

This is a **retry-amplified single incident, not three independent
failures** -- the fault is deterministic in payload size, so each
automatic retry replays the same oversized payload and dies identically.
`ops/limit-triage.py` (DAN-926) now collapses `retryOfRunId` chains before
counting for exactly this reason: a scan of every seat's full run history
(back to each seat's earliest available run, 2026-09-27 through
2026-10-09) found **10 raw `acpx_session_init_failed` rows, collapsing to 4
real incidents**:

| when | seat | issue | raw rows | incidents |
|---|---|---|---|---|
| 2026-09-29, 02:16-02:50 UTC | Cloud | DAN-1 ("Paperclip onboarding") | 7 | 3 |
| 2026-10-08, 01:43-02:07 UTC | Beatrice | DAN-781 | 3 | 1 |

The 2026-09-29 cluster corrects DAN-920's own citation on every count: it
names "the 2026-09-30 'third fault' ... 7 occurrences" -- the date is
2026-09-29 (one day earlier), the seat was Cloud, not an unspecified
Beatrice-pattern seat, and it is 3 incidents, not 7 (the same
retry-overcounting DAN-914 made about the 2026-10-08 Beatrice incident,
applied a second time to an event that, per the provenance note above, was
apparently never actually measured before now). Both known incidents share
the same shape DAN-920 already identified: a heavy, long-lived,
deeply-cross-linked issue (DAN-1 was the company's own first issue; DAN-781
is independently described that way in DAN-920) -- consistent with payload
size scaling with an issue's own history depth, not with seat identity or
quota.

**Not an outage.** Across the full measured window this is 4 incidents
against several thousand successful runs. Narrow, intermittent, repeatable,
and now bounded by `ops/payload_headroom.py`'s warn/alarm thresholds before
it costs another run.

## Gap, still open

The platform does not record the attempted argv/env byte count on an
`E2BIG` death in any field surfaced by `GET /api/heartbeat-runs/{id}` taken
*at the time of failure* -- `contextSnapshot.paperclipWake` is still
present and measurable after the fact (see Reproduction above), which is
how the table above was built, but there is no first-class "this spawn
would have been N bytes over the ceiling" field. Logging the computed size
before `execve()` server-side (asked for below) would remove the need to
reconstruct it from the stored snapshot.

## Asks (board decision -- vendor-bundle work, held on DAN-920 per DAN-348)

1. Move `--mcp-config` off argv onto a file reference or stdin in the
   vendored spawn path (hygiene; not the cause of this fault).
2. Cap or paginate the continuation/wake payload injected per run instead
   of always replaying full history -- this is the actual fix for the
   fault measured here.
3. Log the computed argv+env byte count (or at least the
   `PAPERCLIP_WAKE_PAYLOAD_JSON` string length alone) before `execve()`, so
   a future occurrence doesn't require reconstructing the cause after the
   fact.

## Related DAN-926 findings (company-side, not vendor-bundle)

- `GET /api/companies/{id}/heartbeat-runs` hard-caps at 1000 rows
  regardless of `?limit=` and silently ignores `?offset=` -- confirmed by
  requesting `limit=2000`/`5000` (still exactly 1000 rows) and
  `offset=1000` (byte-identical to no offset). `?agentId=` **is** honoured
  and, because each seat produces far fewer runs/day than the fleet total,
  reaches each seat's entire history -- this is how the 2026-09-29 cluster
  above was found at all.
- The same endpoint's bulk list vs. per-run detail asymmetry
  (`contextSnapshot.paperclipWake` present only in the latter) is the same
  "bulk list lies" shape already on record for issues (`executionPolicy`,
  DAN-348) and agents (`access.grants`, DAN-497).
- The bulk `GET /api/companies/{id}/issues` list truncates `description` to
  1,200 bytes (`descriptionTruncated: true`) on 947 of 1,000 rows sampled
  2026-10-09 -- irrelevant to this fault directly, but relevant to anything
  trying to estimate a wake payload's size from issue content, which is
  exactly what `ops/payload_headroom.py`'s proxy path does; it reads
  per-issue, never from the bulk list, for this reason.
