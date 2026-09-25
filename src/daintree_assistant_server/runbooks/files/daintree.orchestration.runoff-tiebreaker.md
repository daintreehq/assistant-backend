---
id: daintree.orchestration.runoff-tiebreaker
title: Run a runoff tie-breaker for an agent competition
summary: Break a tie in an agent voting competition by getting new submissions from tied entrants, polling the original cohort, and tallying the runoff accurately.
whenToUse: Use when an agent competition, poll, or peer-vote ends in a tie and the user asks for a tie-breaking round or runoff using the same cohort. Do not use to start a fresh competition or poll (use daintree.orchestration.competition or daintree.agents.poll-pinned-agents), and do not spawn new agents unless the user explicitly changes the electorate.
risk: project
requiredTools:
  - terminal.sendCommandOwned
  - terminal.getStatus
optionalTools:
  - terminal.list
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned
---
Use when: a competition, poll or peer vote ended with a tied top score and the user wants a runoff with the same cohort.

Availability: Core. An API-key client has no `notify`: close each phase with one `terminal.waitUntilIdleBatch` (`mode:"all"`).

Procedure:
1. Restore the contest from this conversation (or its checkpoint on a long run): rules, scoring and missing-vote rule, entrants and terminal ids, the tied entrants, the original electorate, prior scores, and anyone removed. Say which parts you reconstructed.
2. Revalidate the electorate with one `terminal.getStatus` over every original voter. Reuse only those still live and yours or handed over. An original voter gone, unreachable or earlier removed stays explicitly absent: never replaced by a fresh agent and never counted. New voters only if the user changes the electorate.
3. Rules: inherit the original scoring, self-vote and missing-vote rules unless the user changes them. Default format: one new short entry from each tied entrant, then every eligible voter picks among those. With the original submissions reused, or a tie between options you supplied (a poll), skip new entries. A further tie is broken only by a rule agreed in advance.
4. New entries: send to each tied entrant only, in one turn, restating the task and its exclusions, then end the turn.

```json tool=terminal.sendCommandOwned
{"terminalId":"<tied entrant terminal id>","command":"Runoff: new entry. Task: <original task, with its exclusions>. Give ONE new short <entry>, different from your earlier one, in at most a few lines. Do not contact other agents.","notify":true,"handback":true}
```

5. Runoff ballot: once each new entry is in or recorded missing, give every eligible voter a fresh ballot of only the tied entries under new neutral labels, a new order and a new private map (never the original round's), asking for exactly one label and a one-line reason. Send all in one turn with `notify` and `handback`, then end it.

On notice or resume:
1. Each quote is that terminal's runoff entry or vote. A runoff vote counts only from an eligible voter whose runoff ballot is pending; a reply to the original ballot is stale, and an old label is never mapped through the runoff map.
2. A dialog is handled as the foundation says; otherwise that entry or vote stays missing.
3. Map each vote through that voter's runoff map, accepting only the tied entries. An ambiguous answer gets one clarification, then is invalid. A voter that cannot choose is recorded as abstaining under the rules; never force a vote. Never silently drop a ballot: it changes the denominator.
4. Tally the runoff from zero under the inherited scoring. Keep runoff and cumulative scores apart; show a combined leaderboard only labelled as combined.
5. One leader wins. Still tied: report it, or apply only a tie-break agreed in advance. A result a missing vote could change is provisional and crowns no one.

Stop and recovery: one runoff per request. Never resend a prompt already pending. Close only on request, the recorded cohort ids, confirmed from `closedIds`. On cancellation a late vote changes nothing.

Report back: the runoff entries; each voter's de-blinded choice and reason; eligible, received and valid counts, naming absent voters; the runoff tally (and a labelled combined one if shown); the winner or the remaining tie and the options put to the user; any cleanup.
