#!/usr/bin/env bash
#
# Pre-flight guard for the shared checkout. Run this as the very first step
# of every agent heartbeat, before any edits or `git checkout`/`git reset`.
#
#   scripts/agent-preflight.sh                  # validate only (old behavior)
#   scripts/agent-preflight.sh <ticket-id> [branch-name]
#                                                # validate, then provision
#                                                # and print an isolated
#                                                # worktree for <ticket-id>
#
# It never force-resets, discards, or rewrites anything the agent didn't
# write itself this run. If the checkout is not safe to proceed from (dirty,
# or holding commits that exist nowhere but this local tree), it aborts
# loudly and leaves everything exactly as found. Escalate on abort — do not
# force past it. See CLAUDE.md for why: this guard exists because a shared
# checkout was left dirty and 20 commits stale, and the next run silently
# inherited that state instead of catching it (DAN-113).
#
# Before that check, this script also reclaims two sources of *harmless*
# leftover state that otherwise accumulate into false aborts over time
# (DAN-176, DAN-189, DAN-200, DAN-272):
#
#   - prunable `git worktree` administrative entries whose directory is
#     already gone (`git worktree prune` — touches no commits, no refs
#     with content)
#   - local branches that are provably redundant: either identical to a
#     remote-tracking branch that still exists (so the local ref carries
#     no information the remote doesn't also have), or — when that isn't
#     true, or the remote branch is gone — patch-identical to something
#     already on origin/main per `git cherry` (which is immune to the
#     "squash-merge leaves a different SHA" false positive that `git log
#     --branches --not --remotes` can't see through), or — when `git
#     cherry` still can't clear it — already shipped to origin/main under
#     a single *combined* squash commit per
#     `ops.ancestry_guard.branch_ship_verdict` (DAN-272: `git cherry`
#     compares each commit's own patch-id against origin/main one at a
#     time, so a multi-commit branch squashed into one PR merge commit
#     reports every commit as unmerged even though none of it is actually
#     missing — confirmed live on this repo's own
#     `agent/beatrice/DAN-193-rule34us-hydrus-checks` and
#     `agent/beatrice/DAN-217-is-ancestor-squash-fix`, both fully
#     squash-merged yet unclearable by `git cherry` alone). A branch none
#     of these three checks can clear is left alone and still fails the
#     abort check below by design — that is the "refuses to discard"
#     guarantee, not a bug to route around.
#
# DAN-200 also makes this the one mandatory place that installs the
# commit-blocking guard (scripts/githooks/pre-commit) via `core.hooksPath`,
# so running this script is what turns "please use a worktree" from a
# convention into an enforced one — including on a fresh clone that has
# never had the hook configured before.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

git fetch origin main --quiet

# --- reclaim harmless leftovers (never touches anything with content
#     that isn't also verifiably present elsewhere) -----------------------

git worktree prune

current_branch="$(git symbolic-ref --quiet --short HEAD || true)"
worktree_locked_dirty=()
worktree_admin_locked=()

# A branch that passes the patch/shipped checks below can still survive
# `git branch -D` because it's checked out in a *different*, abandoned
# linked worktree - `git branch -D` always refuses that, regardless of
# content. If that worktree is itself clean, the lock is pure leftover
# state (the agent that owned it never ran `git worktree remove` per
# CLAUDE.md) and safe to clear so the already-verified-shipped branch can
# finally go too. If it's dirty, leave both alone and say so by name in
# the abort message below (DAN-387) instead of letting it fall into the
# generic "commits not on any remote" line, which names the wrong cause.
#
# A clean worktree can still refuse `git worktree remove` on its own
# merits: `git worktree lock` (e.g. to protect an in-progress long-running
# run from accidental removal) blocks it independent of dirtiness
# (DAN-481). That failure must not fall through silently just because it
# took the "clean" branch above - name it too, distinctly from the dirty
# case, so the abort points at `git worktree unlock` instead of sending
# whoever's debugging it hunting for lost/unpushed work.
reclaim_locked_worktree() {
  local branch="$1" wt_path="" wt_branch=""
  while IFS= read -r line; do
    if [ -z "$line" ]; then
      wt_path=""
      wt_branch=""
      continue
    fi
    case "$line" in
      worktree\ *) wt_path="${line#worktree }" ;;
      branch\ *) wt_branch="${line#branch refs/heads/}" ;;
    esac
    if [ -n "$wt_path" ] && [ -n "$wt_branch" ] && [ "$wt_branch" = "$branch" ]; then
      if [ -z "$(git -C "$wt_path" status --porcelain 2>/dev/null)" ]; then
        if git worktree remove "$wt_path" >/dev/null 2>&1; then
          return 0
        fi
        worktree_admin_locked+=("$branch ($wt_path)")
        return 1
      fi
      worktree_locked_dirty+=("$branch ($wt_path)")
      return 1
    fi
  done < <(git worktree list --porcelain)
  return 1
}

