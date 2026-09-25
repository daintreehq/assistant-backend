---
id: daintree.orchestration.fan-out-work
title: Fan out disjoint work across parallel agents
summary: Launch one agent terminal per independent GitHub issue or task, each in its own worktree. Look up custom commands and skills, run each terminal's commands in order with verification, and supervise the parallel issue chains independently.
whenToUse: Use for a BATCH of independent issues or tasks, one agent terminal per issue or worktree. Includes Codex or Claude custom command/skill sequences — implement each issue, then review it, then prepare its PR — with each terminal advancing independently and a failed issue stopping only its own chain. Also use to split one goal into disjoint file-scoped tasks or open a coding-agent terminal in each existing issue worktree. PLURALITY plus INDEPENDENCE is the signal. Also a ROLLING QUEUE under a cap — "no more than 6 at a time", "start the next one whenever one finishes", "keep going until all 30 are done" — including the recurring check that refills it — a list of work plus a concurrency cap is this runbook, not a timer or PR runbook. Exclude one issue (daintree.workflow.start-work-on-issue), one agent (daintree.edits.spawn-visible-agent), agents collaborating on the same deliverable (daintree.orchestration.multi-agent), rival solutions (daintree.orchestration.competition), supervising/resuming a fleet already running (daintree.orchestration.supervise-fleet, daintree.orchestration.status-and-resume), opinions rather than work (daintree.agents.poll-pinned-agents), rescuing one stuck agent (daintree.agents.recover-agent), or separating agents that are ALREADY RUNNING into a worktree each, where the terminals exist and are to be relocated rather than replaced (daintree.worktree.relocate-agents).
risk: project
requiredTools:
  - tool.search
  - tool.schema
  - daintree.invoke
  - terminal.run.async
  - workflow.plan
  - workflow.getGraph
  - workflow.next
  - workflow.attachResource
  - workflow.recordEvidence
  - workflow.reconcile
  - context.snapshot
  - git.getProjectPulse
  - forge.listIssues
  - forge.getIssue
  - worktree.list
  - worktree.createWithRecipe
  - workflow.startWorkOnIssue
  - agentTask.spawnForEdits
  - copyTree.injectToTerminal
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.summarize
  - terminal.extract
  - terminal.extract.json
  - queue.resolve
  - agentTask.superviseTerminal
  - watcher.list
  - async.list
  - workflow.update
  - workflow.create
  - workflow.list
  - timer.schedule
  - timer.list
  - timer.cancel
  - grant.create
---
Use when: several INDEPENDENT units of work each need their own agent, running at once — a batch of forge issues (ENTRY A), or one goal you split into disjoint sub-tasks (ENTRY B). Both entries share one spine. If the agents would work the SAME problem, or their output has to be merged into one answer, this is the wrong runbook.

Hard non-goal — decomposition is SPLITTING, not DESIGNING. Draw boundaries and write taskPrompts: what each unit must achieve, which files it owns, what "done" means — never HOW. Do not sketch an implementation, invent an API, or read code to design a change and hand the design down; state the outcome plus the acceptance criteria, and read only enough to draw non-overlapping boundaries.

Procedure:

1. Establish the set and its authority. Orient first — worktree entries carry `issueNumber`/`prNumber`, so you can see what is already in flight and not duplicate it.
   - ENTRY A: `forge.listIssues({"arguments":{"state":"open"}})` -> `{ items, nextCursor, hasMore }`. It is a PASSTHROUGH wrapper — `arguments` is the ONLY key it takes at the top level (no `requestKey` on the forge readers), so nest every query field inside it; a top-level `state` is rejected outright. Same envelope for `forge.getIssue({"arguments":{"issueNumber":<int>}})`.
   - ENTRY B: split the goal into sub-tasks, each with a label, an outcome, and the files or area it owns. git.getProjectPulse and a shallow read are enough to draw lines; stop there.
   - Read the proposed units back as a list before anything mutates; never fan out on an inferred set. A set the user named exactly is confirmed by saying it back, so state it and proceed; a set you derived, or one that adds unrequested scope, needs their yes first.

