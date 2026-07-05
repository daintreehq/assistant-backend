---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn several agents, relay their work between them with terminal.sendCommand,
  and synthesize one answer — collaborative or competitive multi-agent tasks.
whenToUse: Use when the user wants several agents to work one problem TOGETHER (or
  against each other) — collaborate, cross-check, debate, vote, or combine outputs
  — and you must coordinate the back-and-forth between the terminals.
priority: 95
risk: project
tags:
  - daintree
  - orchestration
  - multi-agent
  - agents
  - collaborate
  - relay
  - vote
  - debate
  - fan-out
  - sendcommand
requiredTools:
  - context.snapshot
  - agentTask.spawnForEdits
  - agentTask.status
  - agentTask.list
  - terminal.sendCommand
  - terminal.summarize
  - terminal.extract
  - terminal.extract.json
  - terminal.awaitAll
  - terminal.list
  - terminal.read
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
Use when: the user wants several agents to collaborate, compete, or cross-check on a single problem, and you are the conductor relaying between them.

The pattern is a loop: spawn a cohort → collect each one's output → relay the others' output back to each → ask for the next step → repeat → synthesize one answer. terminal.sendCommand is how you relay; it is always callable (every turn, including a watcher wake) — never hunt for it with tool.search.

CRITICAL — turn-by-turn id discipline. Every `scr_…` storeId and every `terminal-…` id is MINTED by a call (scratch.create, agentTask.spawnForEdits) and only EXISTS once that call RETURNS — which lands in your results on the NEXT turn. A batch's arguments are all frozen before any call in it runs, so a call CANNOT use an id an earlier call in the SAME batch produced; guessing one yields a fake that misses every time ("no scratch store", "No live terminal matches", "Terminal not found"). So run the loop strictly turn by turn: emit the producers (create store, spawn cohort), END the turn; next turn read the REAL ids from the results, THEN scratch.set / awaitAll / extract / sendCommand against them. This bans ONLY consuming ids minted earlier in the same batch — reusing ids you already hold from a prior turn SHOULD still be batched (later rounds batch the sendCommands + a trailing awaitAll). Never one-shot the whole orchestration as one giant batch — that is the #1 way this detonates.

Copy every id EXACTLY and in FULL from the tool result — a terminalId looks like `terminal-5284bfef-3d11-424c-90cb-136f24046295`. Never abbreviate it to a `terminal-5284bfef` prefix or rebuild one from memory: Daintree matches the whole string, so a truncated id matches NO terminal — awaitAll/getStatus report it "still working" forever and terminal.read returns "Terminal not found". Store each id full-length in scratch.

Procedure:
1. Scope it. Decide the cohort (which agents — claude, codex, gemini, antigravity, …; resolve any user-spoken name against the roster, since a mis-transcribed id launches a dead terminal) and the rounds (e.g. round 1: each answers; round 2: each sees the others and votes). Don't over-plan — the loop is the same regardless of round count.
   - Create ONE scratch store up front as compaction-safe state: `scratch.create({"name":"multi-agent-orchestration"})`; hold the returned `scr_…` storeId (there is no way to list stores, so this handle is the only way back). Keep the evolving state — terminalId→role map, running scores, round counter, chronic-straggler counts — in ONE key (`orchestration_state`) that you OVERWRITE each round (never a key per round). Pass `value` as a real JSON object, not a string. WHY: a long run crosses the auto-compaction threshold, which replaces the transcript with a lossy summary and drops state living only in prose; the scratch store lives outside the transcript and survives it (but NOT a process restart — re-scope if the assistant restarts mid-run).