for b in $(git for-each-ref --format='%(refname:short)' refs/heads/); do
  if [ "$b" = "main" ] || [ "$b" = "$current_branch" ]; then
    continue
  fi

  upstream="$(git rev-parse --abbrev-ref --symbolic-full-name "$b@{upstream}" 2>/dev/null || true)"
  gone=""
  if [ -n "$upstream" ]; then
    git rev-parse --verify --quiet "$upstream" >/dev/null || gone=1
  fi

  if [ -n "$upstream" ] && [ -z "$gone" ]; then
    # Remote still exists: the local ref is redundant the moment it holds
    # nothing the remote doesn't. `-d` only succeeds when that's true. If
    # it fails, the branch has diverged from its own upstream (e.g. it was
    # rebased/amended into a different branch that got merged instead) —
    # fall through to the same patch/content checks used below rather than
    # giving up on it just because an upstream ref happens to still exist.
    if git branch -d "$b" >/dev/null 2>&1; then
      continue
    fi
  fi

  # No upstream, upstream deleted on origin, or upstream present but
  # diverged: the next signal is patch-equivalence against origin/main.
  cherry="$(git cherry origin/main "$b" 2>/dev/null || true)"
  if [ -n "$cherry" ] && ! grep -q '^+' <<<"$cherry"; then
    if ! git branch -D "$b" >/dev/null 2>&1; then
      if reclaim_locked_worktree "$b"; then
        git branch -D "$b" >/dev/null 2>&1 || true
      fi
    fi
    continue
  elif [ -z "$cherry" ]; then
    git branch -d "$b" >/dev/null 2>&1 || true
    continue
  fi

  # git cherry couldn't clear it: ask ops.ancestry_guard's per-PR GitHub
  # merged-state signal, which already solved exactly this squash-merge
  # trap for the done-issue sweep (DAN-116/DAN-266) — see the file header
  # for why per-commit patch-id comparison alone misses a multi-commit
  # squash, and ops/ancestry_guard.py's branch_ship_verdict docstring for
  # why this intentionally checks the exact branch name on GitHub rather
  # than grepping for a ticket id (a ticket can have more than one round of
  # work, only some of which has shipped).
  if python3 -m ops.ancestry_guard branch-shipped . "$b" \
      --gh-bin "${AGENT_PREFLIGHT_GH_BIN:-gh}" >/dev/null 2>&1; then
    if ! git branch -D "$b" >/dev/null 2>&1; then
      if reclaim_locked_worktree "$b"; then
        git branch -D "$b" >/dev/null 2>&1 || true
      fi
    fi
  fi
  # Anything left (no shipped verdict, or delete refused e.g. checked out
  # in a linked worktree) is untouched and, if warranted, surfaces below.
done

# --- the actual guard: never force past this ----------------------------

dirty="$(git status --porcelain)"
unpushed="$(git log --branches --not --remotes --oneline)"

if [ -n "$dirty" ] || [ -n "$unpushed" ] || [ "${#worktree_locked_dirty[@]}" -gt 0 ] || [ "${#worktree_admin_locked[@]}" -gt 0 ]; then
  echo "ABORT: shared checkout is not safe to use." >&2
  if [ -n "$dirty" ]; then
    echo "  - working tree has uncommitted changes:" >&2
    echo "$dirty" | sed 's/^/      /' >&2
  fi
  if [ "${#worktree_locked_dirty[@]}" -gt 0 ]; then
    echo "  - branches are verified shipped but checked out in a dirty linked worktree" >&2
    echo "    (git branch -D refuses while checked out elsewhere; clean or remove" >&2
    echo "    that worktree with 'git worktree remove', then re-run):" >&2
    printf '      %s\n' "${worktree_locked_dirty[@]}" >&2
  fi
  if [ "${#worktree_admin_locked[@]}" -gt 0 ]; then
    echo "  - branches are verified shipped but checked out in a worktree that is" >&2
    echo "    clean but administratively locked (git worktree remove refuses while" >&2
    echo "    locked, regardless of dirtiness; run 'git worktree unlock' on it, then" >&2
    echo "    re-run):" >&2
    printf '      %s\n' "${worktree_admin_locked[@]}" >&2
  fi
  if [ -n "$unpushed" ]; then
    echo "  - local branches have commits not on any remote:" >&2
    echo "$unpushed" | sed 's/^/      /' >&2
  fi
  echo "Do not proceed and do not force past this (no reset/checkout -f/clean)." >&2
  echo "Isolate your own work in a worktree (see CLAUDE.md) and escalate this" >&2
  echo "checkout's state before anyone touches it further." >&2
  exit 1
fi

git checkout main --quiet
git pull --ff-only origin main --quiet

# Self-install the commit-blocking guard every run so it self-heals even
# on a checkout that has never run this script before.
repo_root="$(git rev-parse --show-toplevel)"
git config core.hooksPath "$repo_root/scripts/githooks"

echo "OK: shared checkout is on main, clean, and up to date with origin/main."

# --- optional: provision the isolated worktree in the same step ---------

ticket="${1:-}"
if [ -n "$ticket" ]; then
  branch="${2:-agent/run/${ticket}}"
  dest_root="${PAPERCLIP_RUN_SCRATCH_DIR:-/tmp}/worktrees"
  dest="${dest_root}/${ticket}"
  mkdir -p "$dest_root"
  git worktree add "$dest" -b "$branch" origin/main
  echo "OK: isolated worktree ready at $dest (branch $branch)."
  echo "cd \"$dest\" and do the run's work there — never back in $repo_root."
fi
