---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn or use explicitly named open agents, relay their work or conversation with terminal.sendCommand, and synthesize one answer — collaborative/back-and-forth multi-agent tasks.
whenToUse: Use when the user wants several agents to work one problem TOGETHER through genuine back-and-forth — facilitate a conversation, let X and Y talk, see where it goes, collaborate, cross-check, debate, iterate toward convergence — and you must coordinate the relay between the terminals. Do not use when agents produce independent submissions that are then scored, voted on, or peer-reviewed to pick a winner (use the competition skill), for a judged interview (use the interview skill), a one-shot independent poll (use the poll-pinned-agents skill), or a post-tie runoff (use the runoff tie-breaker skill).
priority: 95
risk: project
tags:
  - daintree
  - orchestration
  - multi-agent
  - agents
  - collaborate
  - conversation
  - facilitate
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
Use when: the user wants several agents to collaborate, converse, or cross-check on a single problem, and you are the conductor relaying between them.

The pattern is a loop: spawn or identify a cohort → collect each one's output → relay the others' output back to each → ask for the next step → repeat → synthesize one answer. terminal.sendCommand is how you relay; it is always callable (every turn, including a watcher wake) — never hunt for it with tool.search.

You are the ONLY conduit. The spawned agents have NO Daintree MCP access — they cannot see, reach, or message each other or any other terminal. Every hand-off between agents flows THROUGH you: you read each one's output and relay the gist to the others with terminal.sendCommand. So never write a taskPrompt or relay that tells an agent to contact, send its answer to, reply to, or coordinate directly with another agent or terminal — it has no way to, and the instruction silently does nothing. Tell each agent only to produce its output in its OWN terminal; YOU route everything.

Conversation relay is a FIRST-CLASS case, including after an interview, competition, poll, or other run where the user names agents that are already open. If the user asks you to 'facilitate', 'have a back and forth', 'let them talk', or 'see where it goes', do NOT answer by summarizing prior outputs or by accepting one agent's claimed direct contact as completion. Run the relay yourself: prompt one side for a short message, read it, send the condensed or quoted message to the other side, await/read, and continue for a few exchanges until a natural stop or the user interrupts.

CRITICAL — turn-by-turn id discipline. Every `scr_…` storeId and every `terminal-…` id is MINTED by a call (scratch.create, agentTask.spawnForEdits) and only EXISTS once that call RETURNS — which lands in your results on the NEXT turn. A batch's arguments are all frozen before any call in it runs, so a call CANNOT use an id an earlier call in the SAME batch produced; guessing one yields a fake that misses every time. Run the loop strictly turn by turn: emit the producers (create store, spawn cohort), END the turn; next turn read the REAL ids from the results, THEN scratch.set / awaitAll / extract / sendCommand against them. This bans ONLY consuming ids minted earlier in the same batch — reusing ids you already hold from a prior turn SHOULD still be batched.

Copy every id EXACTLY and in FULL from the tool result — a terminalId looks like `terminal-5284bfef-3d11-424c-90cb-136f24046295`. Never abbreviate it to a prefix or rebuild one from memory: Daintree matches the whole string, so a truncated id matches NO terminal. Store each id full-length in scratch.

Procedure:
1. Scope it. Decide the cohort (which agents; resolve any user-spoken name against the roster or existing terminal titles) and the rounds. Don't over-plan — the loop is the same regardless of round count.
   - Create ONE scratch store up front as compaction-safe state: `scratch.create({"name":"multi-agent-orchestration"})`; hold the returned `scr_…` storeId. Keep the evolving state — terminalId→role map, round counter, slow counts, dropped ids, and any one-line gists needed next round — in ONE key (`orchestration_state`) that you OVERWRITE each round. Pass `value` as a real JSON object, not a string.
   - If this is a follow-on conversation between named agents that are already open, do NOT spawn replacements. Confirm their full terminalIds from your recorded map or context.snapshot, write them into scratch as the cohort, and proceed to the relay loop. The user named them, so they are the cohort; your job is to relay, not to summarize what they already said.

