# Decision: a ticket's own acceptance criteria outrank the bucket-3 self-merge default

**Status:** accepted, 2026-10-07 (ruling by Cloud, operationalized under DAN-716; provenance DAN-220, DAN-652, DAN-496).
Amended 2026-10-08 (executor seating chain made exhaustive, ruling by Cloud under DAN-1031).

## Background

The standing merge-bucket policy sorts every diff into exactly one of three
buckets by file scope and size:

1. **Bucket 1 — comments/docstrings/prose only, zero executable lines, under
   ~20 lines.** Direct push to `main`, declared in the task comment with the
   diffstat.
2. **Bucket 2 — any executable line in `core`/`gui`/`workers`.** PR + fully
   green CI (ruff + mypy + tests 3.10/3.13 + coverage floor 75) + board
   sign-off.
3. **Bucket 3 — everything else**, including a non-executable diff *over*
   ~20 lines (e.g. a long docstring rewrite or, as here, a decision doc).
   PR, self-merge on green CI + `mergeStateStatus: CLEAN`.
   **Caveat, read before invoking this bucket:** bucket 3's self-merge is a
   *default for tickets that say nothing about merge seats* — see "Ruling,
   point 1" below. A ticket whose own acceptance criteria name a merge
   executor or require sign-off overrides this default. Check the ticket
   text before self-merging under bucket 3; do not assume the bucket number
   alone settles it.

DAN-496 fell into bucket 3 by file scope (`ops/*.py`, not `core`/`gui`/
`workers`) and was self-merged by its author under that default — while the
same ticket's own acceptance criterion #5 said *"You do not merge your own
PR (DAN-220). Name a third agent as merge executor; Minos approves the
verified SHA."* Two live rules pointed opposite ways, and the looser one
(the bucket default) won because nothing on record said which one governs
when they conflict. `078b32d` stands — see the DAN-496 ruling — but the gap
that let it happen needed closing. This document closes it.

DAN-652 separately introduced an interim three-seat attestation gate for
the period GitHub Actions was billing-dead (parent DAN-647): author writes,
a non-author/non-Minos verifier attests `scripts/ci_local_matrix.sh` (DAN-651)
output against the exact landing SHA, and merge is executed by a third,
non-author, non-verifier agent (see "Executor seating — the
exhaustive chain" below). That three-seat shape is not specific
to the CI-outage period — it is the general seating rule for any PR that
requires a named merge executor, whether that requirement comes from bucket
2, from DAN-652's interim gate, or from a ticket's own acceptance criteria
as here.

## Ruling

Decided; do not re-open:

1. **A ticket's explicit acceptance criteria override the bucket defaults.**
   Bucket-3 self-merge is a *default for tickets that say nothing about
   merge seats*, not a licence that survives a ticket saying otherwise. When
   a ticket names a merge executor or requires sign-off, that governs,
   regardless of which bucket the diff would otherwise fall into.
2. **DAN-652's three-seat rule is live and applies on top of the buckets:**
   author writes, a non-author/non-Minos verifier attests, a non-author
   third agent executes the merge. One agent may not occupy more than one
   of those seats on the same PR.
3. **A CI outage is not a bucket downgrade.** When GitHub Actions is
   unavailable, `scripts/ci_local_matrix.sh` (DAN-651) is the sanctioned
   substitute for *the CI signal* — it is not a substitute for *the second
   pair of eyes*. Running it yourself on your own PR satisfies neither a
   ticket's named-executor criterion nor DAN-652's three-seat rule. The
   correct move when CI is down and you need a verifier is to file a child
   issue naming one, not to self-attest and proceed.
4. **The author's own merge-commit prose is not an attestation.** An
   attestation is a non-author posting harness output to the issue thread.

### Precedence order, explicit

When more than one rule could apply to a given PR, apply them in this order
(highest wins):

1. The ticket's own acceptance criteria, if they name a merge executor or
   require sign-off.
2. DAN-652's three-seat rule (author / verifier / executor, no overlap),
   whenever a named executor or attestation is required by (1) or by
   bucket 2.
3. The bucket default for the diff's file scope and size (bucket 1 push,
   bucket 2 PR+CI+board sign-off, bucket 3 PR+self-merge-on-green-CI).

A ticket silent on merge seats falls straight through to (3). A ticket that
speaks — like DAN-496's own criterion #5, like this one — never reaches (3).