2. DISJOINTNESS GATE — the step that makes fan-out safe. For every pair of units ask: do they touch the same files, the same module, or the same shared artifact (lockfile, generated schema, single config, migration sequence)? If yes, they are not parallel work. Pick one and say which: MERGE them into a single unit for a single agent; SERIALIZE — launch the first now, hold the second until the first lands; or RE-SPLIT until the overlap is gone. Never spawn two agents onto the same files and plan to "sort it out at merge time" — separate worktrees hide the collision until it is a conflict nobody watched happen. A shared-by-nature file belongs to exactly ONE unit: say so in that agent's prompt and tell the others to leave it alone. Report the gate's outcome before spawning.

3. Cap the fan-out and say the number. Every extra agent is another watcher, another wake, another relay, all on one machine. State how many you are about to launch and keep the batch manageable — above about four, get the go-ahead before launching, unless the user already authorized that cost and concurrency. If the user requested a saved recipe, its agent-sourced agent terminals are capped at 3 — overflow comes back as failed entries. Plain worktree creation does not spawn recipe terminals; agentTask.spawnForEdits has no such recipe cap.

4. Launch ENTRY A — one `workflow.startWorkOnIssue` per issue for ordinary issue work. If the user specified commands or skills, use the explicit worktree-and-spawn path in step 5 instead: the default issue workflow can submit its own task before the requested command. It does worktree + branch + spawn in a single call, and the wrapper auto-attaches ONE supervisor watcher. Every Daintree field nests inside `arguments`; only `attachWatcher` (assistant-side, defaults true) and `requestKey` are legal siblings:

       workflow.startWorkOnIssue({
         "arguments": { "issueNumber": <int>, "agentId": "<resolved id>", "injectContext": true },
         "requestKey": "<unique key for this repository and issue launch>"
       })

   Resolve `agentId` against the available-agent registry in your startup context BEFORE the first call — current Daintree rejects unknown/non-terminal agent ids before setup, while older hosts can fail later; read actual partial-failure results before recovering any unit. Emit independent calls in one tool round; the runtime decides which are parallel-safe (do not promise startWorkOnIssue passthrough calls run concurrently). Use a distinct stable requestKey per repository and logical unit; reconcile ambiguous results before retrying. Per-issue detail (partial-failure fields, watcher warnings, assignment) lives in daintree.workflow.start-work-on-issue.

