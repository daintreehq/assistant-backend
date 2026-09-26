---
id: daintree.recipe.run-or-create
title: Run a Daintree workspace recipe, or create a worktree with one
summary: List, inspect and run an EXISTING Daintree workspace recipe — or create a worktree with one as its startup recipe — then check which terminals started, including any LIVE agent. Nothing here authors or edits a recipe.
whenToUse: Use when the user asks to list, inspect, load, run or apply an existing Daintree workspace recipe, or to create a worktree with a startup recipe. Also use when they ask to CREATE or edit a recipe — only so you can say plainly that no Daintree action authors one and offer what you can actually do. Do not use to create worktrees for agents that are already running (use daintree.worktree.relocate-agents), to create a worktree for a forge issue, which has its own typed workflow (use daintree.workflow.start-work-on-issue), to launch a single agent with no recipe involved (use daintree.edits.spawn-visible-agent), or to delete a worktree and tear the workspace down (use daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - worktree.list
  - recipe.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.getStatus
optionalTools:
  - recipe.run
  - terminal.notifyWhenIdle
  - terminal.closeOwned
---
A new worktree with a recipe is `worktree.createWithRecipe` with `recipeId` (asks the user to confirm). A recipe in an EXISTING worktree needs `recipe.run`: a pane is asked first, and an approved prompt starts every terminal while a pre-authorized call starts at most three; the sidebar at core lacks it, so say so rather than substituting a new worktree.

A recipe is a saved set of terminals (shells, previews, agents that start with a saved prompt). It supervises nothing. A slash command is not a recipe, and a recipe whose name matches the user's words does not license swapping its saved behaviour for their brief (plain worktree: daintree.worktree.create-plain-worktree; custom agents: daintree.orchestration.fan-out-work). No action creates, edits, deletes or returns a recipe: for a new or changed one point to Daintree's recipe editor, and offer running an existing recipe or building the layout by hand. Edit `.daintree/recipes/` files only on explicit request, in the format one there shows.

1. Resolve an explicit `worktreeId` (else `recipe.run` lands in the active worktree, which can change). A new worktree's `source` follows daintree.worktree.create-plain-worktree (`collisionPolicy:"error"` for an exact new name).
2. `recipe.list` with `worktreeId`. An empty list with `isLoading:true` means not read yet, not "no recipes": list once more, then report it as still loading. Ask only if two recipes plausibly match. Agents with a saved prompt start immediately, before a new worktree's setup finishes: if setup matters to their task, say so before running.
3. Existing worktree: note its current terminals (`terminal.list`), so this run's are never confused with earlier ones.
4. Run once:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<branch>","baseBranch":"<verified base>","collisionPolicy":"error"},"recipeId":"<id from recipe.list>"}
```

5. Read the outcome per terminal (`spawnedTerminalIds`, failed counts and reasons) against `terminalCount`, and report any shortfall. `PARTIAL_SUCCESS`: the worktree exists; keep it. Never re-run to fill a gap: that duplicates every terminal that started. Reconcile an ambiguous reply with `terminal.list` and `worktree.list` (identical arguments within 120 seconds return the cached result).
6. Classify only this run's terminals in one `terminal.getStatus` with `includeOutput`, `kind` and `agentId` from `terminal.list`. An `agentId` is a candidate, not a healthy CLI; empty output just after launch means starting. Trust, login and approval screens are the user's. Only `spawnedTerminalIds` are yours.
7. New worktree: `worktree.waitUntilReady`; only `ready` is success. If setup is running or failed while an agent already works, warn that its early work may have hit an incomplete tree; do not restart it.
8. Stop unless the user asked to hear when the agents finish; a run authorizes no further prompts. If asked, arm `terminal.notifyWhenIdle` per `working` agent with a note naming the run, and end your turn. `armed:false` returns the current state: report it now.

On notice or resume: its task came from the recipe's saved prompt, not from you. Report what it did and any question, approval or error, judging success only against criteria the user gave. A question beyond the user's authority goes to the user. Finished when every tracked agent is reported.

Stop and recovery: never re-run a recipe after a partial or ambiguous result, and never relaunch a failed recipe agent in a loop. Close nothing unless asked, and then only this run's terminals.

Report back: the recipe and where it ran (worktree id, path, branch, new or existing); terminals started against expected; each terminal's id, kind and state; a new worktree's setup state; whether tracking was arranged and on which terminals.
