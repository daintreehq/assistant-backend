---
id: daintree.hosts.do-delegated-work
title: 'Implement the hub''s unit of work in a new worktree'
summary: 'On a Remote Host''s lane, implement one unit the hub delegated by creating a worktree on its branch from the base commit and launching the agent it names there with the objective, then check the agent''s commit.'
whenToUse: 'Use on a Remote Host''s lane when the hub asks THIS machine to implement, fix or build something as one unit of work: "the hub asked me to create a worktree and have Claude write X", "hub message: implement this on its run branch and reply with the SHA", "the hub wants an agent here to fix the parser in a new worktree". The deliverable is the unit implemented here by a visible agent: a new worktree on the brief''s branch from its base commit, the agent CLI the brief names launched in it with the hub''s objective and acceptance, a setup or launch that half happened picked up rather than repeated, and the agent''s commit checked by the lane. Do not use for reading, judging or answering the hub''s message itself (daintree.hosts.handle-hub-message), for the same work asked by the person at this machine (daintree.edits.spawn-visible-agent; several units, daintree.orchestration.fan-out-work; a started task moved to another agent, daintree.agents.hand-off-task), or for handing work to another machine from the hub (daintree.hosts.delegate-to-host).'
risk: project
requiredTools:
  - actions.getContext
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - terminal.waitUntilIdle
  - terminal.getStatus
  - terminal.sendKeys
  - assistant.reply
optionalTools:
  - slashCommands.list
  - terminal.sendCommand
---
After daintree.hosts.handle-hub-message has read and fitted it: one hub unit, by a visible agent here; never a batch.

1. Pin from the brief: run and unit ids, remote, base SHA, unit branch, CLI, checks, push or PR authority; gaps: ask back. Git runs as `git -C <project path>` (`actions.getContext`): `remote get-url origin` must match (else ask back); `cat-file -e <SHA>`, else `fetch` once, else blocker.
2. Reconcile first: `worktree.list`, `git worktree list`, `terminal.getStatus` on `<run>-<unit>`. What the unit has is resumed, never remade; its branch alone, still at the base SHA, is `existingBranch`; else blocker.
3. Create, then `worktree.waitUntilReady`: only `ready` admits the launch, else (after a few `timedOut`) blocker. No `recipeId`: its terminals start before setup; a named recipe: ask back.

```json tool=worktree.createWithRecipe surface=lane-full
{"source":{"kind":"newBranch","branchName":"<unit branch>","baseBranch":"<base SHA>","collisionPolicy":"error"}}
```

4. Launch once. Brief: the hub's objective and acceptance verbatim; commit on this branch only; no push, merge or delete unless the brief says; the checks exactly as named; a named command or skill first, resolved here for that CLI (`slashCommands.list` `agentId`, `projectPath`), else ask back; report files, SHA, each check's command and exit, any not run.

```json tool=agent.launch surface=lane-full
{"agentId":"<brief's CLI>","name":"<unit>","requestedId":"<run>-<unit>","worktreeId":"<worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

5. Startup: `terminal.waitUntilIdle` (30 s), then `terminal.getStatus` with `includeOutput`, a few rounds, until it works or shows a dialog. Folder trust: `terminal.sendKeys` `choose` its trusting label ("Yes, I trust this folder"; Codex "Yes, continue"), `notify:true`, never Enter, which may pick "No, exit"; still shown: blocker. End the turn.

On notice or resume: a dialog, as step 5. Done: in the worktree `git status --porcelain`, `git log -1 --stat`, `git merge-base --is-ancestor <base SHA> HEAD`, rerun the checks as named; one that cannot run is reported, never replaced. Unmet: one `terminal.sendCommand` correction, `notify`, `handback`; end the turn, verify again. Then reply; leave the pane open.

Stop and recovery: one create, one launch, one correction per unit; the reply names what stays unmet.

Report back: branch, SHA, checks run vs claimed, pane id.
