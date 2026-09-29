---
id: daintree.orchestration.reprioritize-work
title: Reprioritize running agents around urgent work
summary: Something became urgent while agents are busy — decide per running agent whether it keeps going, pauses at a clean point with a resume note, or is redirected, and put the freed capacity on the urgent work in its own worktree without losing anyone's work.
whenToUse: 'Use when something became URGENT while agents are already busy and the user wants the running work re-planned around it — "this bug is now urgent, pause the less important work and put agents on it", "drop what the agents are doing, prod checkout is broken, get two of them on it", "stop the docs agents and move them onto the release blocker", "the demo is in an hour, reshuffle the agents". The deliverable is a revised allocation across the existing agents: each one kept, paused at a clean point with where it stopped and how to resume, or redirected; the urgent work started in its own worktree; nothing lost. Do not use when everyone stops and no new work starts (use daintree.orchestration.park-work), to correct one agent on its own task (use daintree.agents.steer-running-agent), for a status sweep with no reallocation (use daintree.orchestration.supervise-fleet), to launch a new batch with nothing paused (use daintree.orchestration.fan-out-work), to move one task to a different agent (use daintree.agents.hand-off-task), to reorder the issue backlog itself (use daintree.forge.triage-issue-backlog), or for the urgent work alone when nothing running gives way: a red check (use daintree.ci.diagnose-and-fix), a live production incident (use daintree.debug.investigate-outage).'
risk: project
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandMany
  - agent.launch
optionalTools:
  - terminal.interruptOwned
  - terminal.sendCommandOwned
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.setClientMetadata
---
Reallocation, not teardown: a paused agent's worktree stays exactly as it left it; the urgent work gets its own.

Procedure:
1. The ask: the urgent task, how many agents it gets ("two of them"), and which work gives way. Apply the user's ranking ("the docs agents", "the less important work") to the inventory; pick the smallest sufficient set, and ask first only when two plausible picks differ materially.
2. Inventory: `worktree.list`, `terminal.list` (`owned:true`, then unfiltered for the rest), one `terminal.getStatus` with `includeOutput:{"lines":30}` over the agents. Per agent: its task (your record or its screen), worktree and state; per worktree `git -C "<path>" status --short`.
3. Decide each agent: keep (the default for anything the user did not name), pause, or redirect. Only agents you launched or were handed can be paused or redirected; list the rest with the words for the user to type in their panes. An agent waiting on a question keeps it: record the question in its resume note, never answer it with the pause. Redirect only an agent the user wants on the urgent work for what it knows; otherwise pause it and launch fresh, keeping its context for the resume.
4. Pause in one `terminal.sendCommandMany` with `notify:true, handback:true`. A send is queued: it is read when the agent's current turn ends, so count the agent as running until its reply confirms the stop. "Drop it now", prod broken: `terminal.interruptOwned` each first and confirm the stop from its screen; an unconfirmed stop gets nothing that could restart work, and an interrupted agent's message adds "You were stopped mid-edit: re-read <files> first."

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<agent terminal id>","command":"Priority change: pause your task at a safe point. Leave partial edits in place and revert nothing; start nothing new, make no commits. Reply in prose: what is done, what is half-done, every uncommitted or incomplete file, and the exact next step to resume."}],"notify":true,"handback":true}
```

   A checkpoint commit only when the user allowed one; then add "Commit as wip: <summary>, staging by path; no secrets, never --no-verify or git stash."
5. The urgent work, now: fresh agents need nothing from the pauses; a redirected agent starts only when its current turn ends. Its worktree: the user's, else `worktree.createWithRecipe` with no `recipeId`, `source:{"kind":"newBranch","branchName":"<unique urgent branch>","collisionPolicy":"error"}` (base defaults to the main branch; pass `baseBranch` when the fix belongs elsewhere, never a paused agent's branch). Keep the returned `worktreeId`; `worktree.waitUntilReady` until `ready` (`timedOut`: call again; `failed` or `needs-approval`: stop and tell the user). Launch the number the user gave, one `agent.launch` each, with distinct `name`s and parts. One writer per worktree: a second agent on the same task reproduces or reads the recent changes and edits nothing, or gets its own worktree. Brief and verify under the runbook that owns that work (a red check, an incident, a change). Urgent is not unbounded: no push, deploy or merge unless the user said so.

```json tool=agent.launch
{"agentId":"claude","name":"Urgent: checkout 500s","worktreeId":"<urgent worktree id>","prompt":"<urgent brief: task, acceptance, scope, write or read-only, what to report>","notify":true,"handback":true}
```

   A redirected agent's process stays in its old directory (moving its panel changes nothing), so its brief says: "Work only in <absolute path>; every write must target it; leave your own worktree as it is; first run pwd and git -C <path> status."
6. Tell the user the allocation and end the turn.

On notice or resume:
- A pause reply: check it against `git -C "<path>" status --short` and `log -1 --oneline`, then record its resume note: task, worktree, branch, HEAD, uncommitted files, pending question, next step. A multi-session run's goes in its checkpoint, optionally on the panel with `terminal.setClientMetadata` (merged with its record). Still working after the pause: one reminder, then report it unconfirmed.
- An urgent agent's notice: verify under its runbook.
- "Put them back": send each paused agent its own note: "Resume. First run git status and confirm it matches, then continue from: <next step>", with `notify:true`. Resume only on the user's word; the urgent work finishing is not that word.

Stop and recovery: one pause message and one reminder per agent; one launch per urgent slot, reconciled with `terminal.list` before any second. Never reset, stash, clean, close or delete to free capacity.

Report back: per agent kept, paused (with its resume note and how the stop was confirmed), redirected, or not yours; the urgent worktree, branch and agents with terminal ids; what is still pending and what will bring you back.
