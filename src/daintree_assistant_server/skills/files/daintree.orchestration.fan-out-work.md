---
id: daintree.orchestration.fan-out-work
title: Fan out disjoint work across parallel agents
summary: Put one agent/terminal on each of several INDEPENDENT units of work — a batch of forge issues, existing issue worktrees, or one goal split into disjoint file-scoped sub-tasks — then supervise the whole fleet in the background.
whenToUse: Use when the user wants several agents each working their OWN separate piece of work at once — take these issues and put an agent on each, an agent per ticket, work the whole batch in parallel, parallelise this across worktrees, knock out the backlog, for each of those worktrees create/open/spawn a new coding-agent or Claude Code terminal in each, run `/work {issue_number}` using the issue number detected on each worktree. The signal is PLURALITY plus INDEPENDENCE — many units, no shared deliverable. Do not use for ONE issue (daintree.workflow.start-work-on-issue) or ONE agent (daintree.edits.spawn-visible-agent); for several agents on the SAME problem converging on one answer (daintree.orchestration.multi-agent) or producing rival solutions to be scored (daintree.orchestration.competition); for opinions rather than work (daintree.agents.poll-pinned-agents); for a fleet that ALREADY exists (daintree.orchestration.supervise-fleet, daintree.orchestration.status-and-resume); or for rescuing one stuck agent (daintree.agents.recover-agent).
risk: project
requiredTools:
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
---
Use when: several INDEPENDENT units of work each need their own agent, running at once — a batch of forge issues (ENTRY A), or one goal you split into disjoint sub-tasks (ENTRY B). Both entries share one spine. If the agents would work the SAME problem, or their output has to be merged into one answer, this is the wrong runbook.

Hard non-goal — decomposition is SPLITTING, not DESIGNING. Draw boundaries and write taskPrompts: what each unit must achieve, which files it owns, what "done" means — never HOW. Do not sketch an implementation, invent an API, or read code to design a change and hand the design down; state the outcome plus the acceptance criteria, and read only enough to draw non-overlapping boundaries.

Procedure:

1. Establish the set, then get an explicit yes. Orient first — worktree entries carry `issueNumber`/`prNumber`, so you can see what is already in flight and not duplicate it.
   - ENTRY A: `forge.listIssues({"arguments":{"state":"open"}})` -> `{ items, nextCursor, hasMore }`. It is a PASSTHROUGH wrapper — `arguments` is the ONLY key it takes at the top level (no `requestKey` on the forge readers), so nest every query field inside it; a top-level `state` is rejected outright. Same envelope for `forge.getIssue({"arguments":{"issueNumber":<int>}})`.
   - ENTRY B: split the goal into sub-tasks, each with a label, an outcome, and the files or area it owns. git.getProjectPulse and a shallow read are enough to draw lines; stop there.
   - Read the proposed units back as a list and confirm WHICH ones before anything mutates. Never fan out on an inferred set.

2. DISJOINTNESS GATE — the step that makes fan-out safe. For every pair of units ask: do they touch the same files, the same module, or the same shared artifact (lockfile, generated schema, single config, migration sequence)? If yes, they are not parallel work. Pick one and say which: MERGE them into a single unit for a single agent; SERIALIZE — launch the first now, hold the second until the first lands; or RE-SPLIT until the overlap is gone. Never spawn two agents onto the same files and plan to "sort it out at merge time" — separate worktrees hide the collision until it is a conflict nobody watched happen. A shared-by-nature file belongs to exactly ONE unit: say so in that agent's prompt and tell the others to leave it alone. Report the gate's outcome before spawning.

3. Cap the fan-out and say the number. Every extra agent is another watcher, another wake, another relay, all on one machine. State how many you are about to launch and get the go-ahead above about four. One hard limit: if you launch through a RECIPE (`worktree.createWithRecipe`, or a `recipeId` on `workflow.startWorkOnIssue`), agent-sourced recipe terminals are capped at 3 — overflow comes back as failed entries, not an error. agentTask.spawnForEdits has no such cap, so there the ceiling is your own supervision bandwidth.

