---
id: daintree.orchestration.work-in-waves
title: Work a planned list of issues in merge-gated waves
summary: Run waves of issues one after another — each wave's issues in their own worktrees and agents (often one recipe per issue), its pull requests proven green, merged by one merge agent, its worktrees torn down, and the next wave branched from the merged base.
whenToUse: Use when the user hands over WAVES (or rounds, or ordered batches) of issues to run one after another, here or on one other machine they name, whatever they call it (a Remote Host), where the next wave starts only once the previous wave's pull requests are merged — "wave 1 is these three issues, wave 2 these four", "once every PR in the wave passes CI, merge them, delete the worktrees and start the next wave", "run the merge command between waves", "keep going until all the waves are done", "start the next wave". Covers launching each wave (often a recipe per issue that assigns it and starts the agent), the per-wave CI gate, the merge step and teardown. Do not use for one batch, or a rolling queue under a cap, with no merge between waves (daintree.orchestration.fan-out-work), splitting a spec into waves for approval (daintree.orchestration.decompose-work), dependent phases that are not issue waves (daintree.orchestration.execute-durable-workflow), finding more issues to add (daintree.workflow.find-issue-work), merging finished agent branches once (daintree.git.integrate-agent-branches), or tearing a workspace down (daintree.orchestration.workspace-cleanup).
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
  - slashCommands.list
  - terminal.readLastMessageOwned
---
A wave is a barrier: nothing in the next wave starts until every pull request in this one is merged. Before ending a turn, say what wakes the run. Pin the repository on every read (`gh --repo`, `git -C`); a failed read is an unknown gate.

On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. The contract is the user's; restate it: waves they listed, or a drain ("until none are left") picked a wave at a time by their command or as daintree.workflow.find-issue-work does; the cap; overlap policy; each command; who fixes failing CI; teardown scope. Report a drain's picks and any overlap, never re-planning; a changed listed wave needs a yes.
2. Before each wave re-read each issue on the forge: skip closed or in-flight ones (`worktree.list` `issueNumber`, `prNumber`), saying why; a listed one waits on its prerequisites, a pick needs them merged and no assignee. An empty pick ends a drain; a pick the re-check empties is picked again. Checkpoint each branch before its create.
3. Commands: find each with `slashCommands.list` for its agent in the main worktree before the first launch. Send it as typed with `notify:true` and no `handback` (it joins the arguments), so re-arm `terminal.notifyWhenIdle` once it works again (a stopped one refuses: recheck). Its finish is the end marker it documents, else its screen.
4. Neither a forge merge nor a create updates the local base: before each wave `git fetch`, confirm the last wave's merges are in `origin/<base>` and branch from it.
5. Launch the wave, one worktree per issue. A recipe the user named starts the agent (`recipe.list` gives ids, not prompts: confirm it runs this task):

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"feature/issue-42","baseBranch":"origin/<base>","fromRemote":true,"collisionPolicy":"error","issueNumber":42,"assignToSelf":true},"recipeId":"<id from recipe.list>","focusPolicy":"preserve"}
```

   It is confirm-gated; `CONFIRMATION_TIMEOUT` created nothing (check `worktree.list`), so ask. Its panels are `spawnedTerminalIds` (setup may still run), without notify: arm `terminal.notifyWhenIdle` on each working agent (`armed:false`: read it now). Report a `failedTerminalCount` or `assignmentError`; never re-create. No recipe: omit `recipeId` and launch on `setupState:"ready"` as daintree.orchestration.fan-out-work does. `assignToSelf` as asked.
6. The gate is on the forge: per issue a PR whose head is the worktree's `HEAD` (`gh pr view --json headRefOid,mergeStateStatus`), merge state clean, no check failing or pending (`gh pr checks`), a clean tree. A failing check goes to whoever the contract names.
7. Merge with the user's command where they said, once you know what it lands: PRs beyond the wave need a yes unless authorized. If it moves its checkout, wait until that is clean and on the base, never switching or cleaning it. A long merge stops between background steps; without its finish that is normal: read it for a question, nudge once after long silence. Done is its documented marker or finished screen (never that phrase in progress text) and every wave PR `MERGED` on the forge; reverted is not landed. Then check base CI; close the merge agent if asked.
8. Teardown when and as the user asked (default: landed issues, after the merge). Per worktree: agent not working, `HEAD` still the gated head, tree clean, no pane you did not open (`terminal.list` by `worktreeId`, background too); else keep it, saying why. Close the agent (`terminal.closeOwned`, confirmed by `closedIds`), then `worktree.deleteOwned`, which keeps the branch. `CONFIRMATION_TIMEOUT` is pending: say so once, retry with the user there; it holds the next wave only if asked. Then step 2.

On notice or resume:
1. A notice or scheduled check prompts a fresh `terminal.getStatus`. Over hours, if you can schedule, keep one recurring check, removed at the end. Tell the user every check that never ran and how long work sat finished.
2. Rebuild from live state (`worktree.list`, `terminal.getStatus`, the forge) and re-arm per step 3.
3. A new help session owns no earlier worktree: list those for the user to delete.
4. A stopped agent: read its last reply (`terminal.readLastMessageOwned`, or its screen). A working one with a stale `lastOutputChangeAt` is reported, not interrupted. Advance only on step 6's gate or step 7's proof.

Stop and recovery: a blocked issue holds the wave until the user answers; a failed one holds the next wave unless the contract says go on. A drain halted by an unfinished merge or unlanded PRs is stuck, not drained. Teardown on cancellation is daintree.orchestration.workspace-cleanup.

Report back: per wave its issues, branches, terminals, PRs and state; worktrees kept or pending, and why; PRs landed beyond the wave; issues assigned that never landed.