2. Spawn the cohort in ONE BATCH — one agentTask.spawnForEdits per agent (groups of ~4 per turn), each with a distinct `title` and a SHORT, self-contained taskPrompt. The title becomes the tab name (`<Agent>: <title>`) the instant the agent launches, so name it right at spawn — do NOT follow the spawn batch with terminal.rename calls (renaming agents you just titled is pure waste; reserve rename for pre-existing tabs or a fleet whose titles already collide). Pick `mode:"explore"` for thinking/answering/analysis that touches NO files (the read-only constraint is appended automatically and is task-neutral), `mode:"edit"` only when they must change files in a worktree. Keep each prompt MINIMAL — state the one thing you want and stop; a padded or self-contradictory prompt is a top failure. Do NOT attach a watcher (`watch`/`watchGoal`): you are the conductor driving rounds in-turn, and a background watcher is a SECOND supervisor that fights your driving and floods the inbox with redundant items. Driving in-turn does NOT trap the user — terminal.awaitAll is INTERRUPTIBLE (if they type mid-wait it returns `interruptedByUser:true` with their message already folded into the turn), so you never need a watcher as an "unblock" net. (If the user instead wants a fire-and-forget "wake me later" run, attach a watcher, end the turn, and react on the wake — but then do NOT also wait in-turn.)
   - Keep a terminalId→role map — titles can collide (two "Claude" tabs), so the terminalId each spawn returns is your only reliable routing handle. On the turn the spawns RETURN their ids (the turn AFTER the spawn batch, never inside it), and before you wait or relay, write the initial state to scratch so a later compaction can't strand it: `scratch.set({"storeId":"scr_…","key":"orchestration_state","value":{"round":1,"terminalRoles":{"terminal-5284bfef-3d11-424c-90cb-136f24046295":"claude/affirmative","terminal-2e8f0743-b416-4900-816c-297a947874ab":"codex/negative"},"activeTerminalIds":["terminal-5284bfef-3d11-424c-90cb-136f24046295","terminal-2e8f0743-b416-4900-816c-297a947874ab"],"droppedTerminalIds":[],"scores":{"affirmative":0,"negative":0},"slowCounts":{}}})` — each terminalId in FULL.

