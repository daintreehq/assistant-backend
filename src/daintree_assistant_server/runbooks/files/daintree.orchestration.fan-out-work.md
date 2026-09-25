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
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listPresets
  - terminal.list
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdleBatch
  - worktree.waitForPullRequest
  - workflow.startWorkOnIssue
  - forge.listIssues
  - forge.getIssue
  - slashCommands.list
  - skills.search
---
Use when: several independent units of work each need their own visible agent at once: a batch of issues (Entry A) or one goal split into disjoint sub-tasks (Entry B), optionally as a rolling queue under a concurrency cap. If the agents would work the same problem, or their outputs must merge into one answer, this is the wrong runbook.

Availability: Core for worktrees, launches, notices and status. The app's issue reads (`forge.listIssues`, `forge.getIssue`), `workflow.startWorkOnIssue`, `slashCommands.list` and `skills.search` raise an approval in a core pane; your host's `gh` is the Hybrid route for issue reads. Commits, pushes and PRs are the children's (or your host's) `git` and `gh`, never app actions. The fleet advances on notices to your pane. Without `notify` (an API-key client) you can only advance it with one bounded `terminal.waitUntilIdleBatch` per activation, and refilling a queue then needs the user's continuation: say so before you start. If your pane exits, its notices end with it; the units keep running and the queue waits for daintree.orchestration.status-and-resume. A pane holds at most 32 pending notices. Daintree has no watchdog for stalled units; only a host mechanism you have verified can provide one.

