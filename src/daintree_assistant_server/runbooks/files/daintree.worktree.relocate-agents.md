---
id: daintree.worktree.relocate-agents
title: Move running agents into their own worktrees
summary: Give agents that are ALREADY RUNNING one isolated worktree each — create the empty worktrees, file each terminal under its own, and tell each agent to continue in its new directory.
whenToUse: Use when agents that are already running share a worktree and the user wants them separated — "move each of those into its own worktree", "create four worktrees and move the agents into them", "give each terminal its own branch", "isolate these agents so they stop colliding". The tell is that the terminals ALREADY EXIST and are mid-task; the user wants them relocated, not replaced. Do not use to create a worktree with no agent involved (use daintree.worktree.create-plain-worktree), to spawn NEW agents into fresh worktrees (use daintree.edits.spawn-visible-agent, or daintree.orchestration.fan-out-work for a cohort), to start a forge issue in a worktree (use daintree.workflow.start-work-on-issue), to create a worktree with a startup recipe (use daintree.recipe.run-or-create), or to delete worktrees (use daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - context.snapshot
  - worktree.list
  - worktree.createWithRecipe
  - terminal.moveToWorktree
  - terminal.sendCommand
  - terminal.read
  - watcher.list
  - watcher.cancel
  - async.list
  - agentTask.superviseTerminal
  - workflow.update
---
Use when: existing live agents need separate worktrees while keeping their sessions.

Moving a panel changes its Daintree grouping, not its process directory or working-tree changes. Telling the agent the destination is a second operation, and even that is not proof it switched. Preserve the original sessions and all uncommitted work.

Procedure:
1. Read the requested terminals, `worktree.list` and their current paths. Keep a per-agent map of terminalId, old worktreeId/path, new branch/base and destination id/path. Use only the terminals the user intended. Fresh agents belong to the spawn/fan-out runbook.
2. Inspect whether agents are actively editing and whether the source has uncommitted changes. Separate checkouts do not copy those edits. If work must transfer, first obtain an agent's concrete transfer plan within the user's scope; do not move it by reset, clean, blind cherry-pick or file copying. Arrange a handoff boundary with busy agents so edits cannot continue in the old tree unnoticed.
3. Settle distinct branch names and bases, using user/repository conventions. Create EMPTY worktrees with `worktree.createWithRecipe`, omitting `recipeId`. Use a distinct stable requestKey for each logical creation and `collisionPolicy:"error"` when an exact new name is required.

Example arguments to `worktree.createWithRecipe`:
```json tool=worktree.createWithRecipe
{"arguments":{"source":{"kind":"newBranch","branchName":"<chosen branch>","baseBranch":"<verified base>","collisionPolicy":"error"}},"requestKey":"<unique key for this destination>"}
```

4. Read every creation result before moving anything. A partial/ambiguous failure requires `worktree.list` reconciliation, not another create. Copy BOTH `worktreeId` and `worktreePath` verbatim; do not construct one from the other.
5. Move each terminal with the typed wrapper, never through `daintree.call`. Group only terminals sharing the SAME destination; one-worktree-per-agent normally means one call per agent:

```json tool=terminal.moveToWorktree
{"terminalId":"<exact terminal id>","worktreeId":"<returned destination worktreeId>"}
```

   This is `terminal.moveToWorktree` argument JSON. Pass the returned id, not a guessed path or branch; an older host may itself return path-shaped ids. The action can move the whole tab group with the named panel, so inspect grouping before moving and verify any other affected panels afterward. Do not claim independent relocation when a shared tab group moved together; resolve that grouping in Daintree first.
6. Tell each agent ONCE: `terminal.sendCommand({"terminalId":"<id>","command":"Please continue in the directory <returned worktreePath>. Confirm the directory and branch before editing; leave the original worktree's existing changes intact."})`. If panel filing failed, report that separately; the authorized directory instruction can still be sent to the verified live agent. Do not resend because its ready prompt has not yet changed.
7. Verify the two postconditions independently: `context.snapshot`/`worktree.list` for panel placement and original branch, then a bounded tail for the agent's acknowledgement of its directory and branch. Panel placement alone cannot prove where file operations occur. If the agent cannot change roots without restart, report the limitation and preserve its session until the user chooses a next step.
8. Reconcile existing supervision against the new destination. A watcher created for the old worktree retains that verification scope. Read `watcher.list` and `async.list`; do not add a second watcher. If a supervisor must be replaced, retire that exact old watcher, adopt the live terminal with its new worktreeId and acceptance criteria, and save the new handle. Mere `superviseTerminal` deduplication does not update an existing watcher's scope. Update the task's ledger/checkpoint from observed results.
9. Close nothing; `terminal.close` requires a separate explicit closure request. A refused move does not justify closing/respawning the agent, raw git worktree operations, or deleting its original checkout.

Confirmation: the user's relocation request authorizes the resolved creates, panel moves and directory instructions. Let normal tool gates handle confirmation; ask only for unresolved destinations, transfer decisions or newly discovered scope.

Report back: each terminal's destination id/path/branch, whether panel filing and directory acknowledgement each succeeded, any retained source changes, refreshed supervision handles, and any incomplete relocation.