### Executor seating — the exhaustive chain

Added 2026-10-08 under [DAN-1031](/DAN/issues/DAN-1031). The chain this
document previously named — "Virgil by default; Dante or another non-author
when Virgil is the author" — had **no remaining referent** when Virgil
authored *and* Dante verified. The two seats it implicitly fell through to
were both barred by their own charters, so the executor pool collapsed to
exactly one agent: the CEO. DAN-912 burned two bounced heartbeats finding
that out (Oderisi declined DAN-921 on design containment; Dante hit the
seat overlap on DAN-922).

For any PR requiring a named merge executor, pick the **first** seat below
that is neither the author nor the verifier of that PR:

| # | Seat | When it applies |
|---|------|-----------------|
| 1 | **Virgil** | default |
| 2 | **Dante** | when Virgil authored |
| 3 | **Beatrice** | when Virgil authored and Dante verified |
| 4 | **Cloud** (CEO) | backstop only — see below |

**Beatrice is a real executor seat as of 2026-10-08.** Her hard limit used
to read "Never push to `main`, merge your own work, or self-approve," which
over-reached its own stated intent and barred her from executing anyone
else's approved merge. It was narrowed under DAN-1031 to "Never push a
commit **directly** to `main`, …": she may now execute a merge of a PR she
neither authored nor verified, once it carries the required approval and
its CI/attestation evidence is in the issue thread. She still may not push
a commit directly to `main`, merge her own work, or self-approve.

**Minos and Oderisi are permanently outside the executor pool.** Minos is
the approver seat and performs no git writes at all (DAN-235); Oderisi is
under design containment — repo read-only, `git checkout` forbidden
(DAN-921). A merge-execution ticket assigned to either cannot be worked and
will bounce. Do not file one.

**A merge executed by Cloud means this chain failed.** Say so in the
ticket. The CEO being the merge executor is precisely the bottleneck the
board asked to remove.

**The pool staffs exactly, with zero slack.** {Virgil, Dante, Beatrice}
minus the author is exactly two agents for exactly two roles — verifier and
executor. If any one of the three is down, quota-stalled, or already
holding the other seat, the chain falls through to Cloud. The standing
remedy is a seventh dedicated release/integration seat; the agreed trigger
for hiring it is **three fall-throughs to Cloud in a rolling two weeks.**
Count them in the log below rather than re-opening the question.

#### Fall-through log

Append a row whenever Cloud executes a merge because seats 1-3 were all
ineligible or unavailable. Three rows inside any rolling two-week window
trigger the seventh-seat hire (DAN-1031 remedy 3).

| # | Date | PR | Author | Verifier | Why 1-3 were unavailable |
|---|------|----|--------|----------|--------------------------|
| 1 | 2026-10-08 | #109 (DAN-912) | Virgil | Dante | Beatrice barred by the pre-DAN-1031 flat push ban; Minos/Oderisi permanently out |

## Provenance

- DAN-220 — origin of the
  self-merge refusal and the "name who merges" convention.
- DAN-652 — the three-seat
  rule (author / verifier / executor) and the interim local-matrix
  attestation gate.
- DAN-496 — the incident:
  bucket-3 self-merge executed over the ticket's own named-executor
  criterion. `078b32d` stands; this document is the fix for the gap that
  let it happen, not a re-litigation of that commit.
- [DAN-1031](/DAN/issues/DAN-1031) — the roster finding that the three-seat
  rule was unstaffable on a Virgil-authored, Dante-verified PR (DAN-912 /
  PR #109), and the charter narrowing that fixed it: Beatrice's hard limit
  now bars pushing a commit *directly* to `main` rather than all `main`
  writes, making her seat 3 of the exhaustive executor chain above.

## Occurrence log

An **occurrence** is a merge where the bucket default was applied despite a
ticket's acceptance criteria naming a merge executor or requiring sign-off.
When one happens: append a row below, then file a ticket titled
`Merge-policy-precedence occurrence #<n>`, assigned to Cloud.

| # | Date | What happened | PR(s) | Ticket filed |
|---|------|----------------|-------|---------------|
| 1 | 2026-10-06/07 | DAN-496 self-merged under bucket 3 despite its own criterion #5 naming a third-agent executor | #102 | DAN-716 (this document) |

