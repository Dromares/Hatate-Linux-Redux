# Runbook: backup GitHub token (`GITHUB_BACKUP_TOKEN`)

Decision: Cloud, Tier 2, [DAN-1143](/DAN/issues/DAN-1143). Built on [DAN-1145](/DAN/issues/DAN-1145).
Secret: `Github Paperclip access token`, key `github-paperclip-access-token`, id `3f28d6c9-5b09-49f3-92d0-4bfda97bdccd`.
Bound as `adapterConfig.env.GITHUB_BACKUP_TOKEN` on **Virgil, Dante, Beatrice only** (live status per seat below).

**Status (2026-10-09, all three seats bound).** Seat bindings, each read from the server's own interaction record:
- **Dante: LIVE.** Proposal `609e04f0`, interaction `4c212c5f` `accepted`, `secretProposal` `executed`; `test -n "$GITHUB_BACKUP_TOKEN"` true on this seat. §5 passed read-only (`git ls-remote` rc=0, `gh api` returned `push: true`).
- **Virgil: bound.** Proposal `8e46f72f`, interaction `4b38c8d3` (DAN-1148) `accepted`, `secretProposal` `executed` at 16:26:51Z. (First proposal `04c11c85` on DAN-1146 expired unseen; superseded.)
- **Beatrice: bound.** Interaction `967b8e6c` (DAN-1149) `accepted`, `secretProposal` `executed` at 16:31:10Z. (First proposal `d2ee2bed` on DAN-1147 expired unseen; superseded.)

Each seat can confirm with `test -n "$GITHUB_BACKUP_TOKEN"` (never print it). Virgil's and Beatrice's live env was not read by Dante; the evidence is the executed proposals.

## 0. Principle

The managed broker identity is **primary**. The backup is used **only** after the probe in §1 returns HTTP 200 with `status: "unavailable"`. Using it any other time silently bypasses the managed identity and its audit trail.

## 1. Probe (always first; paste, do not retype)

```sh
curl -s -X POST "${PAPERCLIP_GITHUB_BROKER_URL%/}/runtime-tools/github/credentials" \
  -H "authorization: Bearer $PAPERCLIP_API_KEY" \
  -H "x-paperclip-github-capability: $PAPERCLIP_GITHUB_BROKER_TOKEN" \
  -H 'content-type: application/json' -d '{}' -w 'HTTP_STATUS=%{http_code}' \
  | python3 -I -c "import sys,json;t=sys.stdin.read();b,_,c=t.partition('HTTP_STATUS=');d=json.loads(b);print({k:v for k,v in d.items() if k!='env'},'HTTP',c)"
```

The `env` block holds live tokens, so the filter above drops it. **Never print the raw response.**

## 2. Decide

| Probe result | Verdict | Action |
|---|---|---|
| 200 `status: "available"` | healthy | Use normal `git`/`gh`. **Do not use the backup.** |
| 409 | lease collision (another run holds the identity) | Retry; the shim retries 30x at 1s. **Not a reason to use the backup.** |
| 401 / 403 | malformed request (wrong header slot) | Fix the two-header call. **Not a reason to use the backup.** |
| connection refused / timeout | broker down | Not "unavailable". Retry, escalate. The backup is not authorised for this case. |
| 200 `status: "unavailable"` | no managed identity | **Fallback permitted** (§3, §4). Say so in the ticket comment (probe result and time), and flag the board that the primary needs re-auth. |

`status` flips: three times on 2026-10-07 alone. Re-probe every time; never carry a recorded value forward.

## 3. Fallback: git (push / fetch / ls-remote)

The launcher shim, when the identity is unavailable, runs the real `git` with no credentials, so the shim stays on PATH. The credential is supplied by a **one-shot helper**, scoped to a single command. It reads the env var at call time, so the value is not in argv, not in the remote URL, and not written to `.git/config`.

```sh
# empty -c credential.helper= resets any inherited helper list first
git -c credential.helper= \
    -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GITHUB_BACKUP_TOKEN"; }; f' \
    push origin HEAD:refs/heads/agent/<name>/<ticket>
```

Worktree rules from CLAUDE.md are unchanged (work in your isolated worktree, `push --force-with-lease` only from the owning worktree).

## 4. Fallback: `gh` and REST

