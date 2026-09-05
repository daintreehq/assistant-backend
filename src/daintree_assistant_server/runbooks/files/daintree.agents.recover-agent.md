---
id: daintree.agents.recover-agent
title: Recover a broken agent or spawn
summary: Triage an agent that went wrong — read the durable spawn saga, tell a hang from a bad id, and adopt an unsupervised agent instead of restarting it.
whenToUse: Use when something has gone WRONG with an agent or its spawn — "my agent is stuck", "it has been sitting there for ages", "did that agent even start?", "the spawn failed", "nothing happened when you launched it", "that terminal id does not work", "this agent has no watcher on it", "there is an agent running from earlier that nobody is watching", "is it hung or just slow?". Covers a launch that returned no terminalId or an ambiguous result, a hung or unresponsive agent, a stale or mistyped terminal id, and adopting an orphaned or unsupervised agent. Do not use when the agent is healthy and merely ASKING something — answer it with daintree.agents.answer-waiting-agent; for routine triage of a fleet where nothing is broken use daintree.orchestration.supervise-fleet or daintree.orchestration.status-and-resume; to start fresh work use daintree.edits.spawn-visible-agent. Do NOT use when the agent and its launch are both healthy and the user is questioning something YOU did — "why did you not wait", "why did you spawn that" — a complaint about your own handling is not a broken agent, so answer it directly rather than opening a recovery.
confirmation: "Recovering an agent that has gone wrong. The aim is to identify which failure this actually is before acting, since a hang, a stale terminal id and a spawn that never landed need different moves."
risk: terminal
requiredTools:
  - context.snapshot
  - agentTask.status
  - agentTask.list
  - agentTask.superviseTerminal
  - agentTask.spawnForEdits
  - watcher.list
  - async.list
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
1. Name the shape before you act. Read live state first, then two cheap reads that settle most cases: `agentTask.list({})` (this session's spawn sagas, newest first, each with its stage, bound terminal/watcher and any error; add `{"scope":"all"}` only when the user asks about earlier sessions — prior-session in-flight sagas can be marked failed on open, so reconcile bound live resources instead of treating their stored failure as proof the agent never started) and `watcher.list({})` / `async.list({})` (which terminals already have supervision). Then branch: no terminal ever appeared → step 2; the terminal exists but is unresponsive → step 3; a read says "Terminal not found" or a whole cohort looks frozen seconds after spawn → step 4; it is running but unsupervised → step 5.

2. SPAWN DID NOT LAND — read the saga, do not guess. `agentTask.status({"launchId":"<agt_...>"})` returns the durable record for one launch: its stage — launch_requested, agent_started, terminal_bound, watcher_attached, confirmed, or failed / ambiguous — plus the bound terminalId/watcherId and any error detail. Act on the stage:
   - terminal_bound, watcher_attached or confirmed — the spawn LANDED. Take the bound ids from the saga, use them verbatim, carry on. The launch call's return was merely uninformative. Do not respawn.
   - launch_requested or agent_started on a fresh launch — still in flight, not failed. Say so, re-read once, give it a moment.
   - failed — report the relevant error detail with credentials removed, inspect any bound terminal/worktree, then respawn AT MOST ONCE only when launch failure is established with agentTask.spawnForEdits, correcting exactly what the error names (an unknown agentId, resolved against the available-agent registry in your startup context, or a wrong worktreeId). The same failure twice is not a retry problem: stop and hand it over.
   - ambiguous — do NOT respawn. Two agents in one worktree is worse than none. Identify the real terminal from the roster by title/worktree, confirm with a bounded tail read, then adopt it per step 5.
   Never "fix" a failed spawn by closing terminals. A failed launch did not create the terminal you would be closing, so closing one destroys somebody else's work.

3. HUNG — bound it under the foundation's wait rule, then be honest. What that rule does not cover is the rest of the fleet and the wording of your report. First rule out the two look-alikes: an agent in `askingQuestion`, or at waitingReason "question" or "approval", is not hung, it is blocked on you — answer or approve it (answer-waiting-agent); one at waitingReason "error" is not hung either, it stopped on a blocking error (rate limit, auth, network) and needs that fixed, not a longer wait; a cohort still working immediately after spawn may simply be starting; check ids in step 4 only when they mismatch or a read fails. Then do not hold the finished agents hostage: relay their results in THIS turn and report the hung one separately, so one straggler does not stall five completed jobs. Take ONE bounded read of the stuck terminal for evidence (terminal.summarize, or terminal.read when you need the literal last lines) and report what it actually shows, the terminalId, roughly how long it has been silent, and the async handle now watching it. Never dress up awaitAll's `working` as progress — it is a state, not evidence that anything is happening.

4. BAD ID vs SLOW AGENT — the cheapest fix, always try it before escalating. Run the foundation's id check before you conclude anything about the agent, correct any mismatch, and resume the SAME wait. Never respawn a healthy agent because you mistyped its id.

5. ORPHANED or UNSUPERVISED — adopt, do not restart. Before calling `agentTask.superviseTerminal`, obtain current results from BOTH `watcher.list` and `async.list`, even when the launch reported a watcher-storage failure. That failure does not rule out another supervisor or async handle. If current results for both are already present, reuse them. If either read fails, supervision coverage is unknown: report that blocker rather than attach a duplicate. If an active watcher or async handle already covers this task, retain it rather than adding another. A terminal listed by a stale or unrelated watcher still needs its goal/scope checked. Otherwise confirm it is a live agent and not a corpse: a CLI that errored on startup sits at a shell prompt and reports `waiting`, indistinguishable from an idle agent. Take one bounded tail read; if it shows a startup error or usage failure, treat the launch as failed. A bare prompt with no error is ambiguous, not enough to relaunch. Report the evidence and use one authorized corrective spawn only when failure is established. Never attach a supervisor to a dead shell; it would wait forever. For a genuinely live agent, adopt it with `agentTask.superviseTerminal({"terminalId":"<id>","goal":"<what completion looks like>","acceptanceCriteria":"<concrete definition of done>","spawnMode":"edit","worktreeId":"<id>"})` — terminalId is the only required field, spawnMode is "edit" or "explore" (use "explore" so the supervisor does not expect file changes), and worktreeId scopes its post-completion git check. It launches nothing and edits nothing, and it normally reuses an existing active spawn supervisor. A deduped response does NOT update its goal, mode, acceptance criteria or worktree; check those before accepting it. Then END the turn and react when the watcher wakes you.

6. Close the loop honestly. Say plainly which watcher you adopted, which you cancelled with watcher.cancel, and what remains unsupervised. If your one corrective action — respawn, adopt, or id fix — did not hold, STOP: report the ids and the error and let the user decide. A second respawn is a loop.

Confirmation: adopting a supervisor is cheap and reversible, but respawning an agent or sending input into a live terminal mutates real project state — confirm per the active tier before either.

A failed saga can still contain a terminalId or worktreeId from a partial launch; preserve those resources and reconcile before retrying. The launch idempotency key depends on the effective title/prompt/agent/worktree, so changing the title to force a retry can create a second worker. Use status/list to recover the original result. A read timeout or an exhausted foreground budget is not proof of a dead agent; distinguish slow, blocked, failed, disappeared and unknown. Never expose raw environment/configuration fields in a recovery report.

Report back: which failure shape it was and the evidence for it (the saga stage or relevant safe error detail); the launchId, terminalId and watcherId involved; what you recovered and what you deliberately did NOT do (no terminal closed, no blind resend, at most one respawn); what is still running, and what will wake you or what you need from the user.