3. Re-anchor, then wait for the WHOLE cohort. At the top of every round, FIRST `scratch.get({"storeId":"scr_…","key":"orchestration_state"})` and restore terminalRoles / activeTerminalIds / scores / slowCounts / round — after a compaction the transcript may no longer hold them, so scratch is your source of truth. (If that get is RECOVERING ids you no longer hold, they arrive in its result next turn — await them THEN; when you already hold the ids, the get and awaitAll share one batch.) Then ONE `terminal.awaitAll({terminalIds:[t1,t2,t3]})`: it polls every agent's agentState concurrently (no model call, no output read) and resolves when each has gone working→idle (or completed/exited) — fast and light, but agentState is IMPERFECT (an agent can read idle while parked/paused/backgrounded mid-work), so "finished" is a strong hint, NOT proof. Size the budget up front: start a multi-round cohort with `maxAttempts:60` (≈120s); the default 30 (≈60s) only fits a fast, single-shot cohort. Do NOT issue one terminal.extract wait:{} per agent — that is the single-agent path and stacks its timeouts one after another.
   - READ + SELF-HEAL after awaitAll reports finished. Pull every agent's DISTINCT answer in ONE `terminal.extract.json` over ALL the terminalIds: pass an `instruction` AND a `jsonSchema` that is a **real JSON Schema** (only the keywords `type`/`properties`/`items`/`required`/`enum`) whose value is an array of objects each carrying its own `terminalId` — e.g. `{"type":"object","properties":{"answers":{"type":"array","items":{"type":"object","properties":{"terminalId":{"type":"string"},"fact":{"type":"string"}},"required":["terminalId","fact"]}}},"required":["answers"]}` (swap `fact` for `vote`/`draft`/whatever the round produced). Do NOT pass an example value shape like `{"answers":[{"terminalId":"string","fact":"string"}]}` — the backend rejects it (`result_schema key 'answers' is not allowed`); it wants a schema, not a sample. If you pass `maxTokens` it MUST be between 1 and 2000 (`INVALID_ARGS: maxTokens must be between 1 and 2000`) — omit it to default, or split the read. The multi-terminal tail is labelled `[terminalId]`, so ONE call returns one attributed entry per agent; when it comes back `matched:true` with one entry each, those entries ARE the answers — go straight to scoring/synthesis and do NOT re-extract per agent afterward (same in every round: facts, votes, drafts). Use plain-text terminal.extract (no schema) only for a SINGLE terminal or a deliberately BLENDED read; a bounded terminal.read is fine for one agent's short verbatim answer. If extract.json fails (502/parse error), fall back to terminal.summarize or plain-text terminal.extract per agent — the structured extract is a convenience, not a requirement.
   - CRITICAL LIMITATION — every extract tool (extract.json AND plain-text extract) reads ONLY the terminal's visible TAIL, the last screenful. When an agent's answer is long (multi-question, multi-paragraph), earlier content has scrolled off and is INVISIBLE to extract: it can return `matched:true` yet MISS earlier answers (only the last question answered, `[Not present in terminal output]` for the rest, or a one-word verdict where the full reasoning scrolled past). When an extract looks sparse or partial, do NOT trust it as complete — fall back to `terminal.read` (the full scrollback, archived as an artifact if large — page through with artifact.read) or terminal.summarize, and re-read the missing content before you relay or synthesize. No extract tool sees beyond the tail, so terminal.read is the reliable path for long output.
   - Sanity-check each: if a terminal reported "finished" but its tail shows it is still working (a half-finished message, a live spinner, no real answer yet), do NOT relay it — re-await just that one, watch it, or send a visible agent to look closer. For garbled multi-screen TUI use terminal.summarize. Never re-summarize or re-extract an extract result (a model read of a model read).
   - Suspect a BAD ID before a slow agent. If awaitAll reports the ENTIRE cohort still-working for the whole budget — especially round 1 right after spawn, when a quick task finishes in seconds — or a read returns "Terminal not found", re-read the live ids with terminal.list (or context.snapshot), confirm each id matches a listed terminal verbatim and in full, fix any mismatch in your scratch map, then resume. A whole cohort stuck for minutes on trivial work is the signature of a bad/truncated terminalId, not slow agents.
   - Handle the non-finished cases (awaitAll names them at the top level as `stillWorking` / `askingQuestion` id arrays). An agent ASKING a question → answer those ids with sendCommand (step 4) and await again. FAILED → drop it and note it in the synthesis; do NOT respawn in a loop. FAILED covers a nonzero exit AND a terminal counted "finished" whose tail shows a CRASH instead of your answer (an error id, "terminated with an error", an empty/garbled screen — Daintree often reports a CLI that errored on launch as "waiting", so awaitAll mislabels it finished): tell the user plainly, re-spawn AT MOST once if they still want it (a fresh id next turn — update the map), and don't silently re-spawn-and-re-block. STILL WORKING when the budget ran out → re-await EXACTLY the `stillWorking` ids (not the whole cohort), raising `maxAttempts` (max 240 ≈ 480s) for a genuinely slow agent, but BOUND it: re-await at most twice (three awaitAll total on one terminal). After two re-awaits with no finish the agent is HUNG — stop (never a fourth await) and take the defined exit: if others finished, synthesize from them and note it plainly ("agent `<terminalId>` never finished — omitted"); if the hung agent is the ONLY work left, hand it to the background and END the turn —
   ```
   queue.publish({ "source": "model_worker", "severity": "blocked",
     "title": "Agent hung — did not finish",
     "summary": "Agent <terminalId> still working after 2 re-awaits; attaching a watcher so you're notified when it finishes.",
     "target": { "terminalId": "<terminalId>" }, "dedupeKey": "hung-<terminalId>" })
   watcher.terminal.create({ "terminalIds": ["<terminalId>"], "title": "hung agent recovery",
     "goal": "notify when the stuck agent finishes",
     "stopWhen": { "all": [ { "stateIs": "waiting" }, { "modelJudge": "Has the agent finished its work and stopped, not just paused?" } ] } })
   ```
   Never relay a half-done or dead screen.
   - Isolate a chronic straggler. If the SAME terminal is the lone `stillWorking` entry in two consecutive rounds, stop awaiting it inside the cohort: read the fast (already-idle) agents immediately with a no-wait extract, then await ONLY the straggler in its own call at a higher budget (`maxAttempts:120`–`240`). A cohort awaitAll dispatches in emission order, so leaving the slow terminal in it blocks every read queued behind it.

