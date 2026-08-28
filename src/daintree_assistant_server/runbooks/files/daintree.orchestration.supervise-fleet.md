---
id: daintree.orchestration.supervise-fleet
title: Supervise a running agent fleet
summary: Triage every agent already running — finished, working, waiting, failed, unsupervised — in one state-only sweep, act on each class, and report what still needs the user.
whenToUse: Use when agents are ALREADY running and the user wants the fleet swept and acted on — "how's everyone doing?", "anything need me?", "is anyone stuck?", "catch me up on the agents". Also for adopting a running-but-unsupervised agent, "show me the next one" navigation, and arming terminals for the user's typed input. Do not use for a plain state read with no action (daintree.foundation), for answering agents that are ASKING (daintree.agents.answer-waiting-agent), for rescuing one broken or hung agent (daintree.agents.recover-agent), for handing out new work (daintree.orchestration.fan-out-work), for "where were we" across sessions (daintree.orchestration.status-and-resume), for proving one agent's work landed (daintree.orchestration.verify-agent-work), for watching past this turn (daintree.orchestration.watch-outcome), or for retitling tabs (daintree.terminals.rename).
confirmation: "Checking in on the agents already running. The aim is to say where each one stands and which few need the user, rather than to form a view on the work itself."
risk: terminal
requiredTools:
  - context.snapshot
  - terminal.awaitAll
  - terminal.summarize
  - terminal.read
  - terminal.await.async
  - agentTask.superviseTerminal
  - agentTask.list
  - agentTask.status
  - watcher.list
  - async.list
  - timer.list
  - workflow.list
  - queue.digest
  - queue.resolve
  - queue.publish
  - terminal.focus
  - terminal.arm
  - terminal.disarm
  - terminal.disarmAll
---
Use when: agents are already running and the user wants the whole fleet triaged and acted on — "how's everyone doing?", "anything need me?", "who's stuck?" — including agents you did not spawn this turn.

You are triaging the fleet, not joining it. Non-goals: never write product code, read a diff, or form your own engineering opinion about an agent's work — relay what the agent reported and spawn an agent if judgment is needed. Never commandeer a running terminal as a worker; it belongs to its own task. Never close a terminal to tidy up after a sweep. The output is a report plus the few unblocking actions the user asked for.

Procedure:

1. Read the roster you already have. The turn context carries a fresh terminal list with id, title, agent state, waiting reason, exit code and worktree — classify from that; do NOT open with context.snapshot. Call it only to re-confirm ids immediately before a mutation (adopt, arm, send), or when the roster reads empty and the connection itself is suspect. If the user pointed at a subset ("the three review agents"), keep only those ids.

2. Classify the cohort with ONE state-only call. `terminal.awaitAll({"terminalIds":[...],"maxAttempts":3})` polls agent state only — no model call, no output read — and returns a perTerminal status of exactly "finished" | "failed" | "question" | "working", plus top-level stillWorking and askingQuestion id arrays. Keep maxAttempts LOW (1-3): you are taking a reading, not waiting on anyone. ONE call for the cohort, never one per terminal; the cap is 16 ids, so a larger fleet needs consecutive calls of 16 or fewer.
   THE SWEEP TAKES OWNERSHIP. Every terminal that settles finished or failed has its spawn-attached supervising watcher RETIRED by that call (watchersRetired in the result). No completion notification will follow for those agents, so you must read and report them in THIS turn — and must not watcher.cancel them yourself.
   Five classes fall out: FINISHED (settled, and confirmable in the tail), WORKING, WAITING-ON-A-QUESTION (status "question", or a roster waiting reason of "question"), FAILED/EXITED (status "failed", or an exit code on the roster row), and UNSUPERVISED — running, but its id appears in no watcher from step 5.

3. Read the finished ones in ONE batched round. Emit one `terminal.summarize({"terminalId":"<id>","purpose":"what this agent finished and whether it landed"})` per finished terminal, all in the same reply — consecutive summarize and no-wait extract calls dispatch CONCURRENTLY, so the whole cohort costs about the slowest single call. Do NOT use one terminal.extract across many ids: it MERGES every tail into one answer and cannot tell you who did what. Reach for terminal.read only when the exact wording matters.