5. Launch ENTRY B — one worktree and one spawn per sub-task. Resolve worktrees first: `worktree.list` for what exists (reuse only one already assigned to this unit), `worktree.createWithRecipe({"arguments":{"source":{"kind":"newBranch","branchName":"…","baseBranch":"…"}}})` (the branch fields live inside the `source` union, not at the top of `arguments`) for the rest, omitting recipeId so setup does not launch extra agents. Reuse only a worktree assigned to the unit; idle is not permission to repurpose it. ONE worktree per unit — never two agents in the same worktree. Then one `agentTask.spawnForEdits` per unit with: `worktreeId` (the resolved id), a DISTINCT `title` (it becomes the visible tab label the instant it launches — "auth: retry backoff", not "task 3"; name it at spawn, never rename after), `taskPrompt` (the outcome, the files this unit owns, and an explicit "change nothing outside this scope; report skipped adjacent work rather than asking"), `acceptanceCriteria` (a checkable definition of done — the watcher verifies against it instead of guessing from git cleanliness), and the FLAT top-level `watch: true` plus `watchGoal: "…"` (plain scalars, never a nested or dotted watcher key).

   NAMED CAPABILITY GATE — resolve each requested command/skill for the selected agent and actual destination worktree before its first submission. Discover `agentCapabilities.search` and `agentCapabilities.get` through `tool.search` / `tool.schema`, then use `daintree.invoke` with the action name and its arguments. Search with `{agentId,worktreeId,query}` and read the selected ID with `catalogRevision`. If `tool.search` returns only `agentCapabilities.search`, use it without `.get` and treat usage as unavailable. If it returns neither, read `slashCommands.list` with `tool.schema` and invoke it with `{agentId,projectPath}`, passing the destination worktree's path as `projectPath` so discovery resolves from that checkout, and match on `label`. User-level commands and skills live in home config such as `~/.claude/skills`, which no `fs.*` search can reach, so never resolve one from files. Resolve parameters from the user brief and issue metadata; use the returned token unchanged (`insertText`, else the `label`), including Codex `$skill` or `$plugin:skill`. A revision mismatch needs a fresh search with `refresh:true`. New worktrees need their own lookup after creation; the main checkout's definition may differ. Several matches, or arguments the unit needs that neither the user supplied nor the returned usage states, block that unit only, whatever else the lookup showed. No match also blocks that unit: partial coverage is not absence, so report it as not found rather than nonexistent. Only when no lookup is offered at all, and the user explicitly asked for their token to be sent as written, send it verbatim and report it unverified; a capability named only in prose is never sent unresolved. The launch adapter owns CLI flags and shell escaping. When startupSupport is unverified, inspect the initial task's consumption before later commands.

   For a user-supplied command such as `/design-review {query}`, put the command followed by that unit's ENTIRE supplied brief in the initial `taskPrompt`, including its heading, rationale, scope and file references. Preserve the supplied text rather than summarizing it. The spawn wrapper passes this first and appends its constraints. Do not start a similarly named recipe and then send the custom command: the recipe may already have submitted its own startup prompt, leaving the real brief queued behind the wrong task. A successful send proves queue acceptance. Preserve its submission receipt when available; even `pty_written` proves transport only, not that the agent consumed the brief. Use the foundation's receipt check and a bounded terminal read if startup is disputed.

   ORDERED COMMANDS — for several commands in a unit, persist one graph per issue with `workflow.plan` before submission. Include resolved command tokens, full arguments, capability IDs and revisions, and expected outcomes in notes; inspect `workflow.getGraph({id,view:"full"})` to confirm its saved `toolArgs` preserve the sequence. Each command is a delegate node with explicit `expectedEvidence` and a dependency on its predecessor's verified outcome. Keep graphs within 20 nodes; split larger work into linked phases without dropping steps. Feature-detect graph tools first and state when durable sequencing is unavailable.

   Only the first command goes in the initial taskPrompt. Attach the returned terminal/worktree/watcher to that node with `workflow.attachResource`. Later commands use `terminal.run.async({terminalId,command,title})`, with the returned async handle attached to the eligible node. End the turn while it runs. Never join multiple slash commands with newlines into one submission, submit successive steps in one batch, or stack an async observer on an active spawn watcher. `workflow.next` advises which node is eligible; its execution still uses the normal tool and existing permission tier or scoped automation grant.

6. Turn-by-turn id discipline. Every storeId, worktreeId, terminalId and watcherId is MINTED by the call that returns it and only reaches you after the tool batch returns; a batch's arguments are frozen before any call in it runs, so nothing in a batch can consume an id another call in that same batch produced. So: the first tool round emits independent producers; subsequent rounds read their ids, create dependent resources and save the map. End the user turn only after supervision is established.

7. Keep ONE map in scratch — unless it is a rolling queue (step 10), which lives in workflow rows. `scratch.create({"name":"fan-out"})` up front, then in the next tool round `scratch.set({"storeId":"scr_…","key":"fleet","value":[…]})` — one row per unit: label, issue number or scope, worktreeId, terminalId, watcherId, status, and the inbox id once one arrives. Pass a real JSON array, not a string, and OVERWRITE that single key as things land; it is your compaction-safe state, so after a wake you `scratch.get` it instead of re-deriving the fleet. If an agent needs extra workspace context, `copyTree.injectToTerminal({"terminalId":"<id>","worktreeId":"<id>","name":"<unit label>"})` after the spawn — each injection is recorded in the user's copy-tree history, so give it the unit's own scratch-row label as the top-level `name` (prefer 2-4 words; a sibling of `terminalId`/`worktreeId`, never inside `options`; blank is treated as absent and falls back to a selection-derived label). A fresh startWorkOnIssue spawn can take `injectContext: true` instead.