2. Spawn the cohort only when you do not already have explicit live terminals. Use ONE BATCH — one agentTask.spawnForEdits per agent (groups of ~4 per turn), each with a distinct `title` and a SHORT, self-contained taskPrompt. Build the cohort from your OWN fresh spawns: an already-open terminal in the workspace is NOT automatically a cohort member — it may be the user's own work or another agent mid-task, so do not fold it in or send it your round prompts unless the user explicitly named that terminal to include. The title becomes the tab name the instant the agent launches, so name it right at spawn — do NOT follow the spawn batch with terminal.rename calls. Pick `mode:"explore"` for thinking/answering/analysis that touches NO files, `mode:"edit"` only when they must change files in a worktree. Keep each prompt MINIMAL. Do NOT attach a watcher (`watch`/`watchGoal`) when you are driving in-turn; terminal.awaitAll is INTERRUPTIBLE, so you never need a watcher as an unblock net.
   - On the turn the spawns RETURN their ids (the turn AFTER the spawn batch), and before you wait or relay, write the initial state to scratch with full terminalIds.

3. Re-anchor, then wait for the relevant cohort. At the top of every round, FIRST `scratch.get({"storeId":"scr_…","key":"orchestration_state"})` and restore terminalRoles / activeTerminalIds / slowCounts / round. When you already hold the ids, the get and awaitAll can share one batch. Use ONE `terminal.awaitAll({terminalIds:[...]})`; it polls every agent concurrently and resolves when each has gone working→idle (or completed/exited). Start a multi-round cohort with `maxAttempts:60`; raise only for genuinely slow agents. Do NOT issue one terminal.extract wait per agent.
   - READ + SELF-HEAL after awaitAll reports finished. Read every agent's DISTINCT answer in free-form: one bounded `terminal.read` per terminal, all batched in one turn. Do NOT ask agents to emit JSON, and do NOT run a structured `terminal.extract.json` over the cohort. terminal.read returns full scrollback (archived as an artifact when large — page through with artifact.read). `terminal.summarize` is only a fallback for garbled multi-screen TUI output; if its verdict contradicts visible terminal text, trust the text and re-read.
   - Plain-text terminal.extract is fine for pulling one specific detail from a SINGLE terminal, and no-wait extracts can be batched in parallel, but extract sees ONLY the visible tail. A sparse or partial extract means fall back to terminal.read, not a re-extract. Once you hold a real answer per agent, go straight to relay/synthesis and do NOT re-read the same agents afterward in the same round.
   - If a terminal reported finished but its tail shows it is still working, do NOT relay it — re-await just that one, watch it, or read closer. Never relay a half-done or dead screen.
   - Suspect a BAD ID before a slow agent. If awaitAll reports the ENTIRE cohort still-working for the whole budget, or a read returns 'Terminal not found', use context.snapshot and fix any mismatch in your scratch map.
   - Handle non-finished cases. An agent ASKING a question → answer those ids with sendCommand and await again. FAILED → drop it and note it; do NOT respawn in a loop. STILL WORKING after budget → re-await exactly those ids, at most twice (three awaitAll total on one terminal). After that, proceed without it if others finished, or hand it to `terminal.await.async` and end the turn if it is the only work left.
   - If the SAME terminal is the lone straggler in two consecutive rounds, stop awaiting it inside the cohort: read the fast agents immediately, then await ONLY the straggler at a higher bounded budget.