4. Launch ENTRY A — one `workflow.startWorkOnIssue` per issue. It does worktree + branch + spawn in a single call, and the wrapper auto-attaches ONE supervisor watcher. Every Daintree field nests inside `arguments`; only `attachWatcher` (assistant-side, defaults true) and `requestKey` are legal siblings:

       workflow.startWorkOnIssue({
         "arguments": { "issueNumber": <int>, "agentId": "<resolved id>", "injectContext": true },
         "requestKey": "fanout-<issue>"
       })

   Resolve `agentId` against the available-agent registry in your startup context BEFORE the first call — an unknown or mis-transcribed id still creates a terminal and then dies silently, and in a fan-out you would make that mistake once per issue. Emit the calls in ONE turn (they dispatch concurrently), each with a DISTINCT `requestKey` so a retry cannot double-spawn. Per-issue detail (partial-failure fields, watcher warnings, assignment) lives in daintree.workflow.start-work-on-issue.

5. Launch ENTRY B — one worktree and one spawn per sub-task. Resolve worktrees first: `worktree.list` for what exists (reuse an idle one that matches a unit), `worktree.createWithRecipe({"arguments":{"branchName":"…","baseBranch":"…","recipeId":"…"}})` for the rest. ONE worktree per unit — never two agents in the same worktree. Then one `agentTask.spawnForEdits` per unit with: `worktreeId` (the resolved id), a DISTINCT `title` (it becomes the visible tab label the instant it launches — "auth: retry backoff", not "task 3"; name it at spawn, never rename after), `taskPrompt` (the outcome, the files this unit owns, and an explicit "change nothing outside this scope; report skipped adjacent work rather than asking"), `acceptanceCriteria` (a checkable definition of done — the watcher verifies against it instead of guessing from git cleanliness), and the FLAT top-level `watch: true` plus `watchGoal: "…"` (plain scalars, never a nested or dotted watcher key).

6. Turn-by-turn id discipline. Every storeId, worktreeId, terminalId and watcherId is MINTED by the call that returns it and only reaches you on the NEXT turn; a batch's arguments are frozen before any call in it runs, so nothing in a batch can consume an id another call in that same batch produced. So: turn one emits the producers (scratch.create plus the creates/spawns) and ENDS; turn two reads the real ids and writes them down.

7. Keep ONE map in scratch. `scratch.create({"name":"fan-out"})` up front, then next turn `scratch.set({"storeId":"scr_…","key":"fleet","value":[…]})` — one row per unit: label, issue number or scope, worktreeId, terminalId, watcherId, status, and the inbox id once one arrives. Pass a real JSON array, not a string, and OVERWRITE that single key as things land; it is your compaction-safe state, so after a wake you `scratch.get` it instead of re-deriving the fleet. If an agent needs extra workspace context, `copyTree.injectToTerminal({"terminalId":"<id>","worktreeId":"<id>","name":"<unit label>"})` after the spawn — each injection is recorded in the user's copy-tree history, so give it the unit's own scratch-row label as the top-level `name` (prefer 2-4 words; a sibling of `terminalId`/`worktreeId`, never inside `options`; blank is treated as absent and falls back to a selection-derived label). A fresh startWorkOnIssue spawn can take `injectContext: true` instead.

8. Supervise in the BACKGROUND, then END the turn. Every unit already carries its own watcher (auto-attached by startWorkOnIssue, or `watch: true` on the spawn) — the fleet's supervision mode is therefore already chosen. Do NOT also `terminal.awaitAll` the cohort: an edit fan-out runs for many minutes, so that wait burns the turn AND stacks a second supervisor on every terminal at once. Say what is running and that the next checkpoint is each watcher's completed_* event, then stop.

9. React per unit, not per fleet. Each completed_* wake is about ONE terminal: `terminal.summarize({"terminalId":"<that one>"})`, relay it, resolve that one inbox item, update the scratch row. Treat `completed_unverified` as unverified — say so plainly, never upgrade it. Beware the merge trap when reading several at once: `terminal.extract` over MULTIPLE `terminalIds` blends every tail into ONE answer, so for a per-terminal answer issue one extract per id (batched in a single turn), or use `terminal.extract.json` with an array schema keyed by terminalId. One unit failing or hanging never stalls the others — report it, keep the rest moving, hand a hung one off (daintree.agents.recover-agent), and drop the scratch store once the last unit lands.

Confirmation: creating worktrees and spawning agents mutates real state, and a fan-out multiplies it — confirm the SET and the COUNT per the active tier before the first launch.

Report back: the units you fanned out and the gate's verdict (what you merged, serialized, or re-split), then one LIST line per unit — label, worktreeId, terminalId, watcherId, status — never a table. Close with what is still running, what failed, and the next checkpoint: each watcher's completed_* event, which keeps going even if the user closes the assistant.
