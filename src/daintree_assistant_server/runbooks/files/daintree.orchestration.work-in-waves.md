---
id: daintree.orchestration.work-in-waves
title: Work a planned list of issues in merge-gated waves
summary: Run waves of issues one after another — each wave's issues in their own worktrees and agents (often one recipe per issue), its pull requests proven green, merged by one merge agent, its worktrees torn down, and the next wave branched from the merged base.
whenToUse: Use when the user hands over WAVES (or rounds, or ordered batches) of issues to run one after another, where the next wave starts only once the previous wave's pull requests are merged — "wave 1 is these three issues, wave 2 these four", "once every PR in the wave passes CI, merge them, delete the worktrees and start the next wave", "run the merge command between waves", "keep going until all the waves are done", "start the next wave". Covers launching each wave (often a recipe per issue that assigns it and starts the agent), the per-wave CI gate, the merge step and teardown. Do not use for one batch, or a rolling queue under a cap, with no merge between waves (daintree.orchestration.fan-out-work), splitting a spec into waves for approval (daintree.orchestration.decompose-work), dependent phases that are not issue waves (daintree.orchestration.execute-durable-workflow), finding more issues to add (daintree.workflow.find-issue-work), merging finished agent branches once (daintree.git.integrate-agent-branches), or tearing a workspace down (daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - worktree.list
  - recipe.list
  - worktree.createWithRecipe
  - terminal.notifyWhenIdle
  - terminal.getStatus
  - agent.launch
  - terminal.closeOwned
  - worktree.deleteOwned
optionalTools:
  - terminal.list
---
A wave is a barrier: nothing in the next wave starts until every pull request in this one is merged. The run moves on Daintree notices, a host continuation you have verified, or the user: say which before ending a turn. Pin the repository on every read (`gh --repo`, `git -C`); a failed read is an unknown gate.

Procedure:
1. The plan is the user's. Restate it and read each issue in full: it waits until every issue it names as a prerequisite is merged; one closed or in flight (`worktree.list` `issueNumber`, `prNumber`) is skipped; same-file overlaps within a wave are reported, not re-planned. A wave you change or extend needs the user's yes. Checkpoint per the foundation: each issue's branch before its create, the returned ids after.
2. The base. A new branch starts from the main worktree's local branch unless `baseBranch` says otherwise; a forge merge does not move it and the create does not fetch. Before each wave `git fetch`, confirm the previous wave's merges are in `origin/<base>`, and branch from it, never pulling into the user's checkout.
3. Launch the wave, one worktree per issue. `recipe.list` gives ids, not prompts: confirm the user's recipe runs this task (its team file, or ask). It starts the agent; send it nothing more, and pass `assignToSelf` as the user asked:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"feature/issue-42-retry","baseBranch":"origin/<base>","fromRemote":true,"collisionPolicy":"error","issueNumber":42,"assignToSelf":true},"recipeId":"<id from recipe.list>","focusPolicy":"preserve"}
```

   It is confirm-gated; `CONFIRMATION_TIMEOUT` created nothing (check `worktree.list`), so ask. Read `spawnedTerminalIds`, `failedTerminalCount`, `assignedToSelf` and `assignmentError`: report a failure, never re-create. The agent may start before setup finishes (say so if setup matters) and has no notify: arm `terminal.notifyWhenIdle` on each working one, its issue in the note; `armed:false`, read its state now. No recipe: launch as daintree.orchestration.fan-out-work does.
4. The gate is yours on the forge, never an agent's done line. Per issue: a PR whose head is the worktree's `HEAD` (`gh pr view <branch> --json headRefOid,mergeStateStatus`), merge state clean, no check failing or pending (`gh pr checks`; note skipped ones), a clean tree. A failing check is its agent's to fix: report it.
5. Merge where the user said, with their merge command. Resolve what it lands before launching: PRs outside the wave need the user's yes unless already authorized.

```json tool=agent.launch
{"agentId":"claude","name":"Merge wave 2","worktreeId":"<main worktree id>","prompt":"<the user's merge command>","notify":true,"handback":true,"focusPolicy":"preserve"}
```

6. Teardown, if the user asked for it, once the merge is launched unless they ordered otherwise, so a pending delete never holds it. Per issue: its agent no longer working, `HEAD` still the gated PR head, tree clean; close the agent (`terminal.closeOwned`, confirmed by `closedIds`), then `worktree.deleteOwned`, which keeps the branch. `CONFIRMATION_TIMEOUT`: pending; say so once, retry when the user is there.
7. The merge is done when every wave PR is `MERGED` on the forge and the merge agent reports its own finish. Its notice may be an interim stop or a question: read it. `terminal.notifyWhenIdle` arms only on a working terminal, so recheck paced to the work. Then confirm the base's CI, close the merge agent if asked, and go to step 2.

On notice or resume:
1. Compare the time with the check that was due. Every check that never ran, and how long work sat finished, is told to the user.
2. Rebuild from live state: `worktree.list`, `terminal.getStatus` on the issue and merge agents, `gh pr view` per PR. Re-arm `terminal.notifyWhenIdle` on each agent still working.
3. A new help-session provision (your scratch path changed) owns none of the earlier panels or worktrees, and earlier notices may not reach it. Cleanup you no longer own stays pending: name the worktrees for the user to delete in Daintree; never switch tools to get past an ownership refusal.
4. A stopped issue agent: read its last screen; answer a question within scope, else ask the user. Advance only on step 4's gate or step 7's proof. Your own scheduled check firing is not the user's answer.

Stop and recovery: a confirmation timeout or a failed issue holds the advance, not the watch. Stop pacing only after saying what will wake the run. Never relaunch an agent or rerun a recipe to repair tracking. On cancellation launch nothing more; teardown is daintree.orchestration.workspace-cleanup.

Report back: per wave its issues, branches, terminal ids, PRs and state; what you verified and how; pending deletes; PRs the merge landed beyond the wave; the next check and what brings it; every check that never ran.