4. Relay with `terminal.sendCommand({terminalId, command})`. Send each agent ONLY what it needs from the OTHERS — their facts/drafts/votes/messages — plus this round's one question. Condense first: the outputs from step 3 are raw material. Relay the GIST, labeled by agent, never raw scrollback unless the task specifically requires exact wording. One sendCommand per agent, all in ONE BATCH, with the round's terminal.awaitAll as the LAST call in that same batch; dispatch is sequential, so every send completes and then polling begins in one model turn.
   - For open-ended conversation facilitation, relay the actual message content, not merely a summary that a message exists. If one participant drafts a reply and asks whether to send it, and the user asked you to facilitate the conversation, forward the drafted reply unless it requires a real user decision or safety confirmation. Keep going for 2–4 exchanges or until a participant clearly reaches a natural stopping point; do not stop after one exchange just because an agent claims it contacted the other by some private bridge.
   - If the user sends feedback MID-ORCHESTRATION (via interruptedByUser:true or a message between rounds), fold it into the NEXT relay — do NOT restart the orchestration.
   - Before closing each round, OVERWRITE the `orchestration_state` checkpoint with the new round / slowCounts / droppedTerminalIds and any gist you'll need next round. Keep the value compact — ids, counts, one-line gists, never raw scrollback or whole answers.

5. Synthesize and report ONE clean answer — the tally, winner, merged result, or conversation outcome — not a transcript of every terminal unless the user asked for the transcript. Quote real terminalIds where useful.

6. Close out. Driving in-turn you attached no watchers, so there is usually nothing to cancel; resolve any inbox item you created with `queue.resolve`. Leave the cohort's terminals open and idle unless the user asks to close them. Closing a terminal is NEVER your own initiative: only on the user's explicit request. When they DO ask, close ONLY the exact cohort ids you recorded this run; NEVER include a terminal you did NOT spawn unless the user explicitly named that existing terminal for this run's cleanup. For a competitive workflow the user may ask to close only LOSERS and keep the winner(s) and any judge open — close only those. Drop your scratch store in the same cleanup with `scratch.drop({"storeId":"scr_…"})` so the per-session store budget is not exhausted; keep it while the cohort is alive.

Anti-patterns (the non-obvious traps):
- One-shot batching the whole orchestration — putting scratch.set/get, awaitAll, extract, or sendCommand in the SAME batch as the scratch.create/spawns that mint the ids they need.
- Missing the conversation trigger — the user says 'facilitate a back and forth', 'let them talk', or 'see where it goes', and you merely summarize old outputs. Start the sendCommand/read relay loop.
- Treating a claimed direct agent-to-agent call as fulfilling a user request to facilitate a back-and-forth. The user asked YOU to relay; summarize only after you have actually run the loop, or if the user asked only for a summary.
- Attaching a watcher AND driving in-turn — pick one.
- Ignoring an awaitAll that returned `interruptedByUser:true` — the user typed mid-wait and their message is ALREADY in the conversation.
- Trusting a sparse or partial extract as the complete answer — extract sees only the visible tail, so fall back to terminal.read.
- Asking agents for JSON or schema-shaped output, or reading the cohort through terminal.extract.json.
- Hunting agent output through artifact.read instead of reading terminals; use artifact.read only to page terminal.read content archived as an artifact.
- Relaying on awaitAll status ALONE without reading the tail — idle ≠ done.
- Re-awaiting a stuck agent without limit — bound it to 2 re-awaits, then proceed-without-it or use terminal.await.async and end the turn.
- Abbreviating/truncating a terminalId, or padding agent prompts.
- Closing a terminal you did NOT spawn, or closing tabs as cleanup of leftover/stale/extra terminals — only your recorded cohort, only on explicit ask.
- Commandeering a terminal you did NOT spawn — build the cohort from your own fresh spawns unless the user explicitly names an existing terminal to include.
- Telling an agent to communicate DIRECTLY with another agent or terminal — 'send your answer to Codex', 'coordinate with the other agent', 'reply in the other terminal'. Relay every hand-off yourself with terminal.sendCommand.

Report back: the synthesized result or conversation outcome, which agents participated (real terminalIds), and any inbox/terminal cleanup you did.
