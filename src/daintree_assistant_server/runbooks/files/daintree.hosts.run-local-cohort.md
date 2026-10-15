---
id: daintree.hosts.run-local-cohort
title: Run several pieces from the hub on this machine's agents
summary: 'Given several pieces by the hub at once (independent units, or rival attempts it asked for), give each its own worktree, branch and visible agent within the hub''s cap and this machine''s load, verify each, and send the hub one reply with a row per piece.'
whenToUse: 'Use on a lane when the hub hands THIS machine SEVERAL pieces of work at once: "the hub sent these four issues to this machine", "the hub wants three attempts at this here and the best one reported", "run my share of the swarm in separate local worktrees and send the hub one result", a batch of independent units for this project run as a local cohort, or rival attempts at one task when the hub asked for more than one. The deliverable is ONE reply to the hub with a row per piece (worktree, branch, commit SHA, checks, pane id), each piece verified here, failures and open questions in the same reply, and for attempts the best one named with evidence: every piece in its own worktree and branch from the base commit, the CLI the hub named or one signed in here, no more at once than the hub''s cap and this machine allow, and units writing disjoint files. Do not use for one unit of work, or one fix with no attempts asked for (daintree.hosts.do-delegated-work), reading the hub''s message, or answering one that sends no work here (daintree.hosts.handle-hub-message), the same split asked for by the person at this machine (daintree.orchestration.fan-out-work; rival entries, daintree.orchestration.competition; options to prototype, daintree.orchestration.spike-options; a spec to break down first, daintree.orchestration.decompose-work), changing or stopping agents already running for the hub (daintree.hosts.steer-for-hub), or the hub sending work to every host (daintree.hosts.swarm-fan-out).'
risk: project
requiredTools:
  - actions.getContext
  - terminal.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - terminal.getStatus
  - assistant.reply
optionalTools:
  - slashCommands.list
  - terminal.sendKeys
  - terminal.sendCommand
  - worktree.list
  - terminal.notifyWhenIdle
---
After daintree.hosts.handle-hub-message fitted it; keep its `taskId`.

1. Pieces: the hub's list, this project only. Attempts only if it asked for several (one fix: daintree.hosts.do-delegated-work), each with its assigned approach. Units share no files (lockfiles count), else ask back. `git -C <project path>` (`actions.getContext`): origin is the brief's remote; `cat-file -e <base SHA>`, else `fetch` once.
2. Slots: the hub's cap at most, fewer while this project's agents work (`terminal.list`); no cap, about four; under 32 notices pending. Others queue.
3. Per admitted piece: `worktree.createWithRecipe` (new branch `<run>/<piece>` from the base SHA, no `recipeId`, `collisionPolicy:"error"`), `worktree.waitUntilReady` to `ready` (`timedOut`: again, a few times), a named command or skill resolved for its CLI (`slashCommands.list`), then:

```json tool=agent.launch surface=lane-full
{"agentId":"<hub's CLI, else a ready one>","name":"<run> <piece>","requestedId":"<run>-<piece>","worktreeId":"<worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   Brief: the hub's objective, acceptance and limits verbatim; commit here; its checks; report SHA and exits.
4. Paced `terminal.getStatus` `includeOutput` rounds until each works or shows a dialog: trust prompt: `terminal.sendKeys` `choose`, `notify:true`; still stuck, hold it. End the turn.

On notice or resume: map the terminal to its piece; a question beyond the brief holds it. Done: in its worktree `git status --porcelain`, `git log -1 --stat`, `git merge-base --is-ancestor <base SHA> HEAD`, rerun checks; unmet: one correction (`terminal.sendCommand`, `notify`, `handback`). Verified, failed or held with its agent stopped frees a slot: launch the next. Attempts: the best verified one on the hub's criterion, with evidence, or a tie.

Stop and recovery: before a create or launch, and after a restart, reconcile with `worktree.list` and `terminal.getStatus` (`<run>-<piece>`): reuse a made worktree; re-arm a working agent (`terminal.notifyWhenIdle`); launch a missing one only on a clean worktree at the base SHA, else hold it.

Report back: once nothing runs or queues, one `assistant.reply`: `completed`, or `input-required` with the held questions; per piece worktree, branch, SHA claimed vs verified, checks, pane id; the best attempt. The hub's answer is a new exchange: resume held pieces in free slots; reply there with the whole table.
