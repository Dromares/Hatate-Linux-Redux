# Agent Git Workflow

This repo is worked on by multiple autonomous agent runs, often concurrently,
against a shared clone. Follow this convention on every run — it is not
optional, and it is cheap.

## Why this exists

On [DAN-74](https://github.com/Dromares/Hatate-Linux-Redux) two agents ran
against the same shared checkout at once. One agent's activity reset
`agent/dante/DAN-74` to its parent commit, repointed `agent/dante/DAN-72` at
the wrong commits, and left eight unrelated files modified in the tree. The
branch's *base* was correct — both agents had branched from `origin/main` —
but the branch still moved underneath a live run, because both agents shared
one working directory, index, and `HEAD`. Branching-from-`origin/main`-and-
recording-the-base-SHA protects the base; it does not protect a branch that's
checked out in a tree another run can also touch. Only one thing does:
**never let two runs share a working tree.**

## The rule — and why it no longer depends on you remembering it

**Before making any change, create an isolated `git worktree` for this run.
Never `git checkout`, `git reset`, `git stash`, or commit directly in the
shared/primary checkout.** Treat the primary checkout as shared, read-mostly
state — other runs may have it parked on an unrelated branch with uncommitted
changes at any time; do not disturb it.

This paragraph is not the enforcement mechanism. From DAN-82 (2026-09-29)
through DAN-189 (2026-10-02) it *was* — this doc, asking nicely — and that
failed seven times in four days ([DAN-200](https://github.com/Dromares/Hatate-Linux-Redux)
has the full account). As of DAN-200, a `pre-commit` hook
(`scripts/githooks/pre-commit`, installed via `core.hooksPath` by the
pre-flight guard below) makes `git commit` **fail outright** when run
directly in the primary checkout, regardless of whether anyone read this
file first. Follow the rule anyway, because the hook only catches the
commit — it does nothing about a `git checkout` to another branch quietly
overwriting a file another run left edited-but-uncommitted in the shared
tree. The structural guarantee and the convention are complementary, not a
replacement for one another.

## Step zero: run the pre-flight guard

Before doing anything else — before `EnterWorktree`, before `git worktree
add` by hand, before reading files to plan your change — run:

```bash
scripts/agent-preflight.sh
```

from inside the shared checkout. This is mandatory, not optional, on every
run. It fetches `origin/main`, reclaims harmless leftover state (merged
local branches, dead worktree admin entries — see the script's own
comments for exactly what "harmless" means here and why), and then checks
the shared checkout for the two ways it has actually broken in this repo's
history: a dirty working tree, or local commits that exist on no remote
branch. If either is true, it **aborts loudly and changes nothing** — it
never force-resets, discards, or rewrites anything an agent actually wrote,
on purpose, because a guard that "fixes" a bad state by discarding it is
the thing that would have destroyed unpushed work the one time this
actually happened (DAN-113, recovered under DAN-112). If it aborts:

- **Stop. Do not proceed, and do not force past it** (`git reset --hard`,
  `git checkout -f`, `git clean`, `git stash` are all off the table here).
- Escalate — comment on the current issue describing exactly what the guard
  reported, and treat the shared checkout as untouchable until a human or
  the affected agent resolves it.

Only once the guard prints `OK` — meaning the shared checkout is clean, on
`main`, and synced with `origin/main` — move on to isolating your own work
below. The guard syncing the shared checkout to `main`, and installing the
commit-blocking hook described above, are the only mutations it performs,
and both happen only after confirming there is nothing to lose.

The guard also doubles as a one-shot worktree provisioner for any adapter
that isn't a Claude Code session (see below): pass a ticket id (and
optionally a branch name) and it creates the isolated worktree for you in
the same step, instead of leaving that as a second, skippable action:

```bash
scripts/agent-preflight.sh DAN-123                       # branch: agent/run/DAN-123
scripts/agent-preflight.sh DAN-123 agent/<name>/DAN-123  # explicit branch name
```

### Claude Code sessions (this project's normal adapter)

Use the built-in `EnterWorktree` tool before touching any files:

```
EnterWorktree(name: "<ticket-id>")   # e.g. "DAN-82"
```

This creates a fresh worktree under `.claude/worktrees/`, branches it from
`origin/<default-branch>`, and switches the session into it. Do the run's
work there. At the end of the run call `ExitWorktree` with `action: "keep"`
if the branch should survive for review/handoff, or `action: "remove"` once
it's merged or abandoned.

### Any other adapter, or scripted/manual git use

Create the worktree by hand, rooted under the run's own scratch directory
(`$PAPERCLIP_RUN_SCRATCH_DIR` for Paperclip runs; any run-private, run-cleaned
directory otherwise) — **not** inside the shared checkout:

```bash
cd <shared-checkout>
git fetch origin main --quiet
git worktree add "$PAPERCLIP_RUN_SCRATCH_DIR/worktrees/<ticket-id>" \
  -b agent/<name>/<ticket-id> origin/main
cd "$PAPERCLIP_RUN_SCRATCH_DIR/worktrees/<ticket-id>"
```

`git worktree add` only reads the shared checkout's `.git`; it does not
touch the shared checkout's current branch, index, or working files. Do all
further work — edits, commits, `push`, `push --force-with-lease` — from
inside that isolated directory, never from the shared checkout.

Rooting the worktree under the run's scratch dir means Paperclip removes the
directory when the run ends, with no separate cleanup step for the common
case. If the run's branch needs to survive past that (open PR, handoff to
another run), record the worktree path in a task comment before the run
exits, since the directory disappears once the scratch dir is reclaimed —
the pushed branch on `origin` is what persists.

