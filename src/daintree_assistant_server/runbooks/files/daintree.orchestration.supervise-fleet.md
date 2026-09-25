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
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.revealOwned
  - fleet.getRunStatus
---
Use when: agents are already running and the user wants the fleet swept, classified and, where they ask, acted on.

This is a synchronous sweep: no launch and no checkpoint of its own. A status request authorizes reading. It does not authorize launching, renaming, answering, interrupting, closing, arranging notices or moving the user's view; each of those needs the user to ask for it.

Availability: Core. `fleet.getRunStatus`, for a broadcast the user is running from Daintree, raises an approval in a core pane.

Procedure:
1. Scope: the agents or worktree the user named, otherwise every agent terminal in the project. `terminal.list` gives ids, titles, worktrees and `agentState`; ask for `location:"background"` only if agents live there. Add any run checkpoints of yours (`.daintree/runbook-runs/`) for what each terminal was asked to do.
2. Read the cohort in one batched call, not one read per terminal:

```json tool=terminal.getStatus
{"terminalIds":["<id 1>","<id 2>","<id 3>"],"includeOutput":{"lines":30}}
```

   Use `terminal.getOutput` (or `terminal.readLastMessageOwned` for a Claude Code pane you own) only where a tail was not enough.
3. Classify each terminal from its own entry and output:
   - `working` or `directing`: "reported working" unless the output shows progress. An old `lastOutputChangeAt` is "possibly stalled", not a hang verdict.
   - `waiting` with `waitingReason` `question`, `approval` or `error`: needs someone. Quote the actual prompt or error from the output.
   - `waiting` at its prompt with a coherent final message or a `lastHandback`: a candidate completion, unverified.
   - `idle` before any turn: started but given no work, or its prompt never landed.
   - Exited: report `exitCode` (null means killed by a signal). Success is still unproven.
   - Unknown: an entry with `error`, a reduced `source:"pty"` reading with `unavailableFields`, or a terminal missing from a listing that failed or was filtered. Unknown is neither gone nor done.
   A mixed fleet gets a mixed report; never let one terminal's state stand for another's. Do not call a terminal "unsupervised" because your records hold no notice for it: another pane may be waiting on it, and notices cannot be listed.
4. Act only on the classes the user asked about, each through its own runbook: questions and approvals through daintree.agents.answer-waiting-agent, a broken or hung agent through daintree.agents.recover-agent, proof that work landed through daintree.orchestration.verify-agent-work, and "tell me when it finishes" (adopting an agent nobody is waiting on) through daintree.orchestration.watch-outcome. Never relaunch the fleet because one member failed.
5. Navigation is separate and only on request. `terminal.revealOwned` switches the workspace and raises the window for a panel you created or were handed; for any other panel, give the user its title and worktree. Broadcast arming is read from each entry's `armed` field; changing it is the user's, in Daintree. Retitling is daintree.terminals.rename.

Report back: one line per agent (title, terminal id, worktree, observed state and the evidence for it, outcome or blocker), what needs the user and why, any action taken because the user asked, and what remains unknown.
