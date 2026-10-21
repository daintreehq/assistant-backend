---
id: daintree.git.sync-with-base
title: Bring a branch up to date with its base
summary: Merge or rebase a stale or conflicted branch onto its current base in its own worktree, resolve every conflict keeping both sides' intent, rerun the checks on the new HEAD, tell the agent working there, and push with a lease only when asked.
whenToUse: Use when a branch, worktree or PR has fallen behind its base or conflicts with it and should be caught up — "rebase this on main", "merge latest develop into the auth worktree", "my PR has merge conflicts, sort them out", "this branch is 40 commits behind", "catch all my branches up with main before the agents carry on". The deliverable is each branch containing the current base, every conflict resolved without dropping either side, checks rerun on the new HEAD, any agent there told the ground moved, and a push only as authorized. Do not use to combine several agents' branches into one (use daintree.git.integrate-agent-branches); to carry chosen commits onto another branch (use daintree.git.cherry-pick-changes); only to forecast conflicts without changing anything (use daintree.worktree.compare-worktrees); to commit, push or open a PR for work that is not behind (use daintree.workflow.prep-branch-for-review); to merge the PR on the forge (use daintree.forge.land-pull-request); to fix a check that was already failing before the sync (use daintree.ci.diagnose-and-fix); or when finished branches each pass alone but misbehave once combined (use daintree.git.resolve-semantic-conflict).
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - forge.getPR
  - terminal.notifyWhenIdle
  - terminal.sendCommandOwned
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You bind targets, preflight and verify; a visible agent in the branch's worktree does the merge or rebase (step 4). Never merge, rebase or resolve conflicts yourself unless the user asked: however quick, it is the delegated work. Git reads: `git -C <path>`.

On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Bind each target in `worktree.list`: id, path and checked-out `branch` (even the main checkout; the base branch only when named). "My branches": the non-main worktrees. A PR: its worktree's `prNumber`, or `forge.getPR` for `baseRef`; with no worktree, `worktree.createWithRecipe` `{"source":{"kind":"pullRequest","pullRequestNumber":N}}`, then `worktree.waitUntilReady` with the returned `worktreeId` (at most three calls; only `ready` proceeds, else report its state).
2. Preflight, changing nothing but remote-tracking refs:
   - Base: the user's, else the PR's `baseRef`, else `git symbolic-ref --short refs/remotes/origin/HEAD` (prints `origin/main`). `git fetch origin`, then freeze `B = git rev-parse origin/<base>` and use B, never the name.
   - `git rev-list --left-right --count HEAD...<B>`: behind 0 is already current; skip it. Record old HEAD and, if pushed, the remote tip R (`git rev-parse origin/<branch>`). R not an ancestor of HEAD (`git merge-base --is-ancestor`) means the remote has commits missing here: stop, report.
   - `git status`: an operation in progress you did not start is reported, never aborted; dirty: ask.
   - Agents there: `terminal.list` with the `worktreeId`, and again with `location:"background"`, skipping your own pane (`$DAINTREE_PANE_ID`). `working`: report it, or, to sync once it is free, `terminal.notifyWhenIdle` and end the turn on `armed:true`. `waiting`: its question or approval comes first.
3. Choose the brief's command: the user's or repository's rule wins. Otherwise merge a pushed branch or one with a PR; rebase a never-pushed one.
4. Brief the branch's own idle agent (yours or handed over; it knows the intent) with `terminal.sendCommandOwned`, `notify:true, handback:true`. None, or `RESOURCE_NOT_OWNED`: launch one, and tell the user any other agent there must re-read its files:

```json tool=agent.launch
{"agentId":"claude","name":"Sync: <branch> onto <base>","worktreeId":"<worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief carries the path, branch, base name and B, the command (`git merge --no-edit <B>` or `GIT_EDITOR=true git rebase <B>`) and verbatim: "Never `git stash` or `git checkout <base>`: worktrees share the stash and refs. Per conflicted file (`git diff --name-only --diff-filter=U`) read `:1:`, `:2:` and `:3:` (in a rebase `:2:` is the base, `:3:` your commit) and keep both intents; never take a side wholesale (`-X ours/theirs`), never delete or loosen a test. Lockfiles and generated files: take the base's and regenerate. Then `git diff --check`, no `<<<<<<<` left, `git add -- <paths>`, `GIT_EDITOR=true git rebase --continue` or `git commit --no-edit`; a rebase can stop once per commit. Two sides changing one behaviour differently: `--abort` and report both versions. Then always run the checks (reinstall first if a manifest or lockfile changed); a failure the base also has is pre-existing, report it; one the sync caused, fix it. Do not push. Report old and new HEAD, each conflict and its resolution, commands and results."

Several branches: one agent per worktree; one stopping never blocks the rest.

On notice or resume:
1. A question within scope: answer once with `notify:true`; a behaviour choice goes to the user.
2. Before any report, verify yourself in that worktree: `git status` clean with no operation in progress; `git merge-base --is-ancestor <B> HEAD`; `git diff <old HEAD> HEAD -- <conflicted files>` keeps both sides and no test was removed or loosened; the checks re-run yourself (its commands, or `project.runCheck`: the work's `projectId`, `cwd` = the path); `git ls-remote origin refs/heads/<branch>` still R.
3. Push only when asked, after step 2: one send to the same agent with the exact command. After a merge `git push origin HEAD:refs/heads/<branch>`; after a rebase `git push --force-with-lease=refs/heads/<branch>:<R> origin HEAD:refs/heads/<branch>`: a rejected lease: someone else pushed; stop, report. Read back `git ls-remote` (a PR: `gh pr view <n> --json headRefOid`) against HEAD.

Stop and recovery: one corrective round per branch (the failing evidence, `notify:true`); a second failure goes to the user. Never replay a merge, rebase or push. The rescue point is old HEAD; reset to it only on the user's word.

Report back: per branch the base and B, merge or rebase, old and new HEAD, each conflict and its resolution, the checks you re-ran and results, the push read-back or "not pushed", what was skipped or stopped and why.