## Cleanup

A worktree that's removed by deleting its directory (rather than
`git worktree remove`) leaves a stale, `prunable` entry in `git worktree
list` forever. Before adding a new worktree, or periodically, run:

```bash
git worktree prune
```

When you're done with a worktree you created by hand and its branch has
been pushed/merged and is no longer needed locally, remove it properly:

```bash
git worktree remove "$PAPERCLIP_RUN_SCRATCH_DIR/worktrees/<ticket-id>"
```

Add `--force` only if you intend to discard uncommitted local changes in
that worktree; confirm that's intended before using it.

## Pushing

`git push --force-with-lease` is only safe to run from inside the isolated
worktree that owns the branch. `--force-with-lease` protects against the
*remote* having moved unexpectedly since your last fetch — it does nothing
to protect a branch that's checked out in a working tree another local run
can also touch. Isolation is what makes force-with-lease from an agent run
safe at all.

## Before merging

Branch protection's "require branches to be up to date before merging" is
unavailable on this repo's GitHub plan (private, free tier —
`gh api repos/Dromares/Hatate-Linux-Redux/branches/main/protection` returns
403). That means GitHub itself will let you merge a PR whose branch point
predates commits that have since landed on `main` — two individually-green
PRs can still break `main` on contact this way. Nothing stops that except
you.

The CI workflow runs `scripts/check_merge_base.sh` as a `stale-base` job on
every PR, so a stale branch shows red before you even look at merging it.
That catches the common case, but CI only re-runs when you push — if `main`
moves *after* your last green run and before you click merge, the PR can
still be green on a base that's no longer current.

A documented "run the script by hand first" step is the same failure class
this gate exists to close — it still merges if the reminder gets skipped.
So **the merge itself goes through `scripts/merge_pr.sh`, never a bare
`gh pr merge` or the merge-pull-request tool called directly**:

```bash
scripts/merge_pr.sh <pr-number> [extra gh pr merge args, e.g. --squash]
```

It re-resolves the PR's current head SHA, runs `check_merge_base.sh` against
it, and only calls `gh pr merge` if that passes — defaulting to `--merge` to
match this repo's existing history (`git log --merges`). A `FAIL` aborts
before anything is merged: merge/rebase `main` into the branch, let CI go
green again, and re-run `scripts/merge_pr.sh`. Going around the wrapper to
merge directly is a deliberate bypass, not an oversight, and should be
treated as one. See [DAN-213](https://github.com/Dromares/Hatate-Linux-Redux)
and both scripts' own headers for the full context.

## Still true, and complementary

The standing rule — branch from `origin/main` and record the base SHA you
read in the first task comment — still applies and still matters: it's what
keeps a run's diff reviewable and its starting point traceable. Worktree
isolation is a separate, additional guarantee: it's what keeps one run's
`HEAD`, index, and working files from being moved by another run while both
are live. Two runs on different branches, each in its own worktree, cannot
move each other's refs or dirty each other's tree — each has an independent
`HEAD` and index, and a ref update only repoints the branch being updated,
never another worktree's checked-out branch. Use both rules together.

# Status for finished-but-unmerged work

"Completed" on this board means verified on `origin/main` — not attempted, not
merged-according-to-a-comment (see `scripts/verify_closed.sh`, DAN-187). So
when your PR is open, green, and ready, but you are correctly declining to
self-merge or self-approve your own work, **`done` is still the wrong status**
— the code is not on `main` yet. This exact gap produced two false `done`s,
DAN-207 and DAN-208.

**Set `in_review` instead**, and in the same comment:

- Name who merges (a specific agent, e.g. "Virgil merges, not the author" —
  DAN-220), or open a `request_confirmation` interaction resolvable by anyone
  with merge rights (DAN-208's approach reached a human for both DAN-192 and
  DAN-193).
- Link the PR.
- Only close as `done` once a merge SHA is on `origin/main`, and name that SHA.

Don't reach for `blocked` here — it reads as a first-class blocker with no
owner and just stalls a finished PR. Don't leave it `in_progress` either — that
implies you still have work left. Neither of those is true: your part is done,
you're handing off a merge, and `in_review` + a named owner says exactly that.
This does not change the self-merge refusal — that stays correct. The fix is
routing the handoff explicitly, not pressuring anyone into merging their own
work.