Procedure:
1. Establish the set and its authority. Check `worktree.list` first: its `issueNumber` and `prNumber` show work already in flight, which you do not duplicate.
   - Entry A: read the issues (the app's forge reads, or `gh issue list` / `gh issue view` with `--repo`).
   - Entry B: split the goal. Investigate as far as safe boundaries and a checkable acceptance per unit require. You may pass on design facts you established, but each child owns its implementation.
   Say the units back as a list before anything mutates. A set the user named exactly is confirmed by stating it; a set you derived or extended needs their yes.
2. Disjointness gate. For every pair of units: do they touch the same files, module or shared artifact (a lockfile, generated schema, single config, migration sequence)? If so they are not parallel: merge them into one unit, serialize them (the second starts only after the first's change is verified and integrated as the user decided), or re-split. Two units that each add a dependency share the lockfile: one owns it and the other names the dependency it needs in its report, or they serialize. A shared file belongs to exactly one unit; say so in that unit's prompt and tell the others to leave it alone. Report the verdict before launching.
3. Concurrency. The user's cap ("no more than 6 at a time") is the authorized concurrency: never exceed it and never ask for it again. With no cap, state how many you will launch, and above about four get a go-ahead unless the user already authorized that scale. A unit holds its slot from launch until the release rule under "On notice or resume", not until it opens a PR.
4. Start one checkpoint (the foundation's) before the first launch, in the coordinator checkout, never in a unit worktree. Record the cap, the slot-release rule, and per unit: label, owned files, brief, acceptance, the resolved command sequence with the evidence each step must produce, then as they arrive the worktree id, path and branch, agent id, `requestedId`, terminal id, step and generation, `submissionToken`, status (`pending`, `setting-up`, `running`, `done`, `failed`, `blocked`), whether it holds a slot, evidence, and whether it was reported. Save each returned id before the next call. The checkpoint is the queue; your memory is not.
5. Setup gate, per unit: create, wait until ready, then launch. Reuse only a worktree already assigned to this unit, and never put two agents in one worktree. Omit `recipeId` so setup starts no terminals of its own:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<unit branch>","baseBranch":"<verified base branch>","collisionPolicy":"error","assignToSelf":false}}
```

```json tool=worktree.waitUntilReady
{"worktreeId":"<returned worktree id>","timeoutMs":25000}
```

   Only `setupState:"ready"` admits a launch. `timedOut:true` means still running: call again a few times, then leave the unit `setting-up` and recheck on your next activation (no notice exists for setup). `failed` or `timed-out` fails that unit with its `error`; `needs-approval` waits for the user to approve setup in Daintree; `unknown` cannot be confirmed. Identical create arguments within two minutes return the first worktree, and a `PARTIAL_SUCCESS` error still created one: reconcile with `worktree.list`, never create a second. If the user asked for Daintree's issue convenience, `workflow.startWorkOnIssue` creates and launches in one call with no setup gate and no `notify` (details in daintree.workflow.start-work-on-issue): read its terminal's state, then arm `terminal.notifyWhenIdle` if it is working, or send the unit brief with `notify` if it waits at its prompt. Never use it for units with named commands; it can submit its own task first.
6. Launch into the ready worktree. Brief each unit per the foundation, adding its owned files, "change nothing outside this scope and report adjacent work you skipped", its acceptance, and exactly what to commit, push or open (only what the user asked for). The launch `prompt` carries the first command of a chain, if any, followed by the unit's entire brief as supplied:

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"<unit label, e.g. auth: retry backoff>","worktreeId":"<ready worktree id>","prompt":"<first command if any, then the full unit brief>","requestedId":"<fresh id for this unit>","notify":true,"handback":true}
```

   Serialize startup, not tasks. Launch the same `agentId` one call at a time, each after the previous call returned; different kinds may launch together. Once admitted, units work concurrently: never wait for one unit's task to finish before launching the next. `launched:false` with null ids is a refusal, not a running unit. Identical arguments within two minutes return that same refusal, so retry once, after the earlier same-kind launch returned, with a fresh `requestedId`. A lost reply is reconciled with `terminal.list` by `requestedId` before anything else. `spawnStatus:"missing-cli"` blocks that unit. Save the terminal id and mark the slot taken.
7. Named commands and skills. Resolve each requested command for the chosen agent in the unit's own worktree before its first submission: `slashCommands.list` with that agent and the worktree path as `projectPath`, `skills.search` for plugin skills, or the agent's own command and skill files, which your file tools can read. Send the resolved token unchanged (a Codex `$skill` included). Several matches, required arguments nobody supplied, or no match block that unit only; report "not found", not "does not exist". Only when no lookup is possible and the user asked for their token to be sent as written, send it verbatim and report it unverified. Never start a similarly named recipe and then send the command: the recipe may already have submitted its own prompt.
8. Ordered commands (implement, then review, then prepare the PR). Only the first goes in the launch prompt. Each later command is one send, made only after the previous step's evidence is verified. Never put several commands in one submission, and never send successive steps in one batch:

```json tool=terminal.sendCommandOwned
{"terminalId":"<unit terminal id>","command":"<next resolved command and its arguments>","notify":true,"handback":true}
```

   Keep its `submissionToken` and bump the unit's generation. Idle, a picker or question, a failed command, a startup crash or an unconfirmed submission never unlocks the next command.
9. Report what is running and end your turn. Notices bring you back.

On notice or resume:
1. Follow the foundation's resume procedure. One notice line can name several terminals: map each to its unit, step and generation, and handle each on its own. A terminal the checkpoint does not hold is not this queue's work.
2. Read every named unit in one call, then what you need of each (`terminal.readLastMessageOwned` for a Claude Code pane, otherwise `terminal.getOutput`):

```json tool=terminal.getStatus
{"terminalIds":["<unit terminal id>","<unit terminal id>"],"includeOutput":{"lines":50}}
```

3. Verify the step's evidence yourself in that unit's worktree path (its diff, commits, test run), never in your own checkout. Then advance that unit only: send its next command, or record it `done`, `failed` or `blocked` with the evidence. One failed unit stops its own chain, never its siblings.
4. Release and refill. A slot frees when the unit's final step is verified, or when the unit is recorded failed or blocked and its agent is no longer working, or by a different rule the user set and you recorded. A detected PR is not a finished agent: a `prNumber` in `worktree.list` or a `worktree.waitForPullRequest` hit says a PR was seen, and an agent still working keeps its slot. Then take pending units, through the setup gate and startup serialization, until live units equal the cap. Refill only on these verified transitions, never on elapsed time or a bare notice.
5. Save the checkpoint after each transition and before the next dispatch. Report each unit's result once.

Rules for the cases that go wrong:
- Thirty units, cap three: three live, twenty-seven `pending` in the checkpoint with their briefs. Each verified release launches exactly one pending unit. No timer drives the queue; notices (or the user) do, and the counts come from the checkpoint.
- Stalled worker: an agent that never stops sends no notice, and notices never expire. A unit that reads `working` with an old `lastOutputChangeAt` while you handle others is reported as possibly stalled. It keeps its slot; do not relaunch it or release it. Interrupting or recovering it (daintree.agents.recover-agent) needs the user's word.
- Duplicate notice: a notice for a unit whose current generation is already recorded advanced or done produces nothing: no second send, no second refill, no second report. A launch or send that asked for `notify` already has its notice, so never also arm `terminal.notifyWhenIdle` on it.
- Shared lockfile: settled at the gate, never at merge time. If two units still collide, hold the later unit's next step and report the collision.

Stop and recovery: at most one corrective prompt per unit unless the user set another bound. Never relaunch a unit to repair tracking. A unit still working whose notice was lost (your pane relaunched) gets one `terminal.notifyWhenIdle` with the run id and unit in its `note`; `armed:false` means it already stopped, so read it now. A relaunched pane owns none of the units, so further sends need the user's hand-over. A disputed send is checked by its `submissionToken` before anything is resent. Integration across branches, if requested, is a separate serialized step verified on the combined tree. On cancellation keep a tombstone, launch nothing more, and report which units are still working; closing or deleting anything belongs to daintree.orchestration.workspace-cleanup.

Report back: the gate's verdict (merged, serialized, re-split), then per unit its label, worktree id and branch, terminal id, current step, status and the evidence behind it; N of M done; what is still running or pending; and the continuation (notices requested on which terminals, or none and why).
