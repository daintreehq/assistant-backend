---
id: daintree.orchestration.supervise-fleet
title: Supervise a running agent fleet
summary: Triage every agent already running — finished, working, waiting, failed, unsupervised — in one state-only sweep, act on each class, and report what still needs the user.
whenToUse: Use when agents are ALREADY running and the user wants the fleet swept and acted on — "how's everyone doing?", "anything need me?", "is anyone stuck?", "catch me up on the agents". Also for adopting a running-but-unsupervised agent, "show me the next one" navigation, and reporting which terminals are armed for the user's typed input. Do not use for a plain state read with no action (daintree.foundation), for answering agents that are ASKING (daintree.agents.answer-waiting-agent), for rescuing one broken or hung agent (daintree.agents.recover-agent), for handing out new work (daintree.orchestration.fan-out-work), for "where were we" across sessions (daintree.orchestration.status-and-resume), for proving one agent's work landed (daintree.orchestration.verify-agent-work), for watching past this turn (daintree.orchestration.watch-outcome), for retitling tabs (daintree.terminals.rename), for one question to one existing agent (daintree.agents.ask-running-agent), for a written account of what agents landed over a period (daintree.git.summarize-activity), for bringing running agents to a stopping point for the day (daintree.orchestration.park-work), for pausing or redirecting agents because other work turned urgent (daintree.orchestration.reprioritize-work), or for relieving a machine the agents have overloaded (daintree.runtime.relieve-resource-contention).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.revealOwned
  - fleet.getRunStatus
---
A synchronous, read-only sweep. A status request authorizes reading only; launching, answering, interrupting, closing, arming notices or moving the user's view each needs the user to ask.

1. Scope: the agents or worktree the user named, otherwise every agent terminal from `terminal.list` (`location:"background"` only if agents live there). `fleet.getRunStatus` covers a broadcast the user ran.
2. Read the cohort in one `terminal.getStatus` (`includeOutput: {"lines":30}`), paging past 256 ids; `terminal.readLastMessageOwned` only where the tail is not enough.
3. Classify each from its own entry:
   - `working` or `directing`: working. An old `lastOutputChangeAt` is "possibly stalled", not hung.
   - `waiting` with `waitingReason` `question`, `approval` or `error`: needs someone; quote it.
   - At its prompt with a coherent final message or `lastHandback`: a candidate completion, unverified.
   - `idle` before any turn: given no work, or its prompt never landed.
   - Exited: report `exitCode`; success is unproven.
   - Unknown: an entry `error`, `unavailableFields`, or missing from a failed or filtered listing (not exited).
   Do not call a terminal "unsupervised" because your records hold no notice for it: another pane may be waiting on it.
4. Act only on what the user asked, through its runbook: daintree.agents.answer-waiting-agent, daintree.agents.recover-agent, daintree.orchestration.verify-agent-work, daintree.orchestration.watch-outcome.
5. Navigation only on request: `terminal.revealOwned` for your panels, otherwise give title and worktree. Broadcast arming is each entry's `armed`; changing it is the user's.

Report back: one line per agent (title, terminal id, worktree, state with its evidence, outcome or blocker), what needs the user and why, any action taken at their request, and what remains unknown.
