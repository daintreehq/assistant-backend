---
id: daintree.recipe.run-or-create
title: Run a Daintree workspace recipe, or create a worktree with one
summary: List, inspect and run an EXISTING Daintree workspace recipe — or create a worktree with one as its startup recipe — then supervise any LIVE agent it spawns. Nothing here authors or edits a recipe.
whenToUse: Use when the user asks to list, inspect, load, run or apply an existing Daintree workspace recipe, or to create a worktree with a startup recipe. Also use when they ask to CREATE or edit a recipe — only so you can say plainly that no tool can author one and offer what you can actually do. Do not use to create a worktree for a forge issue, which has its own typed workflow (use daintree.workflow.start-work-on-issue), to launch a single agent with no recipe involved (use daintree.edits.spawn-visible-agent), or to delete a worktree and tear the workspace down (use daintree.orchestration.workspace-cleanup).
risk: project
tags:
  - recipe
  - worktree
  - startup
  - workspace
  - layout
  - pr-review
  - setup
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - worktree.list
  - recipe.list
  - recipe.run
  - worktree.createWithRecipe
  - daintree.call
  - terminal.awaitAll
  - terminal.extract
  - terminal.summarize
  - terminal.read
  - terminal.sendCommand
  - terminal.await.async
  - queue.publish
  - watcher.terminal.create
  - agentTask.superviseTerminal
  - agentTask.spawnForEdits
  - workflow.startWorkOnIssue
---
Use when: the user asks about Daintree workspace recipes or creating a worktree with one.

You cannot author a recipe. The whole recipe surface is `recipe.list`, `recipe.run` and `worktree.createWithRecipe` — read and run, nothing more; no tool creates, edits, renames or deletes one, and `daintree.call` will not conjure the capability. If the user asks for a NEW recipe, say that plainly instead of improvising: recipes are defined in Daintree itself and only they can add one there. Then offer what you CAN do — run an existing recipe, or stand the layout up by hand this once (`worktree.createWithRecipe` for the worktree, `agentTask.spawnForEdits` for each agent it needs; a bare shell terminal is theirs to open, since no tool creates one) — and leave saving it as a recipe to them.

Note: "Daintree workspace recipes" (MCP actions) are distinct from the assistant skills loaded into your context. A recipe is a SAVED SET of terminal definitions — and a recipe terminal can be a plain shell, a dev-preview, OR a LIVE agent that starts running its CLI the moment it spawns, with NO watcher attached. So a recipe SETS UP a workspace; it never supervises. If a recipe launches an agent whose work you must track, you supervise that terminal SEPARATELY (see step 6). Do not assume "recipe = just shells."

Procedure:

