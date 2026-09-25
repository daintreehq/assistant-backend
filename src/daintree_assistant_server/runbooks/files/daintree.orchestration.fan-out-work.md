---
id: daintree.orchestration.fan-out-work
title: Fan out disjoint work across parallel agents
summary: Launch one agent terminal per independent GitHub issue or task, each in its own worktree. Look up custom commands and skills, run each terminal's commands in order with verification, and supervise the parallel issue chains independently.
whenToUse: Use for a BATCH of independent issues or tasks, one agent terminal per issue or worktree. Includes Codex or Claude custom command/skill sequences — implement each issue, then review it, then prepare its PR — with each terminal advancing independently and a failed issue stopping only its own chain. Also use to split one goal into disjoint file-scoped tasks or open a coding-agent terminal in each existing issue worktree. PLURALITY plus INDEPENDENCE is the signal. Also a ROLLING QUEUE under a cap — "no more than 6 at a time", "start the next one whenever one finishes", "keep going until all 30 are done" — including refilling it as each unit finishes — a list of work plus a concurrency cap is this runbook, not a timer or PR runbook. Exclude one issue (daintree.workflow.start-work-on-issue), one agent (daintree.edits.spawn-visible-agent), agents collaborating on the same deliverable (daintree.orchestration.multi-agent), rival solutions (daintree.orchestration.competition), supervising/resuming a fleet already running (daintree.orchestration.supervise-fleet, daintree.orchestration.status-and-resume), opinions rather than work (daintree.agents.poll-pinned-agents), rescuing one stuck agent (daintree.agents.recover-agent), or separating agents that are ALREADY RUNNING into a worktree each, where the terminals exist and are to be relocated rather than replaced (daintree.worktree.relocate-agents).
risk: project
requiredTools:
  - agent.listAvailable
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - terminal.sendCommandOwned
optionalTools:
  - terminal.list
  - terminal.getStatus
  - terminal.notifyWhenIdle
  - terminal.waitUntilIdleBatch
  - worktree.waitForPullRequest
  - workflow.startWorkOnIssue
  - forge.listIssues
  - forge.getIssue
  - slashCommands.list
  - skills.search
---
Use when: several independent units of work each need their own visible agent: a batch of issues, or one goal split into disjoint sub-tasks, optionally as a rolling queue under a concurrency cap. Agents working the same problem, or outputs merging into one answer, is the wrong runbook.

Availability: Core for worktrees, launches and notices; the app's issue reads, `slashCommands.list`, `skills.search` and `workflow.startWorkOnIssue` raise an approval in a core pane (your `gh` reads issues too). Commits, pushes and PRs are the children's `git` and `gh`. The queue advances only on notices: with no `notify` (API-key client), each activation gets one bounded `terminal.waitUntilIdleBatch` and refilling needs the user; say so first. A pane holds at most 32 pending notices, other runs' included, so never start a notify cohort beyond that. There is no watchdog for stalled units.

Procedure:
1. The set. Check `worktree.list` (`issueNumber`, `prNumber`) and skip work already in flight. Read the issues, or split the goal into units with a checkable acceptance each. State the list; a set you derived or extended needs the user's yes.
2. Disjointness gate. Units touching the same files, module or shared artifact (a lockfile, generated schema, single config, migration sequence) are not parallel: merge, serialize, or re-split. Two units adding dependencies share the lockfile: one owns it and the other reports the dependency it needs. Report the verdict.
3. Cap. The user's cap is the authority; never exceed it or ask again. With none, state how many you will run and get a go-ahead above about four.
4. Resolve every unit's first prompt before any mutation. A named command or skill is looked up for that agent in the unit's worktree (`slashCommands.list` with `projectPath`, `skills.search`, or its command files) and sent unchanged. No match, several, or missing arguments blocks that unit with zero submissions; report "not found", not "does not exist".
5. Checkpoint (per the foundation; this run is long). Record the cap and per unit: label, owned files, brief, acceptance, command chain, and as they arrive worktree id and path, `requestedId`, terminal id, step, status (`pending`, `setting-up`, `dispatching`, `running`, `blocked`, `done`, `failed`, `uncertain`) and whether it holds a slot. Reserve the slot and save `dispatching` before each launch or resuming send.
6. Setup gate per unit: create, wait until ready, then launch. Omit `recipeId`; never two agents in one worktree.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<unit branch>","baseBranch":"<verified base branch>","collisionPolicy":"error","assignToSelf":false}}
```

```json tool=worktree.waitUntilReady
{"worktreeId":"<returned worktree id>","timeoutMs":25000}
```

   Only `setupState:"ready"` admits a launch. A setup failure stops before launch and fails that unit; `needs-approval` waits for the user. `timedOut:true` leaves it `setting-up`: no notice exists for setup, so if no unit is running, nothing will wake you; say the queue needs the user to continue. Reconcile a lost create with `worktree.list`, never create twice. Use `workflow.startWorkOnIssue` only when the user asked for it and the unit has no named commands: it skips this gate and may submit its own task, so read its terminal before sending anything.
7. Launch every admitted unit in this turn, then end it. The brief adds owned files, "change nothing outside this scope", the acceptance, what to commit or open, and a report of at most about 30 lines.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"<unit label>","worktreeId":"<ready worktree id>","prompt":"<first command if any, then the full unit brief>","requestedId":"<fresh id for this unit>","focusPolicy":"preserve","notify":true,"handback":true}
```

On notice or resume:
1. Each named terminal maps to its unit by the checkpoint; one not there is not this queue's. Read the quote; the foundation says when to read again.
2. Verify the step in that unit's worktree path (diff, commits, tests), never your own checkout. Then advance that unit only: its next command is one send, only after the previous step's evidence is verified, never several commands at once.

```json tool=terminal.sendCommandOwned
{"terminalId":"<unit terminal id>","command":"<next resolved command>","notify":true,"handback":true}
```

   Idle, a question, a failed command or an unconfirmed submission never unlocks the next command. One failed unit stops its own chain, never its siblings.
3. Release and refill. A slot frees when the unit's last step is verified, or it is failed or blocked and no longer working. A detected PR is not a finished agent: a working agent keeps its slot. A blocked unit that released its slot re-reserves one before it is answered. An uncertain launch keeps its slot until reconciled with `terminal.list` by `requestedId`. Refill pending units through the gate only on these verified transitions.
4. Save the checkpoint after each transition; report each unit once.

Stop and recovery: one corrective prompt per unit unless the user set another bound. A duplicate notice for a step already advanced produces nothing. A unit `working` with an old `lastOutputChangeAt` is reported possibly stalled and keeps its slot; recovering it (daintree.agents.recover-agent) needs the user. A lockfile collision found late holds the later unit's next step. Never relaunch to repair tracking. Integrating branches is a separate serialized step verified on the combined tree; per-branch passes do not prove it. On cancellation, tombstone the run, launch nothing more, and leave closing to daintree.orchestration.workspace-cleanup.

Report back: the gate's verdict, then per unit its label, worktree and branch, terminal id, step, status and evidence; N of M done; what is running or pending; and what will wake the queue.
