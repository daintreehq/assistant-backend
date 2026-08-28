---
id: daintree.agents.recover-agent
title: Recover a broken agent or spawn
summary: Triage an agent that went wrong — read the durable spawn saga, tell a hang from a bad id, and adopt an unsupervised agent instead of restarting it.
whenToUse: Use when something has gone WRONG with an agent or its spawn — "my agent is stuck", "it has been sitting there for ages", "did that agent even start?", "the spawn failed", "nothing happened when you launched it", "that terminal id does not work", "this agent has no watcher on it", "there is an agent running from earlier that nobody is watching", "is it hung or just slow?". Covers a launch that returned no terminalId or an ambiguous result, a hung or unresponsive agent, a stale or mistyped terminal id, and adopting an orphaned or unsupervised agent. Do not use when the agent is healthy and merely ASKING something — answer it with daintree.agents.answer-waiting-agent; for routine triage of a fleet where nothing is broken use daintree.orchestration.supervise-fleet or daintree.orchestration.status-and-resume; to start fresh work use daintree.edits.spawn-visible-agent.
confirmation: "Recovering an agent that has gone wrong. The aim is to identify which failure this actually is before acting, since a hang, a stale terminal id and a spawn that never landed need different moves."
risk: terminal
requiredTools:
  - context.snapshot
  - agentTask.status
  - agentTask.list
  - agentTask.superviseTerminal
  - agentTask.spawnForEdits
  - watcher.list
  - watcher.cancel
  - terminal.awaitAll
  - terminal.await.async
  - terminal.summarize
  - terminal.read
  - queue.resolve
---
Use when: an agent or its spawn has gone wrong — the launch returned no terminalId, an agent has stopped responding, a terminalId no longer resolves, or a running agent has nobody supervising it. Four failure shapes, one triage.

You are recovering the SUPERVISION, not the work — restore the loop and let an agent do the engineering.

Non-goals, absolute: never replace a terminal unless the user explicitly asked; never blindly resend the original command into a terminal you do not understand; never respawn in a loop — one corrective action, then hand it to the user.

Procedure:
1. Name the shape before you act. Read live state first, then two cheap reads that settle most cases: `agentTask.list({})` (this session's spawn sagas, newest first, each with its stage, bound terminal/watcher and any error; add `{"scope":"all"}` only when the user asks about earlier sessions — a stage that still looks live is always from THIS session, since a prior session's in-flight sagas are swept to failed on open) and `watcher.list({})` (which terminals actually have a supervisor). Then branch: no terminal ever appeared → step 2; the terminal exists but is unresponsive → step 3; a read says "Terminal not found" or a whole cohort looks frozen seconds after spawn → step 4; it is running but unsupervised → step 5.

2. SPAWN DID NOT LAND — read the saga, do not guess. `agentTask.status({"launchId":"<agt_...>"})` returns the durable record for one launch: its stage — launch_requested, agent_started, terminal_bound, watcher_attached, confirmed, or failed / ambiguous — plus the bound terminalId/watcherId and any error detail. Act on the stage:
   - terminal_bound, watcher_attached or confirmed — the spawn LANDED. Take the bound ids from the saga, use them verbatim, carry on. The launch call's return was merely uninformative. Do not respawn.
   - launch_requested or agent_started on a fresh launch — still in flight, not failed. Say so, re-read once, give it a moment.
   - failed — quote the error detail verbatim to the user, then respawn AT MOST ONCE with agentTask.spawnForEdits, correcting exactly what the error names (an unknown agentId, resolved against the available-agent registry in your startup context, or a wrong worktreeId). The same failure twice is not a retry problem: stop and hand it over.
   - ambiguous — do NOT respawn. Two agents in one worktree is worse than none. Identify the real terminal from the roster by title/worktree, confirm with a bounded tail read, then adopt it per step 5.
   Never "fix" a failed spawn by closing terminals. A failed launch did not create the terminal you would be closing, so closing one destroys somebody else's work.

3. HUNG — bound it under the foundation's wait rule, then be honest. What that rule does not cover is the rest of the fleet and the wording of your report. First rule out the two look-alikes: an agent in `askingQuestion`, or at waitingReason "question" or "approval", is not hung, it is blocked on you — answer or approve it (answer-waiting-agent); one at waitingReason "error" is not hung either, it stopped on a blocking error (rate limit, auth, network) and needs that fixed, not a longer wait; a cohort that all reads still-working immediately after spawn is a bad id — step 4. Then do not hold the finished agents hostage: relay their results in THIS turn and report the hung one separately, so one straggler does not stall five completed jobs. Take ONE bounded read of the stuck terminal for evidence (terminal.summarize, or terminal.read when you need the literal last lines) and report what it actually shows, the terminalId, roughly how long it has been silent, and the async handle now watching it. Never dress up awaitAll's `working` as progress — it is a state, not evidence that anything is happening.

4. BAD ID vs SLOW AGENT — the cheapest fix, always try it before escalating. Run the foundation's id check before you conclude anything about the agent, correct any mismatch, and resume the SAME wait. Never respawn a healthy agent because you mistyped its id.

5. ORPHANED or UNSUPERVISED — adopt, do not restart. Check watcher.list FIRST: if a watcher already names that terminal it IS supervised, so stop — a second one would stack supervisors. Otherwise confirm it is a live agent and not a corpse: a CLI that errored on startup sits at a shell prompt and reports `waiting`, indistinguishable from an idle agent. Take one bounded tail read; if it shows a startup error, usage text, or a bare shell prompt rather than agent output, treat it as FAILED, not idle — report it and respawn AT MOST ONCE. Never attach a supervisor to a dead shell; it would wait forever. For a genuinely live agent, adopt it with `agentTask.superviseTerminal({"terminalId":"<id>","goal":"<what completion looks like>","acceptanceCriteria":"<concrete definition of done>","spawnMode":"edit","worktreeId":"<id>"})` — terminalId is the only required field, spawnMode is "edit" or "explore" (use "explore" so the supervisor does not expect file changes), and worktreeId scopes its post-completion git check. It launches nothing and edits nothing, and it dedupes, so a repeat call never double-attaches. Then END the turn and react when the watcher wakes you.

6. Close the loop honestly. Say plainly which watcher you adopted, which you cancelled with watcher.cancel, and what remains unsupervised. If your one corrective action — respawn, adopt, or id fix — did not hold, STOP: report the ids and the error and let the user decide. A second respawn is a loop.

Confirmation: adopting a supervisor is cheap and reversible, but respawning an agent or sending input into a live terminal mutates real project state — confirm per the active tier before either.

Report back: which failure shape it was and the evidence for it (the saga stage or error detail, verbatim); the launchId, terminalId and watcherId involved; what you recovered and what you deliberately did NOT do (no terminal closed, no blind resend, at most one respawn); what is still running, and what will wake you or what you need from the user.