4. Branch by class, in one pass:
   - FINISHED — relay the outcome in the user's terms, then `queue.resolve({"id":"<eventId>"})` its inbox item once you have ACTUALLY reported it. If the user wants proof the change landed rather than the agent's own claim, that is the verify-agent-work runbook.
   - WAITING-ON-A-QUESTION — hand it to the answer-waiting-agent runbook; do not invent an answer here. Questions that genuinely need the user get collected and asked ONCE, together.
   - FAILED/EXITED — report the terminal id, the exit code, and the last error line. Do not respawn it, and never respawn in a loop; recovery is the recover-agent runbook and the decision is the user's.
   - UNSUPERVISED — offer to adopt it. `agentTask.superviseTerminal({"terminalId":"<id>","title":"<what it is doing>","goal":"<what done looks like>","spawnMode":"edit"|"explore","worktreeId":"<id>"})` attaches a durable supervisor WITHOUT respawning the agent — right for an agent left from an earlier session, or one whose watcher was retired. Only terminalId is required; pass acceptanceCriteria when there is a concrete definition of done. It mutates and confirms, and it DEDUPES: if an active supervisor already targets that terminal it returns that watcher with deduped:true rather than stacking a second one, so a redundant adopt is harmless.
   - WORKING — leave it alone. If the user should get their answer now, hand the whole still-working set — MINUS any terminal you just adopted above, whose new supervisor is already its one mode — to ONE `terminal.await.async({"terminalIds":[...],"title":"fleet sweep"})`, report what is running, and end the turn. Bound re-awaits as the foundation requires; a terminal that outlasts that bound is hung — `queue.publish` it at severity "blocked" (source, severity, title, summary are all required) and move on.

5. Cross-check the supervision that has no visible tab — a fleet is bigger than its terminals. All read-only, all no-argument unless shown: `watcher.list({})` (who is watched — and therefore which running terminals are UNSUPERVISED), `async.list({})` (live async waits and runs), `timer.list({})` (scheduled wakes), `queue.digest({})` (what is already flagged for the user), `workflow.list({"status":"active"})` (ledger entries still open), and `agentTask.list({})` — this session's spawn sagas, and the ONLY way to see a launch that FAILED before it ever bound a terminal, so the roster alone under-reports. Pass scope:"all" only for older history; chase one launch with `agentTask.status({"launchId":"agt_…"})`. Read these to answer "what is running" — never to poll for a completion, which wakes you on its own.

6. "Show me" is navigation, not a read. Focus is per-terminal and you choose the target: `terminal.focus({"terminalId":"<id>"})` reveals one terminal. "The next one needing input" is not its own call — pick that terminal out of the roster you already read (agentState "waiting", waitingReason "question" or "approval") and focus it by id. Focus answers no question, sends no input, and changes no state, so pair it with the summary you already have rather than issuing it alone.

7. Arming routes the HUMAN's keystrokes, not yours. Daintree keeps a fleet arming set: whatever the user types as broadcast input is typed into EVERY armed terminal. `terminal.arm({"terminalId":"<id>"})` adds one, `terminal.disarm({"terminalId":"<id>"})` removes one, `terminal.disarmAll({})` clears the set. Arming N terminals is N separate arm calls batched in one reply — there is no arm-all and no arm-by-state on the tool surface. All three reroute the user's input, so all three CONFIRM, and all three report the resulting armed set: quote that set back verbatim, because there is no list-armed tool and the call's own result is the only record. When the user has lost track of what is armed, `terminal.disarmAll({})` is the route to a known state (empty), then arm what they name. Never arm a terminal the user did not name.

8. If every tab reads alike, the sweep report will too — retitle them with the rename-terminal-tabs runbook first, as a step before the report rather than instead of it.

Report back: one line per agent — id, short title, class (finished / working / waiting / failed / unsupervised), and for finished ones the outcome in a sentence. Then what you acted on (answers routed, terminals adopted, inbox items resolved, terminals armed plus the resulting armed set), what is still running and what will wake you, and the short list of things that genuinely need the user.
