---
id: daintree.orchestration.supervise-fleet
title: Supervise a running agent fleet
summary: Triage every agent already running — finished, working, waiting, failed, unsupervised — in one state-only sweep, act on each class, and report what still needs the user.
whenToUse: Use when agents are ALREADY running and the user wants the fleet swept and acted on — "how's everyone doing?", "anything need me?", "is anyone stuck?", "catch me up on the agents". Also for adopting a running-but-unsupervised agent, "show me the next one" navigation, and reporting which terminals are armed for the user's typed input. Do not use for a plain state read with no action (daintree.foundation), for answering agents that are ASKING (daintree.agents.answer-waiting-agent), for rescuing one broken or hung agent (daintree.agents.recover-agent), for handing out new work (daintree.orchestration.fan-out-work), for "where were we" across sessions (daintree.orchestration.status-and-resume), for proving one agent's work landed (daintree.orchestration.verify-agent-work), for watching past this turn (daintree.orchestration.watch-outcome), or for retitling tabs (daintree.terminals.rename).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.revealOwned
  - fleet.getRunStatus
---
Use when: agents are already running and the user wants the fleet swept, classified and, where they ask, acted on.

This is a synchronous, read-only sweep: no launch, no notice, no checkpoint. A status request authorizes reading only; launching, answering, interrupting, closing, arming notices or moving the user's view each needs the user to ask.

Availability: Core. `fleet.getRunStatus`, for a broadcast the user runs from Daintree, raises an approval in a core pane.

Procedure:
1. Scope: the agents or worktree the user named, otherwise every agent terminal in the project from `terminal.list` (add `location:"background"` only if agents live there). Your own run records say what each terminal was asked to do.
2. Read the cohort in one batched call, paging past 256 ids:

```json tool=terminal.getStatus
{"terminalIds":["<id 1>","<id 2>","<id 3>"],"includeOutput":{"lines":30}}
```

   Only where 50 tail lines are not enough, `terminal.readLastMessageOwned` for a Claude Code pane you own.
3. Classify each terminal from its own entry:
   - `working` or `directing`: reported working. An old `lastOutputChangeAt` is "possibly stalled", not a hang verdict.
   - `waiting` with `waitingReason` `question`, `approval` or `error`: needs someone; quote the prompt or error.
   - At its prompt with a coherent final message or a `lastHandback`: a candidate completion, unverified.
   - `idle` before any turn: given no work, or its prompt never landed.
   - Exited: report `exitCode` as returned; success is unproven.
   - Unknown: an entry `error`, a reduced reading with `unavailableFields`, or a terminal missing from a failed or filtered listing. Missing from a filter is not exited.
   Never let one terminal's state stand for another's. Do not call a terminal "unsupervised" because your records hold no notice for it: another pane may be waiting on it, and notices cannot be listed.
4. Act only on what the user asked, each through its own runbook: questions and approvals (daintree.agents.answer-waiting-agent), a broken agent (daintree.agents.recover-agent), proof of work (daintree.orchestration.verify-agent-work), being told when one finishes (daintree.orchestration.watch-outcome).
5. Navigation only on request: `terminal.revealOwned` for a panel you created or were handed; otherwise give its title and worktree. Broadcast arming is read from each entry's `armed`; changing it is the user's.

Report back: one line per agent (title, terminal id, worktree, observed state with its evidence, outcome or blocker), what needs the user and why, any action taken at their request, and what remains unknown.
