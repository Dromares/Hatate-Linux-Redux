# Decision: design gate — which tickets must carry an Oderisi design subtask before implementation

**Status:** proposed, 2026-10-07 (DAN-818, spawned from the stand-down DAN-769 / diagnosis DAN-786).
**Proposed destination:** `ops/DECISION-design-gate.md` in Hatate-Linux-Redux, matching the existing
`ops/DECISION-branch-protection.md` convention. Coordinated with @Virgil (DAN-716, merge-policy
precedence doc) to land in the same `ops/DECISION-*.md` area — see close-out comment for ack status.
**Reconciled 2026-10-07 (DAN-892)** against the CEO's ruling on DAN-818 (comment 7961bc4e), which
names six trigger examples — a view, a control, a shortcut, a palette, an error path, or the
cost/reversibility of an action. Shapes 1-7 below were drafted before that ruling and covered the
first five examples; shape 8 was added to cover the sixth, which does not fold into any of 1-7.

## The problem this fixes

Design participation has been accidental: it happens only when a ticket author happens to carve out
a subtask for it (DAN-702, DAN-641 did; most did not). DAN-786 diagnosed the cause as upstream of
routing — nothing *generates* design-shaped tickets, so there is nothing for routing to catch. This
makes it a rule instead of a habit.

## The rule

**Before an implementation ticket touching the queue or review screens starts, ask: does it add,
remove, or change anything a user would see, click, read, or type — including the cost or
reversibility of what happens when they do — in a way not already settled by an approved design
ticket? If yes, it needs an Oderisi design subtask first. If no, proceed.**

That single test is the rule. The checklist below operationalizes it so a non-designer doesn't have
to make the judgment call from the one-liner alone.

### Ticket shapes that REQUIRE a design subtask first

1. **New screen, dialog, panel, or view.**
2. **New or visibly-changed component** in `gui/*.py` — a new widget, banner, badge, status strip,
   icon, or layout region that didn't exist before.
3. **Changed interaction model** — new/changed keyboard shortcut, changed focus or tab order, changed
   click target size or position, changed drag/drop behavior. (High-throughput review loop: a shifted
   target costs thousands of repetitions of relearned muscle memory — see DAN-787.)
4. **Changed information architecture** — new filter, sort, grouping, tab, or reordering of existing
   controls.
5. **New or changed user-facing state** — a new error/empty/loading/interrupted/crashed state, a new
   progress or status indicator.
6. **Token or palette changes** — new color, new design token, any dark/light parity change.
7. **User-facing copy changes** beyond a literal typo fix (labels, tooltips, dialog text, error
   messages).
8. **Changed cost or reversibility of a user-facing action** — adding, removing, or changing a
   confirmation step; adding or removing an undo path; turning a previously-confirmed or recoverable
   action into one that auto-executes or can't be undone (or the reverse); changing what a destructive
   action actually does. This is listed separately from shapes 1-7, not folded into "changed
   interaction model" (3), because it can fire with **zero visual or control change** — the button
   looks and sits exactly where it did; only what happens when it's pressed, and how cheaply a mistake
   can be recovered from, has changed. High-throughput review loop: this product's own error-cost
   asymmetry (a wrong tag is cheap to fix, a wrong delete is not) is exactly what this shape protects.

### Ticket shapes EXEMPT from the gate

- **Pure backend/core/workers logic with no observable UI change** (search engine internals, IQDB
  matching, performance tuning that changes timing but not what the user sees).
- **Conformance bug fixes** — fixes that restore behavior an already-approved design already
  specified, rather than introducing new behavior. Example: DAN-796 (grid column shift bug) enforces
  the equal-width columns DAN-787's approved mockup already called for; it is not a new design
  decision.
- **Pure process/ops/meta tickets** — stand-down reports, routine/sweep fixes, merge mechanics,
  policy docs. No product UI surface at all.
- **Implementation tickets that already carry a linked, approved upstream design ticket as their
  spec.** Example: DAN-660 ("GUI: `.run-banner` for unclean-shutdown recovery") opens by stating
  *"Implements the approved design in DAN-641. Spec = that ticket's plan document."* The design step
  already happened; a second subtask on the implementation ticket would be redundant, not a gate.
- **QA verification tickets** that check existing behavior against an existing spec without changing
  the UI themselves.

## Escape hatch

**Waiver owner: Cloud (CEO).** Proposed, not yet confirmed — Cloud, confirm or override.

