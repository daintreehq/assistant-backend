---
id: daintree.orchestration.runoff-tiebreaker
title: Run a runoff tie-breaker for an agent competition
summary: Break a tie in an agent voting competition by getting new submissions from
  tied entrants, polling the original cohort, and tallying the runoff accurately.
whenToUse: Use when an agent competition, poll, or peer-vote ends in a tie and the
  user asks for a tie-breaking round or runoff.
priority: 100
risk: project
tags:
  - daintree
  - orchestration
  - tie-breaker
  - runoff
  - competition
  - voting
  - multi-agent
requiredTools:
  - context.snapshot
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.extract.json
  - terminal.read
  - terminal.close
---
Use when: an agent competition, poll, or peer-vote has a tied top score and the user asks to break the tie with a runoff among the tied agents or tied submissions.

Procedure:
1. Restore the competition state. If you have a scratch store, `scratch.get` the saved state; if not, create one with `scratch.create({"name":"runoff-tiebreaker"})` and reconstruct from the transcript/current terminals. Build a compact map of full `terminalId` → entrant name, the tied entrants, all original voter terminalIds, prior submissions, and prior scores. Save it as one `runoff_state` key with `scratch.set`. Use full terminal IDs exactly; do not include unrelated open terminals unless the user explicitly named them.
2. Set the runoff rule before sending prompts. Default: ask ONLY the tied entrants for one new short submission, then ask ALL original voters to choose the best among those tied entrants. Self-votes count unless the user forbade them. If the user wants a runoff on the original tied submissions instead, skip the new-submission step.
3. Ask tied entrants for new submissions with `terminal.sendCommand`, one command per tied terminal, batched together. Prompt for one short, quick, unique, task-specific answer; if the original task excluded the project/codebase, restate that exclusion. End the same batch with `terminal.awaitAll({"terminalIds":[...],"maxAttempts":60})` for only the tied entrant IDs. Do not ask non-tied agents for new entries.
4. Collect the tied entries. If `awaitAll` reports an `askingQuestion` terminal, `terminal.read` it before re-prompting; a terminal may be classified as asking even when its answer is already visible. If the answer is visible, proceed. If it is genuinely waiting, send one concise instruction such as “Please choose/answer anyway” and await only that terminal. Extract the entries with ONE `terminal.extract.json` over the tied terminalIds using a real JSON Schema that includes `terminalId`, entrant name, and submission. Do not pass an example object as the schema. If extraction is sparse or missing earlier content, use `terminal.read` for the full answer.
5. Build the runoff ballot: a concise labeled list of the tied entries only. Send it to every original voter terminal with `terminal.sendCommand`, asking for exactly ONE winner from the tied names plus a one-line reason; no rankings or abstentions. Mark the voter’s own entry as `(YOU)` when applicable, but do not discard self-votes unless the rules say so. Batch all vote prompts and put `terminal.awaitAll({"terminalIds":[all voter ids],"maxAttempts":60})` as the last call in that same batch.
6. Collect votes with ONE `terminal.extract.json` over all voter terminalIds. Use a schema like `{"type":"object","properties":{"votes":{"type":"array","items":{"type":"object","properties":{"terminalId":{"type":"string"},"votedFor":{"type":"string"},"reason":{"type":"string"}},"required":["terminalId","votedFor","reason"]}}},"required":["votes"]}`. Normalize `votedFor` only to tied entrant names. If a vote is ambiguous or names an out-of-ballot entrant, read that terminal; if still unclear, send one clarification prompt and await only that voter.
7. Tally mechanically. Start runoff scores at zero and add one per valid vote. Report per-voter choices. If you also present a combined leaderboard, compute it from stored prior scores plus runoff votes in a small explicit pass; do not improvise combined totals from memory.
8. Declare the result. If one entrant leads the runoff, name the winner. If the runoff is still tied, do not invent a winner; ask the user whether to run another runoff, accept co-winners, or apply a stated secondary criterion. Update `runoff_state` with final scores and winner/tie.
9. Cleanup only on request. If the user asks to close the competition terminals, close ONLY the exact recorded cohort IDs with `terminal.close({"terminalIds":[...]})`, then `scratch.drop` the scratch store. Never close unrelated pre-existing terminals as cleanup.

Confirmation: Closing terminals requires an explicit user request; after closing, confirm the count and scratch cleanup.

Report back: the runoff submissions, each voter’s choice and reason, the runoff tally, the winner or remaining tie/options, and any terminal closure or scratch cleanup performed.
