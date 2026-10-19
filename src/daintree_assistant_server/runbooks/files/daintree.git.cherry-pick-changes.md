---
id: daintree.git.cherry-pick-changes
title: Backport or cherry-pick commits onto another branch
summary: Apply exactly the chosen commits (or another worktree's uncommitted work) onto one or more target branches — backports to release branches or a fix ported between worktrees — adapted to each target's code, verified there, with backport PRs only when asked.
whenToUse: 'Use when particular commits or a fix must be applied onto a DIFFERENT branch without merging everything else — "backport #512 to release/2.4 and 2.3", "cherry-pick this fix onto the LTS branch", "take the fix Claude made in the hotfix worktree and put it on my feature branch", "port commit abc123 into develop''s worktree", "get the security fix onto every supported release". The deliverable is, per target, the change applied with provenance, conflicts resolved against that target''s code, its own checks run, and one backport branch and PR per release target when asked; the source untouched. Do not use to bring a branch up to date with its base (use daintree.git.sync-with-base); to combine several agents'' whole branches into one (use daintree.git.integrate-agent-branches); to open a PR for ordinary finished work (use daintree.workflow.prep-branch-for-review); to merge the resulting PRs (use daintree.forge.land-pull-request); to move a running agent to another worktree (use daintree.worktree.relocate-agents); to reverse an agent''s commits in place (use daintree.git.undo-agent-changes); or to lift the worthwhile part of a failed attempt onto a fresh branch (use daintree.orchestration.salvage-failed-attempt).'
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.sendCommandOwned
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You pin the change, prepare targets and verify; an agent in each target's worktree applies it.

On a host: shell steps and checks (`project.runCheck`) run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Pin the exact source, oldest first, after `git fetch origin` (only a pushed source reaches another machine):
   - Named commits: each to a full SHA (`git rev-parse <c>^{commit}`), never its ancestry. An explicit range: `git rev-list --reverse --topo-order <A..B>` (excludes A).
   - A PR: `gh pr view N --repo <r> --json state,mergeCommit,commits`. Unmerged: ask. A merge commit (two parents in `git show --no-patch --format=%P`): pick it with `-m 1`. One parent: one PR commit, pick it; several, a rebase merge if the N commits ending at it carry the PR's subjects in order, else a squash (pick the one). Unclear: ask for exact SHAs.
   - Another worktree's uncommitted work: its agent not `working` or `waiting` (`terminal.list`). List its untracked files (`git -C <src> ls-files --others --exclude-standard`; a patch misses them) and ask whether they come too.
2. Targets: only those named; "every supported release" without a list: ask. Freeze each target's tip H0: an existing worktree's HEAD, or `git rev-parse origin/<target>`. Skip a commit already there (`git merge-base --is-ancestor <sha> <H0>` exits 0); a `git log <H0> --grep=<sha>` hit is a likely `-x` pick, not proof.
3. Where, per target:
   - The user's branch in an existing worktree: only if `git status` is clean and no agent there is `working` or `waiting`, else ask. Its own idle agent is briefed by `terminal.sendCommandOwned`.
   - A release backport: a new worktree, then `worktree.waitUntilReady` (at most three calls; only `ready` proceeds):

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"backport/<N>-to-<target>","baseBranch":"origin/<target>","fromRemote":true,"collisionPolicy":"error"}}
```

4. Brief with `notify:true, handback:true`, launching where no agent is reused:

```json tool=agent.launch
{"agentId":"claude","name":"Backport: <N> to <target>","worktreeId":"<target worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief carries the target path and branch, the SHAs oldest first (`-m 1` on a merge commit), and: "One commit at a time: `GIT_EDITOR=true git cherry-pick -x <sha>`. Resolve conflicts against THIS branch's code: keep its lines and add only what `git show <sha>` changes; a line the fix does not touch keeps this branch's wording (never the source's newer message, name or helper), never a side wholesale. Then `git diff --check`, no `<<<<<<<`, `git add -- <paths>`, `GIT_EDITOR=true git cherry-pick --continue`. Empty pick: confirm the change is already here, then `--skip`. Cannot adapt safely: `git cherry-pick --abort`, report." For uncommitted work it carries the source path instead: "`git -C <src> diff --binary HEAD > <file outside both trees>` (plus the approved untracked files), then `git apply --3way <file>`; never modify the source; commit only if told, citing the source worktree and HEAD." Both end: "Run this branch's own checks; the fix's test must exist and pass here. No push or PR. Report each new SHA, each conflict and how you adapted it, commands and results."

Several targets: one agent each; a failed one never blocks the rest.

On notice or resume:
1. A question within scope: answer once with `notify:true`; a behaviour change is the user's.
2. Before reporting, verify yourself per target: `git status` clean, nothing in progress; `git log --format='%h %s%n%b' <H0>..HEAD` shows each pick's `cherry picked from commit` line; `git diff --stat <H0> HEAD` holds only the fix's files; `git range-diff <sha>^! <H0>..HEAD` per pick: a `++`/`+-`/`-+`/`--` line is a change the source never made, dragged in: one corrective round; the fix's test and checks re-run yourself (its commands, or `project.runCheck`: the work's `projectId`, `cwd` = the path); the source's HEAD unchanged.
3. Push and PR only when asked, after step 2: one send to the same agent (`notify:true`): `git push -u origin HEAD:refs/heads/<branch>`; an open PR by head first (`gh pr list --repo <r> --head <branch> --base <target>`), else `gh pr create --repo <r> --base <target> --head <branch> --title "[<target>] <original title>" --body-file <f>`, its body citing the source and checks. Read back `gh pr view <n> --repo <r> --json url,baseRefName,headRefOid`: head = the verified HEAD.

Stop and recovery: one corrective round per target, then the user. Never replay a pick, push or PR creation.

Report back: per target its worktree id, path, branch and final HEAD; skipped as already present (evidence), or the picks with new SHAs, conflicts and adaptations, checks and results, the PR number and URL read back or "not pushed"; the source unchanged.
