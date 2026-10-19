---
id: daintree.git.integrate-agent-branches
title: Integrate several agents' branches into one
summary: Merge the finished branches of parallel agents or worktrees one at a time into an integration branch in its own worktree, checking after each merge so every conflict or regression is pinned to the branch that caused it.
whenToUse: Use when the work of SEVERAL agents, branches or worktrees must be combined into one branch and shown to work together — "merge all four agents' work together", "combine what each worktree did into one branch", "integrate the fan-out results before I open one PR", "put the frontend and backend branches together and see if it still builds". The deliverable is an integration branch in its own worktree with each source merged in a recorded order at a frozen commit, checks after every merge and on the final tree, and each conflict resolved or handed back with evidence; nothing pushed. Do not use to bring ONE branch up to date with its base (use daintree.git.sync-with-base); to copy particular commits from one branch to another (use daintree.git.cherry-pick-changes); only to predict whether branches will conflict or in what order to merge them (use daintree.worktree.compare-worktrees); to run the parallel work itself (use daintree.orchestration.fan-out-work); to pick one of several rival versions (use daintree.orchestration.competition); to open the PR afterwards (use daintree.workflow.prep-branch-for-review); to merge PRs on the forge (use daintree.forge.land-pull-request); while two agents are still working on the same task (use daintree.orchestration.resolve-task-collisions); or to settle a behaviour clash between merged sources (use daintree.git.resolve-semantic-conflict).
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You resolve the sources, forecast and verify; one visible agent in a new integration worktree does the merging. Git reads: `git -C <path>`; worktrees on one machine share one object store, so any SHA there merges without a fetch; a source on another machine must be pushed, then fetched.

On a host: shell steps via its command runner and checks via its `project.runCheck`, else delegate.

Procedure:
1. Sources: resolve each named branch or worktree to a commit (`git rev-parse <ref>`), with its worktree from `worktree.list` when it has one. A checked-out source is ready only when `git status --porcelain` there is empty and no agent in it is `working` or `waiting` (`terminal.list` with its `worktreeId`, skipping your own pane): uncommitted work never reaches a merge. Not ready: ask its agent to commit (a terminal you own) or tell the user; never commit for it. Freeze each ready source's SHA: the agent merges that, never the name, which moves. Another assistant's brief pins sources by SHA on the shared remote: `git fetch --prune origin`; a pin no `git branch -r --contains <SHA>` lists: ask back, never take the tip. Push or PR only as it authorizes.
2. Base: the one the sources were cut from (the fan-out's, or the user's); freeze its SHA. Forecast without touching refs, the index or any tree: `git merge-tree --write-tree --name-only <sha A> <sha B>` per pair (exit 1 lists conflicting files; git 2.38+; skip it on a strict no-write request). Order: sources owning shared artifacts (lockfile, schema, config) first, foundations before their consumers, then conflict-free and smaller ones. State the order and the predicted conflicts.
3. Create the integration worktree (never the user's checkout, a source's worktree or main/develop), then `worktree.waitUntilReady` with the returned `worktreeId` (at most three calls; only `ready` proceeds, else report its state):

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"integrate/<name>","baseBranch":"<base SHA>","collisionPolicy":"error"}}
```

4. Launch ONE agent there:

```json tool=agent.launch
{"agentId":"claude","name":"Integrate: <name>","worktreeId":"<integration worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief carries the path, the base SHA, the ordered sources with their frozen SHAs, the check commands, the user's limits, and verbatim: "Run the checks on the base first as the baseline. Then one source at a time, never an octopus: `git merge --no-ff --no-edit <sha>`, the checks, and a record of the pre-merge HEAD, new HEAD and result; a failure the baseline lacked is that merge's unless it is environmental. Mechanical conflicts (imports, adjacent additions, both appending to a list): keep both. Lockfiles and generated files: regenerate on the combined tree. Stage only resolved paths (`git add -- <paths>`); `git ls-files -u` empty, no `<<<<<<<`, `git diff --cached --check` clean; then commit. Both sides changing one behaviour: `git merge --abort`, skip that source and any that depend on it, and report both versions. A merge whose breakage you cannot fix without changing a source's behaviour: `git reset --hard <its pre-merge HEAD>` and carry on with the independent rest. Touch nothing outside this worktree: no source branch, push, PR or deletion. Report per source its SHA, both HEADs, conflicts and resolutions, check results, and what was skipped."

On notice or resume:
1. A question within scope: answer once with `notify:true`. A held-back behaviour conflict goes to daintree.git.resolve-semantic-conflict, with this integration worktree, its HEAD and both frozen SHAs; the merge agent does not resolve it.
2. Before any report, verify yourself in the integration worktree (the notice is the agent's claim): `git status` clean, no merge in progress; `git log --oneline --first-parent <base SHA>..HEAD` shows one merge per integrated source in the stated order; `git merge-base --is-ancestor <sha> HEAD` for each; `git show --remerge-diff <merge>` (git 2.36+) on each conflicted merge keeps both sides; the full suite re-run yourself (its commands, or `project.runCheck`: the work's `projectId`, `runnerId` from `project.detectRunners`, `cwd` = the integration path); `git ls-remote origin refs/heads/integrate/<name>` empty.
3. A source readied or resolved later: one send to the same agent with its new frozen SHA, `notify:true, handback:true`.

Stop and recovery: one corrective round per conflict or failure; still stuck, it goes to the user with the evidence. On resume read the agent's status and the integration log before any send; never relaunch while its terminal lives, never replay a merge.

Report back: integration worktree id, path, branch and HEAD; the base SHA and baseline result; the merge order with each frozen SHA; per merge its conflicts and how each was resolved or who holds it, and the check result; your final suite result; sources skipped or pending and why.
