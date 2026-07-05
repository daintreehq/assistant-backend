---
id: daintree.orchestration.tie-breaker-runoff
title: Resolve a tied multi-agent vote
summary: Run a focused tie-breaker round for an existing multi-agent competition by collecting new entries from tied agents, polling the original voters, and tallying the runoff cleanly.
whenToUse: Use when a multi-agent competition, poll, or peer-vote ends in a tie and the user asks for a tie-breaking or runoff round using the same agents.
priority: 100
risk: terminal
tags:
  - daintree
  - orchestration
  - multi-agent
  - competition
  - tie-breaker
  - runoff
  - voting
requiredTools:
  - context.snapshot
  - scratch.create
  - scratch.get
  - scratch.set
  - scratch.drop
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.extract.json
  - terminal.read
  - terminal.summarize
---
Use when: an existing multi-agent competition or peer-vote produced a tie, and the user asks you to break the tie with a runoff using the already-open agents.

Procedure:
1. Recover the competition state: finalist agent names, their exact full `terminalId`s, all original voter terminalIds, prior entries, and prior scores. Use `scratch.get` if a store exists; otherwise reconstruct from the transcript and `context.snapshot`. Copy terminalIds exactly and in full.
2. Set the runoff rule. Default: ask only the tied finalists for one NEW short independent entry, then ask all original voters to choose ONE finalist entry. Use the user's stated rules if different. Do not spawn new agents for a tie-breaker unless the user explicitly changes the electorate.
3. Prompt the tied finalists with `terminal.sendCommand`, one command per finalist, all in one batch. Ask for one new entry, brief, unique, quick, and different from its earlier entry; include any user constraints such as “not related to the codebase.” Add a trailing `terminal.awaitAll({"terminalIds":[...finalistIds...]})` as the LAST call in the same batch — sendCommands plus one await, not a separate round-trip.
4. Handle finalist completion. If `awaitAll` reports `askingQuestion`, read that terminal tail with `terminal.read` before replying; Daintree may label an already-finished TUI prompt as a question. If it already answered, proceed. If genuinely asking, send a short clarification and re-await. Re-await `stillWorking` finalists at most twice, then omit or ask the user rather than blocking forever.
5. Extract the new finalist entries in ONE `terminal.extract.json` over the finalist terminalIds. Use a real JSON Schema, e.g. object with `entries` array of objects containing `terminalId`, `agentName`, and `entry`. If the result is sparse or misses content, fall back to `terminal.read` or `terminal.summarize`; extract tools only see the visible tail.
6. Save compact runoff state with `scratch.set`: finalist ids/names, voter ids/names, new entries, and any prior scores needed for a final report. Store compact JSON, not raw scrollback.
7. Build the voting prompt: list ONLY the runoff entries, labeled by finalist name, and ask each voter to choose exactly one finalist with a one-line reason: `AgentName: reason`. Send it to every original voter terminal with `terminal.sendCommand` in one batch, marking the voter’s own entry as `(YOU)` if self-votes are allowed. Add one trailing `terminal.awaitAll({"terminalIds":[...voterIds...]})` as the LAST call in that same batch.
8. Extract all votes in ONE `terminal.extract.json` over voter terminalIds, with schema objects containing `terminalId`, `votedFor`, and `reason`. Map each `terminalId` back to the voter name. Validate `votedFor` is one of the tied finalists; for an invalid vote, re-prompt that voter once or omit it and state that clearly.
9. Tally only the runoff votes to determine the tie-break winner. If the runoff is still tied, ask the user whether to run another round or use a specified sudden-death rule; do not invent a winner.
10. If you also report cumulative totals, compute them mechanically from stored prior scores plus the runoff votes and label them separately. Do not invent or hand-wave a combined leaderboard; the runoff result alone is usually enough.
11. Leave terminals open unless the user explicitly asks to close them. Drop the scratch store only when the competition is finished and no further runoff/follow-up is likely.

Report back: the new finalist entries, each voter’s choice and reason, the runoff tally, the winner, and any omitted/invalid/hung votes. If showing cumulative totals, clearly separate them from the runoff tally.
