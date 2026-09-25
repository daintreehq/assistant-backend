---
id: daintree.orchestration.runoff-tiebreaker
title: Run a runoff tie-breaker for an agent competition
summary: Break a tie in an agent voting competition by getting new submissions from tied entrants, polling the original cohort, and tallying the runoff accurately.
whenToUse: Use when an agent competition, poll, or peer-vote ends in a tie and the user asks for a tie-breaking round or runoff using the same cohort. Do not use to start a fresh competition or poll (use daintree.orchestration.competition or daintree.agents.poll-pinned-agents), and do not spawn new agents unless the user explicitly changes the electorate.
risk: project
requiredTools:
  - context.snapshot
  - scratch.create
  - scratch.set
  - scratch.get
  - scratch.drop
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.read
  - terminal.close
  - terminal.await.async
---
Use when: an agent competition, poll, or peer-vote has a tied top score and the user asks to break the tie with a runoff among the tied agents or tied submissions.

Procedure:
1. Restore the competition state. If you have a scratch store, `scratch.get` the saved state; if not, create one with `scratch.create({"name":"runoff-tiebreaker"})` and reconstruct from the transcript/current terminals. Build a compact map of full `terminalId` → entrant name, the tied entrants, all original voter terminalIds, prior submissions, and prior scores. Save it as one `runoff_state` key with `scratch.set`. Use full terminal IDs exactly; do not include unrelated open terminals unless the user explicitly named them.
2. Set the runoff rule before sending prompts. Reuse the existing cohort — do not spawn new agents for a tie-breaker unless the user explicitly changes the electorate. Default: ask ONLY the tied entrants for one new short submission, then ask ALL original voters to choose the best among those tied entrants. The runoff vote is BLIND, same as the original (step 5): a voter may unknowingly pick its own entry — a vote counted according to the stated self-vote rule — and you enforce a no-self-vote rule yourself via the map only if the user set one. If the user wants a runoff on the original tied submissions instead, skip the new-submission step.
3. Ask tied entrants for new submissions with `terminal.sendCommand`, one command per tied terminal, batched together. Prompt for one short, quick, unique, task-specific answer; if the original task excluded the project/codebase, restate that exclusion. End the same batch with `terminal.awaitAll({"terminalIds":[...],"maxAttempts":60})` for only the tied entrant IDs. Do not ask non-tied agents for new entries.
4. Collect the tied entries. If `awaitAll` reports an `askingQuestion` terminal, `terminal.read` it before re-prompting; a terminal may be classified as asking even when its answer is already visible. If the answer is visible, proceed. If it is genuinely blocked, read the actual question or approval prompt and answer only within existing authority. Do not send “choose anyway” to a permission/error screen; resolve the blocker or report the missing entry. Collect the entries in free-form — one `terminal.read` per tied terminal, batched in one turn. Each submission is the entrant's own prose; do not ask entrants for JSON or run a structured extraction. Map each entry to its entrant by terminalId.
5. Build a BLIND runoff ballot so the tie breaks on merit, not on who wrote what: the tied entries only, each stripped of its author and any characterization, given equal detail, and labeled with neutral tags (Entry 1/2 or A/B). VARY the tag order across voters (rotate or shuffle) and record each voter's tag→entrant map in scratch. Send it to every original voter terminal with `terminal.sendCommand`, asking for exactly ONE winner by its label plus a one-line reason; no rankings or abstentions. Do NOT reveal who wrote any entry or mark the voter's own as `(YOU)` — a voter may recognize or pick its own entry despite neutral labels; enforce a no-self-vote rule only if the rules set one, detecting it yourself via the map and quietly asking that voter for its next pick without disclosing authorship. Never silently drop the ballot — a discarded vote changes the denominator and can flip the runoff. Batch all vote prompts and put `terminal.awaitAll({"terminalIds":[all voter ids],"maxAttempts":60})` as the last call in that same batch.
6. Collect votes in free-form — one `terminal.read` per voter terminal, batched. Each vote should name exactly ONE tag plus a one-line reason; take it from the voter's own prose, map the chosen tag back to the entrant via that voter's scratch map, and normalize only to the tied entrants. If a vote is ambiguous or names an out-of-ballot label, re-read that terminal; if still unclear, send one clarification prompt and await only that voter.
7. Tally mechanically. Start runoff scores at zero and add one per valid vote. Report per-voter choices. If you also present a combined leaderboard, compute it from stored prior scores plus runoff votes in a small explicit pass; do not improvise combined totals from memory.
8. Declare the result. If one entrant leads the runoff, name the winner. If the runoff is still tied, do not invent a winner; ask the user whether to run another runoff, accept co-winners, or apply a stated secondary criterion. Update `runoff_state` with final scores and winner/tie.
9. Drop scratch when the runoff is complete; retain it while a next round or async result is pending. Close terminals only on request, using ONLY the exact recorded eligible cohort IDs with `terminal.close({"terminalIds":[...]})`. Never close unrelated pre-existing terminals as cleanup.

Confirmation: Closing terminals requires an explicit user request; after closing, confirm the count and scratch cleanup.

For a tie in a poll of externally supplied options, retain those tied OPTIONS and skip new entrant submissions: voters are not necessarily the authors. Restore only original voters still live and eligible; do not message a terminal removed or disqualified in the preceding round. Report the eligible/received/valid vote counts and any resulting change in electorate. Blinded labels reduce presentation bias but do not erase existing peers’ memory. Apply the foundation’s wait budget; keep runoff state while any async result or requested next round is pending, and drop scratch when the runoff is complete even if terminals remain open.

Report back: the runoff submissions, each voter’s choice and reason, the runoff tally, the winner or remaining tie/options, and any terminal closure or scratch cleanup performed.