8. Supervise in the BACKGROUND, then END the turn. Every unit should carry a watcher: verify actual coverage from watcher.list when startWorkOnIssue omits a watcherId, or from the spawn result. Adopt the existing terminal if attachment failed; never relaunch a unit to repair tracking. Once coverage is established, the fleet’s supervision mode is already chosen. Do NOT also `terminal.awaitAll` the cohort: an edit fan-out runs for many minutes, so that wait burns the turn AND stacks a second supervisor on every terminal at once. Say what is running and that the next checkpoint is each watcher's completed_* event, then stop.

9. React per unit, not per fleet. Each completed_* wake is about ONE terminal: `terminal.summarize({"terminalId":"<that one>"})`, relay it, resolve that one inbox item, update the scratch row. Treat `completed_unverified` as unverified — say so plainly, never upgrade it. Beware the merge trap when reading several at once: `terminal.extract` over MULTIPLE `terminalIds` blends every tail into ONE answer, so for a per-terminal answer issue one extract per id (batched in a single turn), or use `terminal.extract.json` with an array schema keyed by terminalId. One unit failing or hanging never stalls the others — report it, keep the rest moving, hand a hung one off (daintree.agents.recover-agent), and drop the scratch store once the last unit lands.

   For a command chain, a completed_* or async wake is a checkpoint for the current step. Read that terminal's output and verify its expected outcome. Record acceptance with `workflow.recordEvidence`, then `workflow.reconcile({workflowId,reason:"wake"})` and consult `workflow.next`. An idle observation alone, picker/question, failed command, startup crash, or ambiguous submission cannot unlock a later command. On resumption read the durable graph and linked terminal/async resources, reconcile, then send only the eligible step; never replay completed or possibly-delivered work. Clear handled inbox items and continue within the existing authority. One failed issue does not stop its siblings.

10. ROLLING QUEUE — more units than slots: "at most 6 at a time", "start the next when one finishes", "until all 30 are done". The named cap IS the authorized concurrency: never exceed it, never ask again for it.
   - Persist first — this REPLACES step 7's scratch map: one `workflow.create` row per unit (status `pending`, its launch arguments verbatim in `notes`) plus a coordinator row with the goal, cap and unit row ids. Scratch dies on restart and the footer shows 10 rows; read the queue with `workflow.list`.
   - Fill to the cap via step 5 (create, then spawn). Spawn into a NEW worktree only once `worktree.list` shows its `setupState` is not `running`; else leave it pending for the next wake. Resend the unit's exact launch arguments (agent, title, brief, worktree): spawn dedupe keys on them, so any rewording on a later wake launches a duplicate. Mark the row `active` with its ids.
   - Pace with ONE repeating `timer.schedule` `message` at the requested cadence, bounded to cover the queue, `target.workflowRunId` = the coordinator row: "Reconcile the rolling queue in <wfr_…>: close finished units with evidence, refill to the cap, cancel this timer when none remain." No second timer, watcher or poll loop on top.
   - Refill on EVERY wake — a unit's completed_* event or the tick. Re-read `workflow.list` and live state first; delivery is at least once, so check before launching. A unit is done on its evidence (PR per the forge plus its final report), not on `waiting` alone.
   - Unattended refills create worktrees and spawn: mint the wake grant now (watch-outcome step 3), two uses per remaining unit, or say refills wait for the user.
   - When nothing is pending or active, `timer.cancel` the tick and report N of M with links. A wake cannot schedule a timer: if the bound runs out first, report what is left instead of re-arming.

Confirmation: creating worktrees and spawning agents mutates real state, and a fan-out multiplies it — confirm the SET and the COUNT per the active tier before the first launch. Resolve both from the user’s request and let the tool tier prompt; do not ask again when the exact fan-out is already authorized.

Keep launchId/workflowRunId and partial-failure details in each row as available. A unit is complete only against its acceptance evidence, not a watcher name or clean checkout. If integration across branches is part of the request, serialize that separate integration step and verify the combined result; isolated passing branches do not prove a combined tree passes. On restart scratch is gone: recover from durable workflow rows/graphs and live resources. Respect already-authorized scope/count without adding another approval loop.

Report back: the units you fanned out and the gate's verdict (what you merged, serialized, or re-split), then one LIST line per unit — label, worktreeId, terminalId, watcherId, status — never a table. Close with what is still running, what failed, and the next checkpoint: each watcher's completed_* event, which keeps going even if the user closes the assistant.
