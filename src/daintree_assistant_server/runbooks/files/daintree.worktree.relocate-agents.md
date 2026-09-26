---
id: daintree.worktree.relocate-agents
title: Move running agents into their own worktrees
summary: Give agents that are ALREADY RUNNING one isolated worktree each — create the empty worktrees, file each terminal under its own, and tell each agent to continue in its new directory.
whenToUse: Use when agents that are already running share a worktree and the user wants them separated — "move each of those into its own worktree", "create four worktrees and move the agents into them", "give each terminal its own branch", "isolate these agents so they stop colliding". The tell is that the terminals ALREADY EXIST and are mid-task; the user wants them relocated, not replaced. Do not use to create a worktree with no agent involved (use daintree.worktree.create-plain-worktree), to spawn NEW agents into fresh worktrees (use daintree.edits.spawn-visible-agent, or daintree.orchestration.fan-out-work for a cohort), to start a forge issue in a worktree (use daintree.workflow.start-work-on-issue), to create a worktree with a startup recipe (use daintree.recipe.run-or-create), or to delete worktrees (use daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.moveToWorktree
  - terminal.sendCommandOwned
optionalTools:
  - terminal.notifyWhenIdle
---
Instructing an agent needs the owned send (agents you launched or were handed). An API-key client lacks `terminal.moveToWorktree` and `worktree.waitUntilReady`: leave panel filing to the user.

Three separate facts: panel placement (all `terminal.moveToWorktree` changes), process root (the process keeps working where it launched until told, and some CLIs cannot change root without a restart), and Daintree binding (its `DAINTREE_WORKTREE_ID` still names the old worktree, so its own calls must pass `worktreeId`).

1. From `terminal.list` and `worktree.list` map each agent the user meant: terminal id, old worktree id and path, new branch and base.
2. Protect uncommitted work. Read each agent's state and `git status` in the shared tree. A new worktree brings no changes: an agent whose work must travel applies a patch of its own changes in the destination and leaves the source as is. Check its patch against the diff; report hunks nobody can attribute. Never reset, clean, stash or check out in the shared tree. A `working` agent is instructed only back at its prompt: arm `terminal.notifyWhenIdle` with a note naming this relocation and agent, and do steps 5 to 7 for it on the notice.
3. One EMPTY worktree per agent, distinct branch, no `recipeId`, `collisionPolicy:"error"`. Keep each `worktreeId` and `worktreePath` verbatim; reconcile a lost reply with `worktree.list`. `worktree.waitUntilReady` each: only `ready` gets an agent.
4. Before the first move, snapshot every panel's `worktreeId` from `terminal.list`.
5. `terminal.moveToWorktree` per agent, then compare with the snapshot. Tab-grouped panels travel together and the listing doesn't show groups: if anything else moved, stop, report the group and leave the split to the user. Never move a panel back.
6. Instruct each owned agent once, at its prompt, with `waitForReply:true, handback:true`; the agent now awaits relocation confirmation until the reply returns. Tell the user, so they skip the pane's "continue in the new worktree" banner; for an agent you don't own that banner is the path.

```json tool=terminal.sendCommandOwned
{"terminalId":"<owned agent terminal id>","command":"Continue this task in <destination worktreePath> on branch <branch>. First run pwd and git branch there and report both. Work only inside that directory, and pass worktreeId <destination worktreeId> on any Daintree call. Leave the original worktree's changes as they are.","handback":true}
```

7. Verify each fact: placement from `terminal.list`; root from the reported `pwd` and branch, then where new edits land (`git status` in both paths), since "OK" is not evidence; the source changes still there. An agent that cannot change root keeps its session: report the limit, never restart or discard it.

On notice or resume: take each agent's phase from your record, not the notice. At the boundary and back at its prompt: steps 5 to 7. Awaits relocation confirmation: step 7 only; never move its panel or send the instruction again. A question, approval or error comes first; never type the relocation into it.

Stop and recovery: one create per destination, one relocation message per agent. A refused move or send never justifies closing or respawning an agent, raw `git worktree`, or deleting the original checkout. Close nothing unasked.

Report back: per agent the destination id, path and branch; whether its panel moved (and what moved with it); its confirmed directory and branch and where edits land; changes kept in the source tree; agents not yet instructed and why.
