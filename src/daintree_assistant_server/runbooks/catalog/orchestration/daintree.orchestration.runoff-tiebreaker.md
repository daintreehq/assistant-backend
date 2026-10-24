---
id: daintree.orchestration.runoff-tiebreaker
title: Run a runoff tie-breaker for an agent competition
summary: Break a tie in an agent voting competition by getting new submissions from tied entrants, polling the original cohort, and tallying the runoff accurately.
whenToUse: Use when an agent competition, poll, or peer-vote ends in a tie and the user asks for a tie-breaking round or runoff using the same cohort. Do not use to start a fresh competition or poll (use daintree.orchestration.competition or daintree.agents.poll-pinned-agents), and do not spawn new agents unless the user explicitly changes the electorate.
risk: project
requiredTools:
  - terminal.sendCommandMany
  - terminal.getStatus
optionalTools:
  - terminal.list
  - terminal.closeMany
---
On a host: call these tools on each terminal's machine.

1. Restore the contest from this conversation (or its checkpoint): rules, scoring and missing-vote rule, entrants with terminal ids, the tied entrants, the original electorate, prior scores, anyone removed. Say which parts you reconstructed.
2. Revalidate the electorate with one `terminal.getStatus` over every original voter. Reuse only those still live and yours or handed over. An original voter gone, unreachable or earlier removed stays explicitly absent: never replaced by a fresh agent and never counted. New voters only if the user changes the electorate.
3. Inherit the original scoring, self-vote and missing-vote rules unless the user changes them. Default: one new short entry from each tied entrant, then every eligible voter picks among those. With the original submissions reused, or a tie between options you supplied, skip new entries.
4. New entries: one `terminal.sendCommandMany` to the tied entrants only, with `waitForReply:true, handback:true`: "Runoff: new entry. Task: <original task, with its exclusions>. Give ONE new short <entry>, different from your earlier one, in at most a few lines. Do not contact other agents."
5. Runoff ballot: every eligible voter gets only the tied entries under new neutral labels, a new order and a new private map (never the original round's), asking for exactly one label and a one-line reason; one `terminal.sendCommandMany` with `waitForReply:true, handback:true`.
6. A reply to the original ballot is stale, and an old label is never mapped through the runoff map. Accept only the tied entries. An ambiguous answer gets one clarification, then is invalid. A voter that cannot choose abstains under the rules; never force a vote, and never silently drop a ballot: it changes the denominator.
7. Tally the runoff from zero under the inherited scoring; show a combined leaderboard only labelled as combined. One leader wins. Still tied: apply the tie-break the user already gave (anything it needs, such as a runner-up, asked on this ballot and mapped through this round's map) before reporting; with none, or still tied, report the tie and ask. A result a missing vote could change is provisional and crowns no one.

One runoff per request. Once a winner is reported, close the voters and entrants this contest launched, per the foundation, in one `terminal.closeMany`, confirmed from `closedIds`, keeping a winning entrant the user may work with next. Still tied or provisional: close none; the next round needs them.

Report back: the runoff entries; each voter's de-blinded choice and reason; eligible, received and valid counts, naming absent voters; the runoff tally (and a labelled combined one if shown); the winner or the remaining tie and the options put to the user; any cleanup.
