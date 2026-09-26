---
id: daintree.orchestration.fan-out-work
title: Fan out disjoint work across parallel agents
summary: Launch one agent terminal per independent GitHub issue or task, each in its own worktree. Look up custom commands and skills, run each terminal's commands in order with verification, and supervise the parallel issue chains independently.
whenToUse: Use for a BATCH of independent issues or tasks, one agent terminal per issue or worktree. Includes Codex or Claude custom command/skill sequences — implement each issue, then review it, then prepare its PR — with each terminal advancing independently and a failed issue stopping only its own chain. Also use to split one goal into disjoint file-scoped tasks or open a coding-agent terminal in each existing issue worktree. PLURALITY plus INDEPENDENCE is the signal. Also a ROLLING QUEUE under a cap — "no more than 6 at a time", "start the next one whenever one finishes", "keep going until all 30 are done" — including refilling it as each unit finishes — a list of work plus a concurrency cap is this runbook, not a timer or PR runbook. Exclude one issue (daintree.workflow.start-work-on-issue), one agent (daintree.edits.spawn-visible-agent), agents collaborating on the same deliverable (daintree.orchestration.multi-agent), rival solutions (daintree.orchestration.competition), supervising/resuming a fleet already running (daintree.orchestration.supervise-fleet, daintree.orchestration.status-and-resume), opinions rather than work (daintree.agents.poll-pinned-agents), rescuing one stuck agent (daintree.agents.recover-agent), or separating agents that are ALREADY RUNNING into a worktree each, where the terminals exist and are to be relocated rather than replaced (daintree.worktree.relocate-agents).
risk: project
requiredTools:
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - terminal.sendCommandOwned
optionalTools:
  - terminal.list
  - terminal.getStatus
  - workflow.startWorkOnIssue
  - forge.listIssues
  - forge.getIssue
  - slashCommands.list
  - skills.search
---
The queue advances only on notices; an API-key client has none, so refilling needs the user: say so first. A pane holds at most 32 pending notices, so never run a cohort beyond that. There is no watchdog for stalled units.

Procedure:
1. The set. Skip work already in flight (`worktree.list` `issueNumber`, `prNumber`). Read the issues, or split the goal into units with a checkable acceptance each. A set you derived or extended needs the user's yes.
2. Disjointness gate. Units touching the same files, module or shared artifact (a lockfile, generated schema, single config, migration sequence) are not parallel: merge, serialize or re-split. Two units adding dependencies share the lockfile: one owns it, the other reports the dependency it needs. Report the verdict.
3. Cap. The user's cap is the authority; never exceed it or ask again. With none, state yours and get a go-ahead above about four.
4. Resolve every unit's first prompt before any mutation: a named command or skill is looked up for that agent in the unit's worktree and sent unchanged. No match, several, or missing arguments blocks that unit with zero submissions.
5. Checkpoint (this run is long). Per unit: label, owned files, brief, acceptance, command chain, worktree, `requestedId`, terminal id, step, status (`pending`, `setting-up`, `dispatching`, `running`, `blocked`, `done`, `failed`, `uncertain`) and whether it holds a slot. Save `dispatching` before each launch or send.
6. Setup gate per unit: create, wait until ready, then launch. `worktree.createWithRecipe` with a new branch, no `recipeId`, `collisionPolicy:"error"`; one agent per worktree. Only `setupState:"ready"` admits a launch. A setup failure stops before launch and fails that unit; `needs-approval` waits for the user. `timedOut` leaves it `setting-up`: setup sends no notice, so with nothing running say the queue needs the user. Reconcile a lost create with `worktree.list`. `workflow.startWorkOnIssue` only when asked and the unit has no named commands: it skips this gate and may submit its own task, so read its terminal before sending.
7. Launch every admitted unit this turn with `notify:true, handback:true` and a fresh `requestedId`, then end it. The brief adds owned files, "change nothing outside this scope", what to commit or open, and a report of at most about 30 lines.

On notice or resume:
1. Map each terminal to its unit by the checkpoint; one not there is not this queue's.
2. Verify the step in that unit's worktree path (diff, commits, tests), then advance that unit only: its next command is one send with `notify:true, handback:true`, never several at once.

```json tool=terminal.sendCommandOwned
{"terminalId":"<unit terminal id>","command":"<next resolved command>","notify":true,"handback":true}
```

   Idle, a question, a failed command or an unconfirmed submission never unlocks the next command. A failed unit stops only its own chain.
3. Release and refill. A slot frees when the unit's last step is verified, or it is failed or blocked and no longer working. A detected PR is not a finished agent: a working agent keeps its slot. A blocked unit re-reserves a slot before it is answered. An uncertain launch keeps its slot until reconciled by `requestedId`. Refill through the gate only on these transitions. Save after each.

Stop and recovery: one corrective prompt per unit unless the user set another bound. A `working` unit with an old `lastOutputChangeAt` is reported possibly stalled and keeps its slot; recovering it (daintree.agents.recover-agent) needs the user. A late lockfile collision holds the later unit. Never relaunch to repair tracking. Integrating branches is a separate serialized step verified on the combined tree. On cancellation launch nothing more; closing is daintree.orchestration.workspace-cleanup.

Report back: the gate's verdict; per unit its label, worktree and branch, terminal id, step, status and evidence; N of M done; what is running or pending and what will wake the queue.
