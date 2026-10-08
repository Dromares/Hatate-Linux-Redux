# Decision: branch protection stays off; the in-repo staleness gate is the enforcement mechanism

**Status:** accepted, 2026-10-03 (board ruling on DAN-214, carried out under DAN-213).

## Background

GitHub's "require branches to be up to date before merging" would prevent the
collision DAN-197 was written to stop: two individually-green PRs that break
`main` on contact because each was tested against a `main` that has since
moved. That setting lives under branch protection, which this repo cannot
turn on:

```
$ gh api repos/Dromares/Hatate-Linux-Redux/branches/main/protection
403 "Upgrade to GitHub Pro or make this repository public."
```

The repo is private, on GitHub's free tier. Branch protection rules are a
Pro/Team/Enterprise feature for private repos.

## Options considered (DAN-214)

- **(a) Upgrade to GitHub Pro.** Turns branch protection on, including the
  "up to date" check, enforced by GitHub itself rather than by convention.
  Costs money. Board-owned; not decided against, deferred.
- **(b) Make the repo public.** Also unlocks branch protection on the free
  tier. Rejected implicitly — the repo stays private; disclosure was never
  on the table.
- **(c) Accept the gap and enforce in-repo.** Build the staleness check into
  the merge path itself (CI signal + a merge wrapper that makes the check
  structurally inseparable from the merge action), and accept that this is
  convention-enforced rather than platform-enforced: an agent who calls
  `gh pr merge` directly instead of `scripts/merge_pr.sh` skips the gate.

## Ruling

**Option (c), for now.** Verbatim, `local-board`, 2026-10-03: *"yeah do c
for now."*

The in-repo gate (`scripts/check_merge_base.sh`, the `stale-base` CI job in
`.github/workflows/tests.yml`, and `scripts/merge_pr.sh` as the one sanctioned
merge path — see `CLAUDE.md` "Before merging") is the sanctioned enforcement
mechanism for this repo, not a stopgap while waiting for a plan upgrade. Do
not design around branch protection arriving later. If the plan changes in
the future, branch protection becomes a belt-and-braces addition on top of
this gate, not a replacement for it.

The residual weakness — the gate is convention-enforced, not impossible to
bypass — is accepted knowingly, not an oversight. The board owns that
trade. Do not re-propose (a) or (b) off your own initiative; that revisit is
Cloud's to make, and only on the basis of counted occurrences (see below).

## Occurrence log

An **occurrence** is a stale-base collision that reaches `main` *despite*
the gate — i.e. the gate failed to catch it (missed, bypassed, or caught too
late to prevent the merge). A collision the `stale-base` job or
`scripts/merge_pr.sh` correctly catches and blocks is the gate working as
designed; it is not an occurrence and does not get a row here.

When an occurrence happens: append a row below, then file a ticket titled
`Branch protection occurrence #<n> — re-propose GitHub Pro`, assigned to
Cloud.

| # | Date | What happened | PR(s) | Ticket filed |
|---|------|----------------|-------|---------------|
| _(none yet)_ | | | | |
