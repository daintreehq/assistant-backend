---
id: daintree.git.split-into-stacked-prs
title: Split a large change into stacked PRs
summary: Plan an oversized branch or PR as ordered layers, rebuild it as a chain of branches each based on the one below and each passing on its own, prove the top of the stack equals the original, and open the stacked PRs only when asked.
whenToUse: 'Use when one change is too big to review and the user wants it broken into smaller dependent pieces — "split this PR into smaller ones", "turn my branch into stacked PRs", "break #600 into reviewable chunks", "separate the refactor from the feature", "make a PR stack out of these commits", or just a proposed split with nothing changed yet. The deliverable is an ordered stack of branches (and PRs if asked), each with its base, scope and own passing checks, plus proof that nothing was lost; the original left untouched. Do not use to open one PR for finished work (use daintree.workflow.prep-branch-for-review); to bring a branch up to date with its base (use daintree.git.sync-with-base); to combine several branches into one (use daintree.git.integrate-agent-branches); or to review the big change as it stands (use daintree.review.review-change).'
risk: git
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.sendCommandOwned
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You freeze the original, plan the layers and verify; a visible agent in one new worktree builds the stack. Git reads are yours.

On a host: shell steps via its command runner, or delegate; own shell: `gh -R <repo>`.

Procedure:
1. Freeze the original. A branch: its worktree from `worktree.list` and `git rev-parse <branch>`. A PR: `gh pr view N --repo <r> --json headRefName,headRefOid,baseRefName`. Record tip O, base B and fork point `M = git merge-base origin/<B> O`; uncommitted changes in its worktree are not in O: say so. The original is never rewritten, force-pushed, retitled or closed here.
2. Plan from `git diff --stat M O` and the diff: definitions before their consumers; mechanical changes (renames, moves, formatting) before behaviour; each test in its code's layer; a few hundred changed lines per layer. The plan (files or hunks per layer, order, why) goes in the brief and your report. A proposal-only request ("how would you split this?") stops here. A request to split has no approval round: never ask "does this work for you?"; go on to step 3 unless a choice changes its scope.
3. Create ONE worktree at M, so the stack reproduces O exactly (a newer B is a later sync), then `worktree.waitUntilReady` with its `worktreeId` (at most three calls; only `ready` proceeds):

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"split/<slug>-1","baseBranch":"<M sha>","collisionPolicy":"error"}}
```

4. Launch the agent there:

```json tool=agent.launch
{"agentId":"claude","name":"Split: <slug>","worktreeId":"<stack worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief carries the path, O, M, the plan and: "Non-interactive only: no `git add -p`, `rebase -i` or editor. First `git checkout --detach <O>` and run the checks as the baseline (a check O already fails is a known constraint: report it). Then `git switch -C split/<slug>-1 <M>`; each later layer `git switch -c split/<slug>-<k>` from the one below. Per layer: whole files `git checkout <O> -- <paths>`, a file O deleted `git rm <path>`; a file split across layers gets an intermediate version (`git diff <M> <O> -- <file>` trimmed to this layer's hunks, `git apply`) so it reaches O's version by the top. Stage exactly the layer's paths, read `git diff --cached --stat`, commit with a message about this layer only, and run the checks on that commit. A layer failing alone lacks a piece: move it down from O (amend this layer) and re-test; never write code O does not have. Changing a lower layer means rebuilding those above. One repair per layer; still failing, stop and report. Clean tree before each next branch. At the top `git diff <O> HEAD` must be empty. No push or PR. Report per layer its branch, HEAD, scope and check results."

On notice or resume:
1. A question within scope: answer once with `notify:true`; a scope change goes to the user.
2. Verify: `git diff <O> split/<slug>-<top>` empty; per layer `git diff --stat <layer below, M for the bottom> split/<slug>-<k>` holds only its planned scope and `git merge-base --is-ancestor` chains it on the one below; the checks re-run yourself per layer (with the agent idle, `git switch split/<slug>-<k>` in the stack worktree, then its commands, or `project.runCheck`: the work's `projectId`, `runnerId` from `project.detectRunners`, `cwd` = the path); the original still at O.
3. Publish only when asked, after step 2: one send to the same agent (`notify:true`): "Push each layer bottom first (`git push -u origin split/<slug>-<k>`). A stacking tool the repository's docs or config name replaces the manual steps. Per layer, an existing open PR by head first, else `gh pr create --repo <r> --head split/<slug>-<k> --base <layer below; B for the bottom> --title "<layer title> (<k>/<n>)" --body-file <f>`; once all exist, edit each body to link the original and the PRs below and above." Read back each `gh pr view <n> --repo <r> --json url,baseRefName,headRefOid`: bases chained, each head the verified layer SHA.
4. Tell the user how it lands: bottom first; GitHub retargets the next PR to B only when the merged branch is deleted; after a squash merge each layer above is restacked upward with `git rebase --onto <new parent SHA> <old parent SHA> <layer>`, keeping every old tip, as a separate authorized round. Suggest closing or drafting the original; do it only if asked.

Stop and recovery: a layer still failing after its one repair stops the build; report the partial stack and failing evidence; re-plan with the user. Read the branch list before any redo; never relaunch while the agent's terminal lives.

Report back: the original's branch, O, B and M (untouched); the plan and why; per layer its branch, base, HEAD, scope, checks you ran; the union proof; PR numbers, URLs and bases as read back, or "not pushed"; what remains for the user.