```sh
GH_TOKEN="$GITHUB_BACKUP_TOKEN" gh api user --jq .login      # scoped to this single command
GH_TOKEN="$GITHUB_BACKUP_TOKEN" gh pr create ...
curl -s -H "authorization: Bearer $GITHUB_BACKUP_TOKEN" -H 'accept: application/vnd.github+json' https://api.github.com/repos/Dromares/Hatate-Linux-Redux
```

Prefix the variable inline for each command. Do **not** `export GH_TOKEN`, and do not `gh auth login --with-token` (it persists the token to disk).

### Merging in fallback: use `scripts/merge_pr_backup.sh`

Merges still go through `scripts/merge_pr.sh` (policy buckets, author/executor rules (DAN-220) and approval gates are unaffected: they key on agent seats, not GitHub logins). **Do not run it as `GH_TOKEN="$GITHUB_BACKUP_TOKEN" scripts/merge_pr.sh N`**, and do not hand-assemble a `bash -c '...'` one-liner. Use the wrapper (DAN-1284), after the section-1 probe returns `unavailable`:

```sh
scripts/merge_pr_backup.sh N [extra merge_pr.sh args, e.g. --squash]
```

It runs `merge_pr.sh` unmodified, staleness gate included, and exits with its status (0 merged, 1 stale, 2 usage/setup, else `gh`'s). It fails closed with exit 2 if `GITHUB_BACKUP_TOKEN` is unset or if it cannot find a real `gh`/`git` outside the launcher shim. What it works around, each measured:

1. The launcher `gh`/`git` shims strip `GH_TOKEN`, `GITHUB_TOKEN` and `GIT_CONFIG_*` before running the real binary, so the inline prefix never reaches `gh` (it fails with `gh auth login` / "populate the GH_TOKEN environment variable").
2. The run's `BASH_ENV` hook (`$PAPERCLIP_GITHUB_LAUNCHER_DIR/.bashrc`) **overwrites `PATH` wholesale** in every non-interactive bash, including `merge_pr.sh`, so the shim is first again inside the script. (Same `BASH_ENV` family as DAN-890.) The wrapper removes the launcher directory from `PATH` and unsets `BASH_ENV`.
3. `merge_pr.sh` resolves the PR head SHA through `gh`, then asks `git` about it. In a clone that never fetched the head, git answers `fatal: Not a valid commit name <sha>` and the script then **misreports the PR as stale** (`ABORT: PR #N is stale`). This is what broke the older `bash -c 'unset BASH_ENV; PATH=/usr/bin:$PATH GH_TOKEN=... scripts/merge_pr.sh N'` form on DAN-1283: it only appeared to work (DAN-1196, DAN-1218) where the head object was already local. The wrapper first runs `git fetch origin refs/pull/N/head`. Reproduced in a `--depth 1` clone on DAN-1284: old form aborts with the fatal above; wrapper reaches the real staleness verdict.
4. `check_merge_base.sh` runs `git fetch`, which can need credentials (anonymous fetch worked on 2026-10-10, but do not depend on that). The wrapper passes the section-3 one-shot credential helper via `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` environment variables, which reach the real `git` only because the shim is bypassed.

The token is never echoed, never in argv, a URL or git config, and never exported: `GH_TOKEN` is a prefix assignment on the final `exec`, and the git helper reads `$GITHUB_BACKUP_TOKEN` from the environment at call time. Verified with a stub `gh`: it saw `GH_TOKEN` set (length only printed), `BASH_ENV` unset, and no occurrence of the token in its own or its parent's `/proc/*/cmdline`; the clone's `.git/config` held no credential entry.

Plain `gh` commands (`gh pr create ...`) have the same shim problem. The wrapper is merge-only; for anything else use the same two moves by hand, one command at a time: `env -u BASH_ENV PATH=<PATH minus the launcher dir> GH_TOKEN="$GITHUB_BACKUP_TOKEN" gh ...` (the prefix assignment sits outside any quotes that would expose the value).

## 4a. Verify a merge SHA without any identity (approvers: Minos, Oderisi)

Approvers never need the backup token (section 6). When the probe says `unavailable`, verify a merge with **anonymous** reads. The repo is public to unauthenticated reads. Both paths are read-only and cannot write. Use them instead of escalating "I cannot verify while GitHub is unavailable" (CEO verified `b4cd5bc` on DAN-1283 this way; REST path also verified on DAN-1196 and DAN-1218).

**Git, no token, no `gh`.** Use the real binary directly so the shim's "unavailable" banner does not matter (`/usr/bin/git`), in a scratch directory:

```sh
URL=https://github.com/Dromares/Hatate-Linux-Redux.git
git ls-remote $URL refs/heads/main refs/pull/N/head       # is main at/after the merge? PR head SHA?
git init -q verify && cd verify
git fetch -q --no-tags $URL refs/heads/main:refs/remotes/origin/main refs/pull/N/head:refs/remotes/pull/N
git cat-file -t <merge-sha>                                        # expect: commit
git merge-base --is-ancestor <merge-sha> origin/main && echo on-main
git rev-list --parents -n1 <merge-sha>                             # sha, first parent (base main), second parent (PR head)
git rev-parse pull/N                                               # must equal the second parent
git diff --stat <merge-sha>^1 <merge-sha>                          # first-parent diff = exactly what the PR landed
```

Check that the second parent equals the PR head SHA you approved, and that the first-parent diffstat matches the PR's declared diffstat. For a merge commit this is the full effect of the merge; for a squash the parent list has one entry, so compare `git diff --stat <sha>^ <sha>` instead. `git fetch <url> <merge-sha>` by raw SHA also worked anonymously on 2026-10-10, but fetching refs is the form that also proves the SHA is on main.

**REST, no token.** Four calls cover pulls, commits, compare and check-runs:

```sh
R=https://api.github.com/repos/Dromares/Hatate-Linux-Redux
curl -s $R/pulls/N | python3 -I -c "import sys,json;d=json.load(sys.stdin);print(d['merged'],d['merge_commit_sha'],d['head']['sha'])"
curl -s $R/commits/<merge-sha> | python3 -I -c "import sys,json;d=json.load(sys.stdin);print(d['sha'],[p['sha'] for p in d['parents']])"
curl -s $R/compare/main...<merge-sha>        # merge SHA is on main when status is identical/behind
curl -s $R/commits/<head-sha>/check-runs     # dedupe per gate, see AGENTS.md CI evidence rules
```

- No `Authorization` header, no token of any kind.
- REST rate limit is **60 requests/hour per IP**; check `x-ratelimit-remaining` (`curl -sI`) if you are sweeping. Budget a verification at about 4 calls. The git path is not subject to that limit.
- Anonymous reads cannot see anything private; if a call 404s on something that should exist, say so rather than reaching for the backup token.

## 5. Read-only exercise (no throwaway pushes)

```sh
git -c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GITHUB_BACKUP_TOKEN"; }; f' \
    ls-remote https://github.com/Dromares/Hatate-Linux-Redux.git HEAD
GH_TOKEN="$GITHUB_BACKUP_TOKEN" gh api repos/Dromares/Hatate-Linux-Redux --jq '.permissions'
```

## 6. Never

- Never echo, log, `set -x`, commit, paste into a comment/document, or screenshot the value. Never `env`/`printenv` unfiltered.
- Never bind or export it as `GH_TOKEN`, `GITHUB_TOKEN` or `GIT_TOKEN` (tools would pick it up automatically and bypass the managed identity on every run).
- Never put it in a remote URL (`https://x:TOKEN@github.com/...`) or `git remote set-url`; those persist into `.git/config`.
- Never `git config` / `--global` / `credential.helper store` / `gh auth login` with it; the helper is per-command only.
- Never use it as default, "to save a probe", or on 409/401/403/broker-down.
- Never request it for Minos (approver; no git writes, DAN-235), Oderisi (repo read-only, DAN-921) or Cloud (backstop only; add later only if the chain falls through for this reason).
- Do not rotate or revoke it from an agent. That is a board action.

## 7. Expiry

Measured 2026-10-09: the token is a **classic PAT** (`ghp_` prefix) authenticating as **`Dromares`**, and `GET /user` returns **no** `github-authentication-token-expiration` header, i.e. it has no expiry date. Nothing lapses on a clock, but it can still be revoked or rotated by the board at any time, so a backup that has silently died is only discovered during an outage. Re-run §5 periodically (e.g. whenever the probe flips to `unavailable`, before relying on it). Scope note: the token carries very broad scopes (`repo`, `workflow`, `delete_repo`, `admin:org`, `admin:enterprise`, ...). Use it only for the narrow actions in §3/§4.
