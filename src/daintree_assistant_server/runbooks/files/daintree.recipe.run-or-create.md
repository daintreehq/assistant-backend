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
  - terminal.getOutput
  - terminal.notifyWhenIdle
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned
---
Use when: the user wants a saved Daintree recipe listed, run in a worktree, or used as the startup recipe of a new worktree.

Availability: Core for a new worktree with a recipe: `worktree.createWithRecipe` with `recipeId`, which asks the user to confirm. Running a recipe in an EXISTING worktree needs `recipe.run`. An agent pane is offered it, but it asks the user first: in the captured Daintree build, approving that prompt starts every terminal the recipe lists, while a call already pre-authorized (for example after "Allow for this session") starts at most three. The sidebar assistant at core is not offered it: say so, and offer a new worktree with the recipe as the different outcome it is, rather than substituting it. `USER_REJECTED` means the user declined: nothing ran; do not retry. Tracking a recipe's agents afterwards uses `terminal.notifyWhenIdle`, which an API-key client does not have.

A recipe is a saved set of terminals: plain shells, dev previews, or agents that start their CLI the moment they spawn, often with a saved prompt. It sets up a workspace and supervises nothing. A slash command such as `/design-review` is an agent command, not a recipe, and a recipe whose name matches the user's words does not license swapping its saved behaviour for the user's own brief: a plain worktree is daintree.worktree.create-plain-worktree, and custom agents for custom tasks are daintree.orchestration.fan-out-work.

No Daintree action creates, edits or deletes a recipe, and none returns a recipe's definition. Asked for a new or changed recipe, say so plainly, point the user to Daintree's recipe editor, and offer what you can do: run an existing recipe, or build the layout once by hand (a worktree, then `agent.launch` per agent). Project recipes can live in the repository under `.daintree/recipes/`; editing one is not this procedure, so do it only on an explicit request and in the format an existing file there shows.

Procedure:
1. Pin the target. `recipe.run` otherwise lands in the user's active worktree, which can change under you, so resolve an explicit `worktreeId` from `worktree.list` or the user's words. For a new worktree, settle its `source`: `newBranch` with `branchName`, `baseBranch` and `collisionPolicy:"error"` for an exact new name; `existingBranch` for a local branch checked out verbatim; or `pullRequest` with `pullRequestNumber` alone.
2. Discover with `recipe.list`, passing `worktreeId` for the recipes available there. Entries carry `id`, `name`, `terminalCount` and `origin`. An empty list with `isLoading:true` means not read yet, not "no recipes": list once more, and if it is still loading report that instead of an empty catalog. Resolve the recipe the user named; ask only if two plausibly match. `terminalCount` says how many terminals will start, not what they are or what their agents will do. Agents with a saved prompt begin work immediately, so do not plan to brief them afterwards. In a new worktree they also start before setup finishes: when setup matters to their task (dependencies, a database), say so before running, because checking afterwards cannot undo early work.
3. For an existing worktree, note which terminals are already there (`terminal.list` with its `worktreeId`), so this run's terminals are never confused with earlier ones.
4. Run once in an existing worktree:

```json tool=recipe.run
{"recipeId":"<id from recipe.list>","worktreeId":"<resolved worktree id>"}
```

   or create a new worktree with it:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<branch>","baseBranch":"<verified base>","collisionPolicy":"error"},"recipeId":"<id from recipe.list>"}
```

5. Read the outcome terminal by terminal. `recipe.run` returns `spawnedCount`, `failedCount`, `spawnedTerminalIds` and `failedTerminals` (`index`, `reason`). `worktree.createWithRecipe` returns `worktreeId`, `worktreePath`, `branch`, `recipeLaunched`, `spawnedTerminalIds`, `spawnedTerminalCount` and `failedTerminalCount`; a `PARTIAL_SUCCESS` error means the worktree exists and the recipe failed partway, so keep and report the worktree. Compare what started with `terminalCount` and report any shortfall, including the cap on a pre-authorized run. A partial run is a normal result. Never re-run the recipe to fill a gap: that duplicates every terminal that did start. An ambiguous reply is reconciled with `terminal.list` and `worktree.list` first; identical arguments within 120 seconds only return the cached earlier result.
6. Classify only this run's terminals, in one call:

```json tool=terminal.getStatus
{"terminalIds":["<spawned id 1>","<spawned id 2>"],"includeOutput":{"lines":20}}
```

   Take each one's `kind` and `agentId` from `terminal.list`. A shell or dev preview needs nothing more. An entry with an `agentId` is an agent candidate, not proof of a healthy CLI; an agent prints nothing for its first seconds, so empty output right after the run means starting, not dead. A trust, login or approval screen is the user's to answer, and an error is reported as seen. Only the ids in `spawnedTerminalIds` are yours: other terminals in that worktree are not, and owned tools refuse them. Yours is provenance, not a guarantee: after your pane relaunches or Daintree restarts, owned tools refuse these too; take the refusal as final rather than routing round it.
7. For a new worktree, check setup with `worktree.waitUntilReady` (`timeoutMs` 0 to 25000; only `ready` is success); an API-key client, which lacks it, reads `setupState` from `worktree.list`. Recipe terminals do not wait for setup, and this check cannot stop them: if setup is still running, or `failed`, while an agent is already working, tell the user that agent's early work may have run against an incomplete tree. Do not restart the agent yourself.
8. Stop here unless the user asked to hear when the recipe's agents finish. A recipe run authorizes no further prompts to its agents.
9. Tracking, when asked. For each agent that is `working`, arm one notice with a note naming the run, record a checkpoint (recipe, worktree id and path, terminal ids, what the user wants reported), and end your turn:

```json tool=terminal.notifyWhenIdle
{"terminalId":"<agent id from spawnedTerminalIds>","note":"recipe <name>, run <run id>: report its result"}
```

   `armed:false` comes back with the terminal's current state instead: report that state now rather than waiting. Without `terminal.notifyWhenIdle` (an API-key client), use one `terminal.waitUntilIdleBatch` over those ids, then report what is still working and that checking again needs the user's continuation.

On notice or resume:
1. Follow the foundation's resume procedure, correlating by terminal id and the note. The notice says a terminal stopped, not that its work succeeded.
2. Read that agent's state and output. Its task came from the recipe's saved prompt, not from you: report what it did and any question, approval or error it stopped on, and judge success only against criteria the user gave.
3. A question beyond what the user authorized goes to the user; never answer it just to keep the agent moving.
4. Mark each terminal reported so a duplicate notice adds nothing. The run is finished when every tracked agent has been reported.

Stop and recovery: never re-run a recipe after a partial or ambiguous result; recover a missing terminal only on request. A failed recipe agent is not relaunched in a loop. One notice per terminal. A declined approval ends the request. Close nothing unless asked, and then only this run's terminals, with `terminal.closeOwned`.

Report back: the recipe and where it ran (worktree id, path and branch, new or existing), terminals started against expected, each terminal's id, kind and state (live agent, shell, starting, blocked, or failed with its reason), the setup state of a new worktree, and whether tracking was arranged (a notice on which terminals, or none and why).