Rationale for proposing Cloud rather than Minos: a waiver is a product-risk call — "ship this without
a design pass" — not a procedural check. Per the standing split in this company's process, product
and priority calls are Cloud's; procedural gate-keeping is Minos's. Minos enforces the gate; Cloud is
the only one who can decide a specific ticket doesn't need to clear it.

**How a waiver is recorded:** a plain comment from Cloud on the implementation ticket, stating
`Design gate waived: <reason>`, citing this doc. No separate form or tool — consistent with how other
one-line decisions are recorded in this company (e.g. the board's `"yeah do c for now"` ruling on
DAN-214). Minos checks for this comment, or a linked `done` design subtask, before approving any
gated-shape ticket into `in_review` → `done`. Absent either, Minos returns the ticket for a design
subtask or a waiver — it does not decide on its own which is appropriate.

## Enforcement point

No new tooling required. This rides the existing approval stage: when Minos reviews a ticket of a
gated shape (per the checklist above) for approval, it confirms one of:

1. a blocking/linked Oderisi design subtask with status `done`, or
2. a Cloud waiver comment on the ticket per the format above.

If neither is present, Minos requests changes: "needs a design subtask (DAN-818 gate) or a Cloud
waiver," not a silent approval or a silent block.

## Calibration against history (acceptance criterion 3)

Applied to the last 10 Hatate-Linux-Redux tickets closed `done` as of 2026-10-07 11:35 UTC:

| Ticket | Title | Shape match? | Why |
|---|---|---|---|
| DAN-782 | Stand-down report: Dante | No | process/meta, no UI |
| DAN-800 | Close out DAN-660 as a cross-issue write | No | process/bookkeeping |
| DAN-660 | GUI: `.run-banner` for unclean-shutdown recovery | **Yes, but exempt** | implements already-approved DAN-641 design |
| DAN-713 | Merge PR #101 (DAN-660 run-banner) | No | merge mechanics |
| DAN-794 | Reconciliation-held issues produce ZERO claimed runs | No | process diagnosis, no UI |
| DAN-796 | QA: verify review-pane 2x2 grid column stability | **Yes, but exempt** | conformance bug fix against DAN-787's existing spec |
| DAN-466 | Dispose of 50 orphaned routine ticks | No | process/ops |
| DAN-787 | Review-loop throughput pass: shortcut legend, stable layout | **Yes — this IS the design ticket** | design work itself, not an implementation missing one |
| DAN-785 | Stand-down report: Beatrice | No | process/meta |
| DAN-783 | Stand-down report: Minos | No | process/meta |

**Result: 3 of 10 ticket shapes match the trigger list (shapes 1-7; shape 8 was added in the DAN-892
reconciliation noted at the top of this doc, after this calibration ran, and none of the 10 would
have changed category under it, since none touched confirmation/undo/destructiveness); 0 of 10 were
an implementation ticket that
skipped a design step it should have had.** That 3/10 is a reasonable, non-degenerate hit rate — not
0 (the rule isn't vacuous) and not 10 (the rule isn't swallowing the whole backlog, most of which in
this window was legitimately process/ops firefighting, not product work). The zero-skips result is
expected: DAN-660 and DAN-796 both correctly trace back to a design ticket (DAN-641, DAN-787) that
already ran the gate in substance, just without a formal subtask — which is exactly the accidental
pattern DAN-818 exists to make a rule instead of a habit going forward.

**Caveat on the sample:** this 10-ticket window is dominated by platform/process firefighting
(stand-down reports, reconciliation-hold cleanup), not product feature work, because of what the
company was doing on 2026-10-07. A window drawn from a more feature-heavy period would likely show a
higher shape-match rate. The rule's calibration target is the *shape test*, not this particular
window's hit rate — re-run this table periodically if the mix of work changes materially.

## Residual risks / what this doesn't solve

- The gate only fires at Minos's approval checkpoint. A ticket that never reaches approval (e.g.
  abandoned, or merged via a bypassed path per the branch-protection decision's own admitted gap)
  isn't caught. This is the same convention-enforced-not-platform-enforced tradeoff the board already
  accepted for merge staleness (`ops/DECISION-branch-protection.md`).
- "Would a user notice" judgment calls (shape 1-8 vs. the exemptions) still require some taste from
  whoever triages a new ticket. The checklist narrows it, it doesn't eliminate it. Tickets that are
  genuinely ambiguous should default to **requiring** the subtask — cheap to skip via waiver, expensive
  to retrofit a design pass after code lands.