4. Relay with terminal.sendCommand({terminalId, command}). Send each agent ONLY what it needs from the OTHERS — their facts/drafts/votes — plus this round's one question (e.g. "Here are the other two answers: A: … B: … Which is better and why?"). Condense first: the outputs from step 3 are raw material — relay the GIST (a few sentences per agent, labeled `**AGENT_NAME**: <condensed answer>` so a judge can attribute and compare), never raw scrollback. One sendCommand per agent, all in ONE BATCH, with the round's terminal.awaitAll as the LAST call in that same batch — dispatch is sequential, so every send completes and then polling begins in one model turn; issuing the awaitAll in a separate turn wastes a round-trip per relay cycle. Then collect again (step 3) and repeat for as many rounds as the task needs. (Exception: while isolating a straggler, the fast agents are already idle — read them straight away and give the straggler its own await; don't bundle a cohort awaitAll for agents that already finished.)
   - If the user sends feedback MID-ORCHESTRATION (via interruptedByUser:true or a message between rounds), fold it into the NEXT relay — do NOT restart the orchestration. E.g. "be more terse" → append "Keep your answer terse." to the next sendCommand. Acknowledge in one line, then continue.
   - Before closing each round, OVERWRITE the `orchestration_state` checkpoint with the new round / scores / slowCounts / droppedTerminalIds and any gist you'll need next round. Keep the value compact — ids, scores, counts, one-line gists, well under a few KB — never raw scrollback or whole answers (a bulky value risks the ~10 000-rune cap and pages out of the inline result).

5. Synthesize and report ONE clean answer — the tally, the winner, the merged result — not a transcript of every terminal. Quote real terminalIds where useful.

6. Close out. Driving in-turn you attached no watchers, so there is usually nothing to cancel; resolve any inbox item you created with `queue.resolve {"id":"<inbox id>"}`. Leave the cohort's terminals open and idle unless the user asks to close them — you can send another round anytime. Closing a terminal is NEVER your own initiative: only on the user's explicit request. When they DO ask, retire the cohort in one confirmed `terminal.close({terminalIds:["<id>",…]})` — close ONLY the exact cohort ids you recorded this run; NEVER include a terminal you did NOT spawn (a pre-existing tab is very likely the user's OWN working agent, and closing it destroys live work irreversibly). For a COMPETITIVE workflow (interview, debate, vote) the user may ask to close only the LOSERS and keep the winner(s) and any judge open — close only those. Drop your scratch store in the same cleanup — `scratch.drop({"storeId":"scr_…"})` — so the per-session store budget (20) isn't slowly exhausted; keep it while the cohort is alive.

Anti-patterns (the non-obvious traps):
- One-shot batching the whole orchestration — putting scratch.set/get, awaitAll, extract, or sendCommand in the SAME batch as the scratch.create/spawns that mint the ids they need. The ids don't exist yet, so you fabricate fakes and every call misses.
- Attaching a watcher AND driving in-turn — pick one (this runbook is in-turn, so NO watcher). Two supervisors on the same terminals is the top cause of churn; the "watchers keep the user unblocked" excuse is false, because awaitAll is interruptible.
- Ignoring an awaitAll that returned `interruptedByUser:true` — the user typed mid-wait and their message is ALREADY in the conversation; read and act on it FIRST before re-awaiting or relaying.
- Trusting a sparse or partial extract as the complete answer — extract sees only the visible tail, so when it looks partial fall back to terminal.read (full scrollback) before you relay or synthesize.
- Re-extracting each agent after a successful cohort extract.json — the one call already returned an attributed entry per agent; go straight to scoring/synthesis (same every round).
- Hunting agent output through artifact.read (especially looping at climbing offsets against ONE id) — read from the terminals via step 3. Never retry an `ARTIFACT_NOT_FOUND` (`recoverable:false`) — the content is gone, so proceed without it.
- Relaying on awaitAll status ALONE without reading the tail — idle ≠ done; self-heal any terminal that reported finished yet still looks busy before relaying.
- Re-awaiting a stuck agent without limit — bound it to 2 re-awaits, then proceed-without-it (note it) or hand it to a watcher and end the turn.
- Abbreviating/truncating a terminalId, or padding agent prompts — both are top failures (see above).
- Closing a terminal you did NOT spawn, or closing tabs as "cleanup" of leftover/stale/extra terminals — only your own recorded cohort, only on the user's explicit ask.

Report back: the synthesized result (winner/tally/merged answer), which agents participated (real terminalIds), and any inbox cleanup you did.
