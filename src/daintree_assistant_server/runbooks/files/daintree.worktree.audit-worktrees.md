---
id: daintree.worktree.audit-worktrees
title: Audit worktrees for merged, stale or at-risk work
summary: Classify every worktree on evidence — merged, closed unmerged, in use, holding uncommitted or unpushed work, parked, behind its base — and recommend keep, finish or remove for each, changing nothing.
whenToUse: 'Use when the user wants to know what STATE their worktrees are in and which ones they could let go — "which worktrees can I get rid of?", "which of these are already merged?", "does anything have unpushed or uncommitted work?", "I have 15 worktrees, what are they all?", "which worktrees are way behind main?", "audit my worktrees". The deliverable is a read-only verdict per worktree (class, evidence, recommendation) plus a proposed removal set the user can approve; nothing is deleted, pruned, pulled or rebased. Do not use when the user orders worktrees closed, removed or deleted, merged ones included (use daintree.orchestration.workspace-cleanup), to triage the agents running now (use daintree.orchestration.supervise-fleet), to reconstruct what was in flight in earlier sessions (use daintree.orchestration.status-and-resume), to bring a branch up to date with its base (use daintree.git.sync-with-base), to compare what several worktrees changed or forecast merge conflicts between them (use daintree.worktree.compare-worktrees), to delete merged or abandoned branches that have no worktree (use daintree.git.prune-stale-branches), for a plain list of which worktrees exist (use daintree.foundation), or to get back work already lost from a removed worktree or deleted branch (use daintree.git.recover-lost-work).'
risk: read
requiredTools:
  - worktree.list
  - terminal.list
optionalTools:
  - terminal.getStatus
  - forge.listPRs
---
Read-only, and yours to do directly: it is Daintree state and git reads. One `git fetch --quiet origin` from the main checkout is allowed (it moves only remote-tracking refs, and "behind" and "merged" are wrong without it). Never run `git fetch --prune`, `git worktree prune`, pull, rebase, stash, checkout, reset or any delete here.

On a host: shell steps via its command runner, or delegate; own shell: `gh -R <repo>`.

1. Inventory: `worktree.list`. Skip the `isMain` row. Keep per row `id`, `path`, `branch` (null is a detached HEAD), `isActive`, `issueNumber`, `prNumber`, `setupState`, and `status`: `active` means uncommitted changes and `stale` means clean with no commit for over a week. Both are hints, not classes. Then `terminal.list` twice, with no filter and with `location:"background"` (the unfiltered call omits background panels, which still run), grouped by `worktreeId`.
2. Git evidence, per worktree, in its own path (no approvals needed):
   - `git -C <path> status --porcelain=v2 --branch`: changed and untracked (`?`) files, and unmerged entries (`u` lines) meaning conflicts.
   - An operation in progress: any of `rebase-merge`, `rebase-apply`, `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `BISECT_LOG` existing at `git -C <path> rev-parse --path-format=absolute --git-path <name>` (never join paths to the git dir yourself).
   - Commits on no remote-tracking ref: `git -C <path> rev-list --count HEAD --not --remotes`. Refs are not pruned, so a branch deleted on the remote still hides its commits: before calling a branch pushed, `git -C <path> ls-remote --heads origin <branch>` must return it.
   - Behind and ahead of its base (the PR's base if known, else `git -C <path> symbolic-ref --short refs/remotes/origin/HEAD`, which prints `origin/<name>`): `git -C <path> rev-list --left-right --count <base ref>...HEAD`.
   - Last commit date: `git -C <path> log -1 --format=%cI`.
3. Merged-ness comes from the forge, never `git branch --merged`: squash and rebase merges never show there. A host's worktree: `gh -R <owner/repo>` of its `origin`. A row with `prNumber`: `gh pr view <n> --json state,baseRefName,headRefOid,mergedAt`. Otherwise `gh pr list --state all --head <branch> --json number,state,baseRefName,headRefOid,mergedAt` (a reused branch name gives several rows: the newest decides). A merged PR covers the worktree only if `git -C <path> merge-base --is-ancestor HEAD <headRefOid>` exits 0; exit 1 means commits after the merge (at risk), an error means unknown. Without `gh`: `forge.getPR` for a `prNumber`, else `forge.listPRs` `"state":"all"` matched on `headRef`; neither gives a head SHA, so say later commits went unchecked.
4. Live use: `terminal.getStatus` once over the agent terminals (`agentId` set) found in step 1. `working` or `waiting` means in use whatever git says. Any open terminal in a worktree, background included, is noted: removing the worktree would strand it. A failed terminal read makes live use unknown.
5. Class, first match wins, then every other flag that applies (dirty or untracked counts with up to five paths, commits on no remote, operation in progress, behind count, `setupState` `failed` or `needs-approval`, "the worktree you are looking at" for `isActive`):
   - In use: an agent working or waiting.
   - At risk, operation in progress: conflicts, or a rebase, merge, cherry-pick or bisect underway.
   - At risk, would lose work: uncommitted or untracked files, commits on no remote, or commits after a merged PR.
   - Merged: a forge read says the PR merged and HEAD is inside its head. A removal candidate. Without a readable forge (no GitHub remote, every forge read fails) nothing is Merged: HEAD being an ancestor of the base is a hint you report on a Parked or Unknown row.
   - Closed unmerged: a closed, unmerged PR; the user decides; it may be abandoned or superseded.
   - In review: an open PR.
   - Parked: clean, pushed, no PR, with its last commit date. No PR is not abandoned.
   - Unknown: a read failed; say which read.
6. Stop. Propose the removal set: only rows where every read succeeded and none is flagged: merged, clean, nothing on no remote, no open terminals. For every other row name the check that blocks it and what would have to happen first (commit and push, finish the rebase, close the terminal). Removal is daintree.orchestration.workspace-cleanup on the user's word; offer it once and do not start it.

```json tool=terminal.list
{"location":"background"}
```

Report back: one line per worktree (id, branch, issue or PR, class and flags, the evidence behind them, recommendation), the proposed removal set, what would be lost if the at-risk ones were removed, whether the fetch ran, and any read that failed.
