---
id: daintree.worktree.relocate-agents
title: Move running agents into their own worktrees
summary: 'Move agents that are ALREADY RUNNING into worktrees of their own: create the worktrees, file each terminal under its own, and tell each agent to continue, or start the task it was given, in its new directory.'
whenToUse: Use when an agent that ALREADY EXISTS, one or several, idle or mid-task, is to be moved into its own new or another worktree, optionally with a task to do there — "move Claude to a new worktree called X and have it fix Y there", "move each of those into its own worktree", "create four worktrees and move the agents into them", "give each terminal its own branch", "isolate these agents so they stop colliding". The tell is that the terminals ALREADY EXIST; the user wants them relocated, not replaced. Do not use to create a worktree with no agent involved (use daintree.worktree.create-plain-worktree), to spawn NEW agents into fresh worktrees (use daintree.edits.spawn-visible-agent, daintree.orchestration.fan-out-work for a cohort, or daintree.orchestration.competition when each does the same task to be compared), to start a forge issue in a worktree (use daintree.workflow.start-work-on-issue), to create a worktree with a startup recipe (use daintree.recipe.run-or-create), to delete worktrees (use daintree.orchestration.workspace-cleanup), when a DIFFERENT agent is to carry on the task in the same worktree (use daintree.agents.hand-off-task), to carry commits from one worktree's branch into another (use daintree.git.cherry-pick-changes), to compare what worktrees changed (use daintree.worktree.compare-worktrees), or when two agents are doing the SAME task and one must be picked as its owner (use daintree.orchestration.resolve-task-collisions).
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
An API-key client lacks `terminal.moveToWorktree` and `worktree.waitUntilReady`: leave panel filing to the user.

Three separate facts: panel placement (all `terminal.moveToWorktree` changes), process root (the process keeps working where it launched until told, and some CLIs cannot change root without a restart), and Daintree binding (its `DAINTREE_WORKTREE_ID` still names the old worktree, so its own calls must pass `worktreeId`).

Fast path: one agent you launched in this conversation, given no task yet (or idle with no edits of its own when you last saw it), has nothing to protect and needs no inventory: skip steps 1, 2 and 4. Create its NEW worktree (step 3; `baseBranch` only if you launched it off the main branch) and await `ready`, move it (step 5: a panel you launched is in no tab group unless the user grouped it, so a move that returns without error moved it alone; do not list again), then send the directory check and the user's task in one message, and tell the user about the banner (step 6), with `notify` for a long task. Verify from its reply (step 7), not a listing. An agent mid-task, with uncommitted changes, or not launched by you takes the full procedure.

On a host: shell steps run there, per the hosts foundation.

1. From `terminal.list` and `worktree.list` map each agent the user meant: terminal id, old worktree id and path, new branch and base.
2. Protect uncommitted work: read each agent's state and `git status` in the shared tree (an idle agent with a clean tree has none). A new worktree brings no changes: an agent whose work must travel applies a patch of its own changes in the destination and leaves the source as is. Check its patch against the diff; report hunks nobody can attribute. Never reset, clean, stash or check out in the shared tree. A `working` agent is instructed only back at its prompt: arm `terminal.notifyWhenIdle` with a note naming this relocation and agent, and do steps 5 to 7 for it on the notice.
3. A destination is on the agent's machine (another machine: the work goes pushed or as content); one that exists: its id from `worktree.list`. Otherwise one EMPTY worktree per agent, distinct branch (the name the user gave, else one you state), `baseBranch` from step 1 when it is not the main branch, no `recipeId`:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<branch>","collisionPolicy":"error"}}
```

   Keep each `worktreeId` and `worktreePath`; reconcile a lost reply with `worktree.list`. `worktree.waitUntilReady` each: only `ready` gets an agent.
4. Before the first move, snapshot every panel's `worktreeId` from `terminal.list`.
5. Move each panel, then compare to the snapshot:

```json tool=terminal.moveToWorktree
{"terminalId":"<agent terminal id>","worktreeId":"<destination worktreeId>"}
```

   Tab-grouped panels travel together and the listing doesn't show groups: if anything else moved, stop, report the group and leave the split to the user. Never move a panel back.
6. Instruct each owned agent once, at its prompt, with `waitForReply:true, handback:true`; until it replies it awaits relocation confirmation. A task the user gave it for the new worktree goes in the same message, in place of "this task". Tell the user, so they skip the pane's "continue in the new worktree" banner; for an agent you don't own that banner is the path.

```json tool=terminal.sendCommandOwned
{"terminalId":"<owned agent terminal id>","command":"Continue this task in <destination worktreePath> on branch <branch>. First run pwd and git branch there and report both; if either differs, stop without editing. Work only inside that directory, and pass worktreeId <destination worktreeId> on any Daintree call. Leave the original worktree's changes as they are.","waitForReply":true,"handback":true}
```

7. Verify each fact: placement from `terminal.list`; root from the reported `pwd` and branch, then where new edits land (`git status` in both paths); the source changes still there. An agent that cannot change root keeps its session: report the limit, never restart or discard it.

On notice or resume: take each agent's phase from your record, not the notice. At the boundary and back at its prompt: steps 5 to 7. Awaits relocation confirmation: step 7 only; never move its panel or send the instruction again. A question, approval or error comes first; never type the relocation into it.

Stop and recovery: one create per destination, one relocation message per agent. A refused move or send never justifies closing or respawning an agent, raw `git worktree`, or deleting the original checkout.

Report back: per agent the destination id, path and branch; whether its panel moved (and what moved with it); its confirmed directory and branch and where edits land; changes kept in the source tree; agents not yet instructed and why.
