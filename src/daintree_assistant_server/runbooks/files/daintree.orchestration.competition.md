---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: 'Run a scored contest among several named agents: ask each for one independent submission, relay all submissions for blind peer votes, tally the points, and declare the winner or tie — you run the ballot, the agents (or a neutral judge agent) supply the verdict.'
whenToUse: 'Use when the user names several agents (or the fleet) and asks each to produce one independent submission — an interesting fact, a design idea, a quick answer — and then wants them scored: award points, send each agent the others'' submissions, ask each to pick the best, tally, declare a winner. The signal is two-stage: the agents'' OWN submissions become the ballot they peer-judge. A vote over options YOU supplied, even one scored and tallied, is a fleet poll, not a competition. Do not use when each agent instead gets its own unit of real work (daintree.orchestration.fan-out-work), for a judge-led interview (interview runbook), a fleet poll with no scoring (poll-pinned-agents), collaborative debate toward one synthesized answer (multi-agent), a debate that ends by voting a position off (debate-elimination), or a runoff after a tie (runoff-tiebreaker).'
risk: project
requiredTools:
  - agent.launchMany
  - terminal.sendCommandMany
optionalTools:
  - agent.launch
  - terminal.getStatus
  - terminal.closeMany
---
1. Fix the rules before launching: the entry task; the judge (peer vote by default); the scoring rule exactly as the user gave it; whether voters may pick their own entry (default no, and ballots then omit it); the tie rule (default: report the tie). Without an agreed judge you never score entries on merit yourself.
2. Entries: one fresh entrant per agent named ("Claude, Codex and Gemini" means new launches, not open terminals), all in one worktree, in one `agent.launchMany` with `waitForReply:true, handback:true` and the prompt "<task>. Reply with ONE <entry> of at most a few lines, in your own words. Do not contact other agents. Change no files." Track per entrant: terminal id, status (active, removed, missing) and entry.
3. Ballots, once every active entrant has an entry or is recorded missing. Entries lock when the first ballot is sent: a later entry is late and changes no issued ballot. Give each voter the eligible entries in one identical format under neutral labels, with no author, agent kind or "(you)", in a different order per voter; keep each voter's label-to-entrant map to yourself. Send all ballots in one `terminal.sendCommandMany` with `waitForReply:true`:

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<voter terminal id>","command":"Ballot. Task was: <task>. A: <entry>. B: <entry>. C: <entry>. Judge on merit. Reply with exactly one label and a one-line reason."}],"handback":true}
```

   A neutral judge is one fresh launch with the same ballot as its `prompt`; it casts no peer vote, and decides only when that was the agreed method.
4. A dialog is not an entry or a vote: that participant stays pending. Map each vote through that voter's map. An ambiguous or off-ballot vote gets one clarification to that voter only, then counts as invalid. Nothing from a removed agent counts; if its entry was on issued ballots, apply the agreed rule or void and reissue that round, never silently remap labels.
5. Tally once every eligible vote is in or recorded missing; never manufacture a vote. The top score alone wins; a tied top score is a tie, never broken by you. A result a missing vote could change is provisional: it crowns no one and closes nothing.
6. On a tie, a winner-only follow-up waits for the user's choice: one of the tied, co-winners, or a runoff (daintree.orchestration.runoff-tiebreaker). Keep the tied terminals open.

Two rounds, no extras unless the rules say so; never relaunch an entrant after entries lock. "Close all but the winner" closes the non-winning entrants and any judge, never a winner; confirm from `closedIds`.

Report back: each entry with its votes and total; each voter's de-blinded choice and reason; eligible, received and valid vote counts; who judged (peer votes are only partly blind, since voters wrote entries); the winner, co-winners or tie; missing, late or removed participants; the terminals left open.
