---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn or use explicitly named open agents, relay their work between them with terminal.sendCommand, and synthesize ONE answer — collaborative multi-agent work driven until the cohort converges.
whenToUse: 'Use when several agents must work ONE problem together and you relay between them until they CONVERGE on a single synthesized answer — debate until they converge, collaborate, cross-check, iterate toward one merged result. The signal is a synthesis target: the deliverable is the ONE answer you assemble, not a dialogue. Do not use when each agent gets its OWN independent unit of work with no shared deliverable (use daintree.orchestration.fan-out-work); for an open-ended visible dialogue — let them talk, see where it goes (use the conversation skill); a debate that ends by voting a position off (use the debate-elimination skill); independent submissions scored or peer-voted to pick a winner (use the competition skill); a one-shot independent poll (use the poll-pinned-agents skill); or forwarding one agent''s review to another (use daintree.agents.relay-review-feedback).'
risk: project
tags:
  - daintree
  - orchestration
  - multi-agent
  - agents
  - collaborate
  - converge
  - cross-check
  - synthesis
  - relay
  - debate
  - fan-out
  - existing-terminals
  - sendcommand
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - agentTask.status
  - agentTask.list
  - terminal.sendCommand
  - terminal.summarize
  - terminal.extract
  - terminal.read
  - artifact.read
  - terminal.awaitAll
  - terminal.await.async
  - terminal.focus
  - terminal.close
  - watcher.terminal.create
  - queue.publish
  - queue.digest
  - queue.resolve
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
---
Use when: the user wants several agents to collaborate, cross-check, or debate a single problem until they converge, and you are the conductor relaying between them toward ONE synthesized answer.

The pattern is a loop: spawn or identify a cohort → collect each one's output → relay the others' output back to each → ask for the next step → repeat until they converge → synthesize one answer. The deliverable is that answer, not a transcript. terminal.sendCommand is how you relay; it is always callable (every turn, including a watcher wake) — never hunt for it with tool.search.

You are the ONLY conduit — the foundation's relay rule is this runbook's whole spine. Its operational edge: never write a taskPrompt or relay that tells an agent to contact, send its answer to, reply to, or coordinate directly with another agent or terminal. It has no way to, and the instruction silently does nothing. Tell each agent only to produce its output in its OWN terminal; YOU route everything.

Convergence relay is a FIRST-CLASS case, including after an interview, competition, poll, or other run where the user names agents that are already open. If the user asks the cohort to compare, cross-check, reconcile, or debate until they agree, do NOT answer by summarizing prior outputs or by accepting one agent's claimed direct contact as completion. Run the relay yourself: collect each side's current position, send each the others' condensed positions plus the next round's question, await/read, and continue until the positions stop moving — then synthesize. Two to four rounds is usually enough; if they will clearly not agree, report the residual disagreement as the result rather than looping.

