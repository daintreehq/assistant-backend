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
  - terminal.revealOwned
  - terminal.arm
  - terminal.disarm
  - terminal.disarmAll
---
Use when: agents are already running and the user wants a fleet status sweep or specific unblocking actions.

Report observed state and agent outcomes; delegate engineering review. A status request does not authorize starting new work, renaming every tab, closing terminals or rerouting the user's keyboard.

Procedure:
1. Start with the fresh injected roster, scoped to the agents/worktree the user named. Call `context.snapshot` only for missing detail, ambiguous ids or revalidation before a mutation. A partial/unavailable roster is not evidence that a terminal is gone.
2. Read existing supervision BEFORE adopting or waiting: `watcher.list`, `async.list`, `timer.list`, `queue.digest`, `workflow.list({"status":"active"})`, and `agentTask.list` for launches that failed before binding a terminal. Use `scope:"all"` for earlier sessions. These are status reads, not a completion-poll loop. Account for list limits and scope.
3. Classify from the roster and bounded output: working/directing; waiting on question/approval; stopped on error; settled with completion evidence; exited/failed; or unknown. Separately mark whether a watcher/async handle covers the task. Unsupervised is an attribute, not proof of failure or unfinished work. Missing exitCode is unknown, never zero. A `working` roster state alone does not prove healthy progress or absence of blockers; say “reported working” when no output evidence establishes more.
4. Keep a passive sweep passive. Do NOT call `terminal.awaitAll` merely to classify a watched fleet: it retires spawn supervisors for members it settles finished/failed. If the user explicitly wants you to wait and take over completion reporting, use it for that cohort only (at most 16 unique ids, shared 120-second budget). Inspect `watchersRetired`, read those tails yourself, and expect no later notification from retired supervisors. Working/question members can retain watchers; exclude them from a new async wait unless deliberately transferring supervision.
5. Read results with one bounded per-terminal read or `terminal.summarize` per relevant agent, batching independent calls. No-wait multi-id text extraction merges outputs; it cannot assign one result to each agent. If a settled tail still shows work or an approval/error prompt, classify from that evidence and restore one suitable supervision mode if your wait retired it.
6. Act only on the requested classes:
   - Completed: relay the actual result and any test/verification limits. Update the matching flat workflow row after evidence, then resolve an inbox item you have reported. For proof of correctness, use verify-agent-work.
   - Question/approval: read the exact prompt and use answer-waiting-agent. Existing authority can answer routine scope questions; collect decisions still needing the user into one request.
   - Error/failed: report the safe error and terminal/launch id. Use recover-agent for an authorized recovery; never relaunch a fleet because one member failed.
   - Working and supervised: leave its current supervisor in place.
   - Working and unsupervised: when continued supervision is requested, adopt with `agentTask.superviseTerminal`, supplying mode, worktree and acceptance criteria, OR create one `terminal.await.async` over uncovered terminals for plain finish detection. Do not attach either to a shell that failed startup. Confirm a returned or listed watcher id before promising a wake.
7. Navigation is separate, and the two calls are not interchangeable: `terminal.revealOwned({"terminalId":"<id>"})` requests a workspace switch and window raise for a terminal this MCP connection created, while `terminal.focus({"terminalId":"<id>"})` only selects a terminal inside this session's project view, including one you do not own. Follow the foundation's navigation and ownership rules. Neither answers the agent. For requested tab titles, use the rename runbook; similar titles alone are not permission to retitle unrelated work.
8. Arming changes where the HUMAN'S broadcast input goes. Use `terminal.arm` / `terminal.disarm` for each requested id, or `terminal.disarmAll` only when the whole set is in scope. These calls confirm and return the resulting armed set. Report the actual set, not the intended one; there is no list-armed tool. Never clear the whole broadcast set merely to discover it.
9. Report promptly. A running task with no observed progress is uncertain, not automatically hung. If all requested work is now under existing or newly established supervision, end the turn and respond to its event rather than polling.

Report back: one line per relevant agent with title, exact id, observed status, outcome/blocker and supervision. Include actions actually taken, launch failures with no tab, and the next supported wake or user decision.