1. Orient if the target is ambiguous. recipe.run lands in the focused/active worktree by default, so before mutating anything pin WHERE the recipe should go: context.snapshot (its terminals digest shows what is already running), then worktree.getCurrent / worktree.list for the worktree set. Skip this only when the user already named the worktree (or you're creating a fresh one in step 3).

2. Discover recipes when needed. recipe.list({ arguments: { worktreeId? } }) ->
   { recipes: [{ id, name, worktreeId, terminalCount, showInEmptyState }], isLoading }.
   Argument shape (these are strict — a stray top-level key is rejected outright): `recipe.list` takes ONLY `arguments`; `worktree.createWithRecipe` takes a required `arguments` plus an optional top-level `requestKey`; `recipe.run` takes a required top-level `recipeId` (merged last, so it always wins) plus optional top-level `arguments` and `requestKey`. Every recipe-defined field nests inside `arguments`.
   Pick the recipe by name/id with the user. LOOK AHEAD: terminalCount tells you HOW MANY terminals will spawn, but recipe.list gives NO per-terminal kind — you cannot tell from listing whether any terminal is a live agent. You only learn that AFTER the run, by inspecting the terminals (step 5). Don't promise "this is just a dev server" off a recipe.list entry.

3. Run or create. Both are mutating and will confirm; pass a requestKey when you can (Daintree dedupes on it and strips it before validation).
   - Apply a recipe to an EXISTING worktree -> recipe.run({ recipeId, arguments: { worktreeId? } }) ->
     { spawnedCount, failedCount, failedTerminals: [{ index, reason }] }.
   - Create a NEW worktree AND run a startup recipe -> worktree.createWithRecipe({ arguments: { branchName?, baseBranch?, recipeId?, fromRemote?, useExistingBranch?, issueNumber? | pullRequestNumber?, assignToSelf? } }) ->
     { worktreeId, worktreePath, branch, recipeLaunched, spawnedTerminalCount, failedTerminalCount, assignedToSelf, assignedUsername, assignmentError }.
     (issueNumber and pullRequestNumber are mutually exclusive.) recipeLaunched is true iff ≥1 terminal spawned.

4. Read the result. The recipe spawns ALL terminals synchronously and reports partial failure INLINE — check failedCount / failedTerminalCount. If nonzero, surface which indices failed and why (failedTerminals[].reason); a partial spawn is normal to report, not a hard error. NOTE the cap: agent-sourced recipe runs are limited to 3 agent terminals — overflow shows up as failed entries, not a thrown error.

5. Detect whether the recipe spawned a LIVE agent. Inspect the worktree's terminals via context.snapshot (its terminals digest lists each terminal with its agentId / agentState / kind); for a precise per-worktree read use daintree.call terminal.list({ worktreeId }) ->
   { terminals: [{ id, kind, type, worktreeId, title, agentId, agentState, isInputLocked, isFocused }] }.
   An entry with an agentId / agentState is a LIVE agent running its CLI right now, unsupervised. Entries that are plain shells or dev-previews need nothing from you. A freshly spawned agent prints NOTHING for several seconds — empty output right after the run means "starting up," never "it died."

6. Supervise any live agent you must TRACK. Pick ONE mode per terminal (foundation) — the recipe attached nothing, so whatever you choose is that terminal's only supervisor:
   - IN-TURN (you drive it to completion right now): attach NO watcher. Wait for the whole cohort with ONE terminal.awaitAll({ terminalIds: [...] }) ->
     { allFinished, perTerminal: [{ terminalId, status }], stillWorking: [...], askingQuestion: [...] } where status is one of "finished" | "failed" | "question" | "working", and the top-level stillWorking/askingQuestion arrays name the straggler ids so you can target them without scanning perTerminal. It returns NO content, so settle the finish the foundation's way — awaitAll plus a bounded tail read of the same ids (terminal.extract for a field, terminal.summarize for the gist of a noisy TUI, terminal.read for short verbatim) — and re-await or watch one that reported finished yet still looks busy. For a SINGLE agent, use terminal.extract wait:{} instead of awaitAll. Answer any askingQuestion ids with terminal.sendCommand and await again; if "failed", note it and don't respawn in a loop. Raise maxAttempts (default 30 ≈ 60s, max 240 ≈ 480s) for a known-slow cohort rather than adding rounds. When the foundation's re-await bound runs out the agent is HUNG: if others finished, report them and note the hung one; if it is the only work left, hand it to ONE `terminal.await.async({ terminalIds: ["<id>"] })` and end the turn. Publish a blocked inbox item alongside it when the user should see the stall in their queue — `queue.publish` with `source:"model_worker"`, `severity:"blocked"`, `title`, `summary`, `target:{terminalId}`, `dedupeKey:"hung-<terminalId>"` — but do NOT also attach a watcher; the async wait is already that terminal's one supervisor.
   - BACKGROUND ("set it up and wake me later"): adopt the running terminal with agentTask.superviseTerminal({ terminalId: "<id>", goal: "...", spawnMode: "edit"|"explore", acceptanceCriteria? }) — the purpose-built way to attach a supervisor watcher to an ALREADY-running terminal (it dedupes, so it never double-attaches). (Lower-level alternative: watcher.terminal.create({ terminalIds: ["<id>"], title: "...", goal: "..." }) — the field is goal, NOT watchGoal; watchGoal belongs to agentTask.spawnForEdits.) Then END your turn. The watcher confirms completion with the same finished check and publishes a completed_* event to the attention queue — react on that wake, don't poll.

7. If the recipe spawned no live agent — or its agents are dev tooling the user watches themselves — there is nothing for you to supervise. Just report what was set up.

8. Scope check. A recipe is workspace setup, not issue work. If the user's real goal is "have an agent IMPLEMENT or INVESTIGATE something in this worktree," that is a separate task: agentTask.spawnForEdits (mode:"edit" or "explore", with its own supervision) or workflow.startWorkOnIssue (which spawns the agent AND the assistant auto-attaches a background watcher — report the terminalId + watcherId and end your turn, reacting on the completed_* event). Don't bend a recipe into an agent-work spawn.

Report back: which recipe ran, the worktree/branch ids created, spawnedCount and any failed terminals, which terminals are live agents, and — if you attached supervision — the mode you chose and the next checkpoint (an in-turn await you're driving now, or a background watcher's completed_* event you'll react to). Watchers are durable — the background supervisor keeps them running if the user closes the assistant.