CRITICAL — turn-by-turn id discipline (this is the part the foundation's verbatim-id rule does NOT cover). Every `scr_…` storeId and every `terminal-…` id is MINTED by a call (scratch.create, agentTask.spawnForEdits) and only EXISTS once that call RETURNS — which lands in your results on the NEXT turn. A batch's arguments are all frozen before any call in it runs, so a call CANNOT use an id an earlier call in the SAME batch produced; guessing one yields a fake that misses every time. Run the loop strictly turn by turn: emit the producers (create store, spawn cohort), END the turn; next turn read the REAL ids from the results, THEN scratch.set / awaitAll / extract / sendCommand against them, storing each one full-length in scratch. This bans ONLY consuming ids minted earlier in the same batch — reusing ids you already hold from a prior turn SHOULD still be batched.

Procedure:
1. Scope it. Decide the cohort (which agents; resolve any user-spoken name against the roster or existing terminal titles) and the rounds. Don't over-plan — the loop is the same regardless of round count.
   - Create ONE scratch store up front as compaction-safe state: `scratch.create({"name":"multi-agent-orchestration"})`; hold the returned `scr_…` storeId. Keep the evolving state — terminalId→role map, round counter, slow counts, dropped ids, and any one-line gists needed next round — in ONE key (`orchestration_state`) that you OVERWRITE each round. Pass `value` as a real JSON object, not a string.
   - If this is a follow-on round between named agents that are already open, do NOT spawn replacements. Confirm their full terminalIds from your recorded map or context.snapshot, write them into scratch as the cohort, and proceed to the relay loop. The user named them, so they are the cohort; your job is to relay, not to summarize what they already said.

2. Spawn the cohort only when you do not already have explicit live terminals. Use ONE BATCH — one agentTask.spawnForEdits per agent (groups of ~4 per turn), each with a distinct `title` and a SHORT, self-contained taskPrompt. Build the cohort from your OWN fresh spawns: per the foundation an already-open terminal is not yours to commandeer, so never fold one in — or send it a round prompt — unless the user explicitly named it. The title becomes the tab name the instant the agent launches, so name it right at spawn — do NOT follow the spawn batch with terminal.rename calls. Pick `mode:"explore"` for thinking/answering/analysis that touches NO files, `mode:"edit"` only when they must change files in a worktree. Keep each prompt MINIMAL. Do NOT attach a watcher (`watch`/`watchGoal`) when you are driving in-turn; terminal.awaitAll is INTERRUPTIBLE, so you never need a watcher as an unblock net.
   - On the turn the spawns RETURN their ids (the turn AFTER the spawn batch), and before you wait or relay, write the initial state to scratch with full terminalIds.

3. Re-anchor, then wait for the relevant cohort. At the top of every round, FIRST `scratch.get({"storeId":"scr_…","key":"orchestration_state"})` and restore terminalRoles / activeTerminalIds / slowCounts / round. When you already hold the ids, the get and awaitAll can share one batch. Use ONE `terminal.awaitAll({terminalIds:[...]})`; it polls every agent concurrently and resolves when each has gone working→idle (or completed/exited). Start a multi-round cohort with `maxAttempts:60`; raise only for genuinely slow agents. Do NOT issue one terminal.extract wait per agent.
   - READ + SELF-HEAL after awaitAll reports finished. Read every agent's DISTINCT answer in free-form: one bounded `terminal.read` per terminal, all batched in one turn. Do NOT ask agents to emit JSON, and do NOT run a structured `terminal.extract.json` over the cohort. terminal.read returns full scrollback (archived as an artifact when large — page through with artifact.read). `terminal.summarize` is only a fallback for garbled multi-screen TUI output; if its verdict contradicts visible terminal text, trust the text and re-read.
   - Plain-text terminal.extract is fine for pulling one specific detail from a SINGLE terminal, and no-wait extracts can be batched in parallel, but extract sees ONLY the visible tail. A sparse or partial extract means fall back to terminal.read, not a re-extract. Once you hold a real answer per agent, go straight to relay/synthesis and do NOT re-read the same agents afterward in the same round.
   - If a terminal reported finished but its tail shows it is still working, do NOT relay it — re-await just that one, watch it, or read closer. Never relay a half-done or dead screen.
   - When the foundation's bad-id check fires (a whole cohort still-working, or 'Terminal not found'), fix the mismatch in your scratch map too, not just in the next call.
   - Handle non-finished cases. An agent ASKING a question → answer those ids with sendCommand and await again. FAILED → drop it and note it; do NOT respawn in a loop. STILL WORKING after budget → re-await exactly those ids under the foundation's bounded-wait rule, then proceed without it or hand it to `terminal.await.async`.
   - If the SAME terminal is the lone straggler in two consecutive rounds, stop awaiting it inside the cohort: read the fast agents immediately, then await ONLY the straggler at a higher bounded budget.

4. Relay with `terminal.sendCommand({terminalId, command})`. Send each agent ONLY what it needs from the OTHERS — their facts/drafts/votes/messages — plus this round's one question. Condense first: the outputs from step 3 are raw material. Relay the GIST, labeled by agent, never raw scrollback unless the task specifically requires exact wording. Convergent work is SIMULTANEOUS by default: every agent answers the SAME round-question and none of them needs to hear a specific other one first, so send one sendCommand per agent, all in ONE BATCH, with the round's terminal.awaitAll as the LAST call in that same batch; dispatch is sequential, so every send completes and then polling begins in one model turn. That is the normal mode here — a draft round, a cross-check round, a reconcile round, a vote. Take the floor away from the batch and give it to ONE agent at a time only when this round genuinely depends on a specific agent reacting to a specific other agent's words (say, one agent must answer a challenge before the rest can move). Each sendCommand forces that agent to take a turn, so a stray fan-out to bystanders makes them all speak at once.
   - Relay actual content, not the fact that content exists. Send each agent the others' real claims, drafts, and objections — a summary saying "the others responded" gives them nothing to converge on. If a participant drafts something and asks whether to proceed, forward it unless it needs a real user decision or a safety confirmation. Keep going for 2–4 rounds or until the positions stop moving; do not stop after one round just because an agent claims it already reached the others by some private bridge.
   - If the user sends feedback MID-ORCHESTRATION (via interruptedByUser:true or a message between rounds), fold it into the NEXT relay — do NOT restart the orchestration.
   - Before closing each round, OVERWRITE the `orchestration_state` checkpoint with the new round / slowCounts / droppedTerminalIds and any gist you'll need next round. Keep the value compact — ids, counts, one-line gists, never raw scrollback or whole answers.

5. Synthesize and report ONE clean answer — the merged result, the agreed position, or the named residual disagreement — not a transcript of every terminal unless the user asked for the transcript. Quote real terminalIds where useful.

6. Close out. Driving in-turn you attached no watchers, so there is usually nothing to cancel. Leave the cohort's terminals open and idle; if the user DOES ask you to close them, close ONLY the exact cohort ids you recorded this run — and for a competitive workflow they may ask to close only LOSERS and keep the winner(s) and any judge open, so close only those. Drop your scratch store in the same cleanup with `scratch.drop({"storeId":"scr_…"})` so the per-session store budget is not exhausted; keep it while the cohort is alive.

Anti-patterns (the non-obvious traps):
- One-shot batching the whole orchestration — putting scratch.set/get, awaitAll, extract, or sendCommand in the SAME batch as the scratch.create/spawns that mint the ids they need.
- Skipping the relay — the user asked the cohort to cross-check, reconcile, or debate until they agree, and you merely summarize the outputs they already produced. Run the sendCommand/read rounds first, THEN synthesize.
- Treating a claimed direct agent-to-agent call as a completed round. The user asked YOU to relay; summarize only after you have actually run the loop, or if the user asked only for a summary.
- Running an open-ended visible dialogue here — if there is no synthesis target and the user wants to watch them talk, that is the conversation skill; if the round ends by voting a position off, that is the debate-elimination skill.
- Ignoring an awaitAll that returned `interruptedByUser:true` — the user typed mid-wait and their message is ALREADY in the conversation.
- Trusting a sparse or partial extract as the complete answer — extract sees only the visible tail, so fall back to terminal.read.
- Asking agents for JSON or schema-shaped output, or reading the cohort through terminal.extract.json.
- Hunting agent output through artifact.read instead of reading terminals; use artifact.read only to page terminal.read content archived as an artifact.
- Padding agent prompts — each taskPrompt and each relay carries only what that agent needs this round.
- Telling an agent to communicate DIRECTLY with another agent or terminal — 'send your answer to the other model', 'coordinate with the other agent', 'reply in the other terminal'. Relay every hand-off yourself with terminal.sendCommand.

Report back: the synthesized result — the converged answer or the residual disagreement — which agents participated (real terminalIds), and any inbox/terminal cleanup you did.
