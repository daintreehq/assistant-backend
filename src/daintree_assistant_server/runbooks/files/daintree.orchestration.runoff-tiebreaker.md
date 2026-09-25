---
id: daintree.orchestration.runoff-tiebreaker
title: Run a runoff tie-breaker for an agent competition
summary: Break a tie in an agent voting competition by getting new submissions from tied entrants, polling the original cohort, and tallying the runoff accurately.
whenToUse: Use when an agent competition, poll, or peer-vote ends in a tie and the user asks for a tie-breaking round or runoff using the same cohort. Do not use to start a fresh competition or poll (use daintree.orchestration.competition or daintree.agents.poll-pinned-agents), and do not spawn new agents unless the user explicitly changes the electorate.
risk: project
requiredTools:
  - terminal.sendCommandOwned
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listAvailable
  - agent.launch
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdleBatch
  - terminal.sendCommand
  - terminal.closeOwned
  - terminal.close
---
Use when: a competition, poll or peer vote ended with a tied top score and the user wants a runoff with the same cohort.

Availability: Core. In an agent pane, `notify` on each send brings every runoff entry and vote back as it lands. An API-key client has no `notify`: close each phase with one bounded `terminal.waitUntilIdleBatch` (`mode:"all"`) and tell the user what remains pending for their continuation. The sidebar assistant uses `terminal.sendCommand` and `terminal.close`.

Ballots follow the foundation's "Judging even-handedly"; below is what a runoff adds.

Procedure:
1. Restore the contest from its checkpoint, not from memory alone. daintree.orchestration.competition hands it over intact: rules, entrants and terminal ids, the tied entrants, the original electorate (every voter's terminal id), prior entries and scores, and anyone removed or disqualified. With no checkpoint, reconstruct from this conversation and the current terminals, write a new one, and tell the user which parts were reconstructed.
2. Revalidate the electorate with one `terminal.getStatus` over every original voter. Reuse only participants still live and still yours or handed over. An original voter that is gone, not reachable by your owned send, or removed or disqualified in the earlier round stays explicitly absent: never replaced by a fresh agent and never counted. Launch new voters only if the user explicitly changes the electorate.
3. Fix the runoff rule and record it. Default: one new short entry from each tied entrant only, then every eligible original voter picks among those. If the user wants the original tied submissions, skip new entries. A tie between options you supplied (a poll) keeps those options and asks for no new entries: voters are not their authors. The self-vote rule carries over unless the user changes it. A further tie is broken only by a rule agreed in advance.
4. Open a runoff generation in the checkpoint: its id, runoff scores reset to zero and kept apart from the original scores, and per participant its pending runoff prompt and `submissionToken`.
5. New entries: send to each tied entrant, and nobody else, only while it sits at its prompt. Restate the original task and any exclusion it carried (for example "do not use the codebase"):

```json tool=terminal.sendCommandOwned
{"terminalId":"<tied entrant terminal id>","command":"Runoff <runoff id>: new entry. Task: <original task, with its exclusions>. Give ONE new short <entry>, different from your earlier one, here. Do not contact other agents.","notify":true,"handback":true}
```

   Record tokens and end your turn.
6. Runoff ballot: once each tied entrant's new entry is in or recorded missing, build every eligible voter a fresh ballot of only the tied entries, with a new tag rotation and a new private map for this runoff (never reuse the original round's map). Ask for exactly one label and a one-line reason; no rankings, no abstentions. Send with `notify` and `handback`, record each token, and end your turn.

On notice or resume:
1. Follow the foundation's resume procedure, for each terminal the notice names.
2. Correlate: a runoff entry counts only from a tied entrant whose runoff-entry prompt is pending; a runoff vote only from an eligible voter whose runoff ballot is pending and settled after that send (`terminal.getStatus` with its `submissionToken`; a matching `lastHandback.submissionToken` confirms). A reply to the original ballot, whether late or re-read, is stale and never a runoff vote, and an old label is never mapped through the runoff map.
3. A terminal reported as waiting may already show its answer: read it before re-prompting. A genuine approval, trust or error screen is never answered with "choose anyway"; take it to the user or record that entry or vote missing.
4. Read each vote in the voter's own words (`terminal.readLastMessageOwned` for Claude Code, otherwise `terminal.getOutput`), map it through that voter's runoff map, and accept only the tied entries. An ambiguous or off-ballot answer gets one clarification to that voter alone; still unclear, it is invalid. Enforce a no-self-vote rule only if the rules set one, by quietly asking that voter for its next pick. Never silently drop a ballot: a discarded vote changes the denominator and can flip the result.
5. Tally the runoff from zero, one point per valid vote. Show a combined leaderboard only if it helps, computed explicitly from the stored prior scores plus the runoff votes and labelled as combined.
6. One leader wins. Still tied: report the tie, or apply only the tie-break agreed in advance; otherwise the user chooses another runoff, co-winners or a stated secondary criterion. Record the final state.

Stop and recovery: one runoff per request. Never resend a prompt to a participant already pending; check its token first. An unavailable original voter stays absent, never substituted. Close terminals only on request, with `terminal.closeOwned` on exact recorded cohort ids, confirmed from `closedIds`; never close unrelated terminals as cleanup. On cancellation keep a tombstone so a late vote cannot change the recorded result.

Report back: the runoff entries, each voter's de-blinded choice and reason, eligible, received and valid counts with any absent original voter named, the runoff tally (and the combined leaderboard, labelled, if shown), the winner or the remaining tie and the options put to the user, and any cleanup performed.
