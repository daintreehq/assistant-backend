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
  - terminal.getOutput
  - terminal.notifyWhenIdle
  - terminal.sendCommand
---
Use when: agents that are already running share a worktree and the user wants each in its own, keeping their sessions.

Availability: Core, from an agent pane. `terminal.moveToWorktree` works on any terminal in the project, but telling an agent to continue elsewhere needs `terminal.sendCommandOwned`, which reaches only agents you launched or the user handed to you (the sidebar assistant uses `terminal.sendCommand`). An API-key client has no `terminal.moveToWorktree`, `worktree.waitUntilReady` or notices: it can create the worktrees and instruct its own agents, and must leave panel filing to the user. Inspecting and carrying uncommitted changes is native git work in verified paths.

Three facts, kept separate, because none implies another:
- Panel placement: where Daintree files the panel. `terminal.moveToWorktree` changes only this.
- Process root: the directory the agent's process works in. A move never restarts the process, so the agent keeps working where it launched until it is told, and is able, to work elsewhere. Some agent CLIs can work in another directory through explicit paths; some cannot change their root without a restart.
- Daintree binding: a moved agent's own Daintree connection, and its `DAINTREE_WORKTREE_ID`, still name its launch worktree, so its own calls that default to "current" keep targeting the old one.

Procedure:
1. Resolve the cohort. From `terminal.list` and `worktree.list`, keep only the terminals the user meant, and build a map per agent: terminal id, old worktree id and path, new branch and base, and later its destination id and path. `terminal.list` with `owned:true` shows which you can instruct. New agents in fresh worktrees are daintree.edits.spawn-visible-agent or daintree.orchestration.fan-out-work.
2. Find a safe boundary and protect uncommitted work. Read each agent's state with `terminal.getStatus`, and the source tree's changes with `git status` in its path. Several agents' edits are interleaved in one tree, and a new worktree brings none of them along. If an agent's work must travel, that agent moves its own changes, for example by applying a patch of them in the destination, and leaves the source tree as it is. With several writers in one tree, its account of which hunks are its own is evidence, not proof: check its patch against the diff (and any commits or baseline you hold), and report hunks nobody can attribute rather than moving them. Never reset, clean, stash or check out in the shared tree to make room. An agent that is `working` is mid-edit: instruct it only once it is back at its prompt. For each working agent you must instruct, arm `terminal.notifyWhenIdle` with a note naming this relocation and that agent, record the checkpoint, and do steps 5 to 7 for it on its notice.
3. Settle one distinct branch and base per agent, following the user's and the repository's naming. Create an EMPTY worktree per agent, with no `recipeId`, one call each:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<branch for this agent>","baseBranch":"<verified base>","collisionPolicy":"error"}}
```

4. Read every result before moving anything, and keep `worktreeId` and `worktreePath` verbatim; never build one from the other. A lost or partial reply is reconciled with `worktree.list`, not answered with another create. Then run `worktree.waitUntilReady` on each destination: only `ready` is usable. A destination that `failed` or `needs-approval` gets no agent; report it.
5. File each panel under its destination, one call per agent:

```json tool=terminal.moveToWorktree
{"terminalId":"<exact terminal id>","worktreeId":"<destination worktreeId>"}
```

   Panels sharing a tab group travel together, this action cannot split them, and `terminal.list` does not show groups. So snapshot every panel's `worktreeId` from `terminal.list` before the first move, and compare after it. If an unrequested panel moved, or a panel bound for a different destination moved with it, stop moving: report the group and leave the split to the user. Never move it back, which would move the whole group again.
6. Tell each owned agent once, at its prompt:

```json tool=terminal.sendCommandOwned
{"terminalId":"<owned agent terminal id>","command":"Continue this task in <destination worktreePath> on branch <branch>. First run pwd and git branch there and report both. Work only inside that directory from now on, and pass worktreeId <destination worktreeId> on any Daintree call. Leave the original worktree's changes as they are.","notify":true}
```

   Ask for `notify` here: any notice that brought you to the boundary has already fired, so only this one brings its confirmation back (an API-key client omits it and makes one bounded wait). Record the `submissionToken` and that the agent now awaits relocation confirmation before ending your turn. If the prompt does not appear to land, read `terminal.getStatus` with that token before anything else; never resend because the screen has not changed yet. Tell the user you have instructed it, so they do not also use the pane's own "continue in the new worktree" banner. For an agent you do not own, that banner is the path: point the user to it, or ask them to hand the terminal to you.
7. Verify each fact on its own. Placement: `terminal.list`. Root: the agent's reported `pwd` and branch in its output (`terminal.getStatus` with `includeOutput`), and in the end where its new edits land (`git status` in both paths); an "OK" is not evidence, and a `pwd` from one shell command shows only that the command ran there. Source: the retained changes are still there. An agent that cannot change its root keeps its session: report the limit and let the user decide; do not restart or discard it.

On notice or resume:
1. Follow the foundation's resume procedure. A boundary notice's note names the relocation and the agent; the relocation send's notice carries no note, so match it by terminal id.
2. Take the agent's phase from the checkpoint, not from the notice. Awaiting the boundary, and at its prompt with its turn finished: do steps 5 to 7 for it. Awaiting relocation confirmation: do step 7 only, and never move its panel or send the instruction again. If it stopped on a question, an approval or an error, handle that first, and never type the relocation into a question or approval prompt.
3. Record each agent's outcome; a duplicate notice changes nothing.

Stop and recovery: one create per destination and one relocation message per agent. A refused move or send never justifies closing or respawning an agent, raw `git worktree` commands, or deleting the original checkout. Close nothing without a separate request.

Report back: per agent, the destination worktree id, path and branch; whether its panel moved (and anything that moved with it); whether it confirmed the new directory and branch, and where its edits now land; changes retained in the source tree; and any agent not yet instructed (unowned, still working, or blocked) with the next step.
