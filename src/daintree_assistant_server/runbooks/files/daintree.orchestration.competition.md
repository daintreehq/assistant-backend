---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: 'Run a scored contest among several named agents: ask each for one independent submission, relay all submissions for blind peer votes, tally the points, and declare the winner or tie — you run the ballot, the agents (or a neutral judge agent) supply the verdict.'
whenToUse: 'Use when the user names several agents (or the fleet) and asks each to produce one independent submission — an interesting fact, a design idea, a quick answer — and then wants them scored: award points, send each agent the others'' submissions, ask each to pick the best, tally, declare a winner. The signal is two-stage: the agents'' OWN submissions become the ballot they peer-judge. A vote over options YOU supplied, even one scored and tallied, is a fleet poll, not a competition. Do not use when each agent instead gets its own unit of real work (daintree.orchestration.fan-out-work), for a judge-led interview (interview runbook), a fleet poll with no scoring (poll-pinned-agents), collaborative debate toward one synthesized answer (multi-agent), a debate that ends by voting a position off (debate-elimination), or a runoff after a tie (runoff-tiebreaker).'
risk: project
requiredTools:
  - agent.launch
  - terminal.sendCommandOwned
optionalTools:
  - agent.listAvailable
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned

---
Use when: several agents each produce one independent submission, then the submissions are judged (peer vote, a neutral judge agent, or you only if the user made you judge) to name a winner or a tie.

Availability: Core, two rounds of notices. An API-key client has no `notify`: close each round with one `terminal.waitUntilIdleBatch` (`mode:"all"`) and report what is still pending.

Procedure:
1. Fix the rules before launching: the entry task; the judge (peer vote by default); the scoring rule, applied exactly as the user gave it; whether voters may pick their own entry (default no, and ballots then omit it); the tie rule (default: report the tie). Without an agreed judge you never score entries on merit yourself.
2. Cohort: one fresh entrant per agent named ("Claude, Codex and Gemini" means new launches, not open terminals of those kinds). Launch each by its built-in `agentId` without listing first, all in one worktree (the sidebar omits `worktreeId`). Track per entrant: agent, terminal id, status (active, removed, missing) and entry.
3. Entries: launch every entrant in this turn, then end it.

```json tool=agent.launch
{"agentId":"<built-in id: claude, codex, gemini, …>","name":"Contest: <agent> <n>","prompt":"<task>. Reply with ONE <entry> of at most a few lines, in your own words, here. Do not contact other agents. Change no files.","focusPolicy":"preserve","notify":true,"handback":true}
```

4. Ballots, once every active entrant has an entry or is recorded missing: entries lock. For each voter, list the eligible entries in one identical format under neutral labels, with no author, agent kind or "(you)", in a different order per voter, and keep that voter's label-to-entrant map to yourself, out of every prompt. Send every ballot in one turn, then end it.

```json tool=terminal.sendCommandOwned
{"terminalId":"<voter terminal id>","command":"Ballot. Task was: <task>. A: <entry>. B: <entry>. C: <entry>. Judge on merit. Reply with exactly one label and a one-line reason.","notify":true,"handback":true}
```

   A neutral judge is one fresh launch with the same ballot as its `prompt`; it casts no peer vote, and its verdict decides only when that was the agreed method.

On notice or resume:
1. Each notice names a terminal and quotes its reply: that is the entry or vote. Count it only from an active entrant or eligible voter with that round's prompt pending. Where a quote is missing or cut short, read all such terminals in one `terminal.getStatus` with `includeOutput`.
2. A dialog is not an entry: answer a trust dialog for the launch directory as the foundation says, with `notify`; anything else goes to the user and that entrant stays pending.
3. An entry arriving after ballots went out is late and changes no issued ballot. Entries lock when the first ballot is sent.
4. Map each vote through that voter's map. An ambiguous or off-ballot vote gets one clarification to that voter only, then counts as invalid. A removed agent gets nothing more and nothing from it counts; if its entry was on issued ballots, apply the agreed rule or void and reissue that ballot round, never silently remap labels.
5. Tally once every eligible vote is in or recorded missing. Never manufacture a vote for a missing or unclear reply. The top score alone wins; a tied top score is a tie, never broken by you. A result a missing vote could change is provisional: it crowns no one and closes nothing until the user decides.
6. On a tie, a winner-only follow-up waits for the user's choice: one of the tied, co-winners, or a runoff (daintree.orchestration.runoff-tiebreaker). Keep the tied terminals open.

Stop and recovery: two rounds, no extras unless the rules say so. Never resend a ballot that is pending or relaunch an entrant after entries lock. "Close all but the winner" closes the non-winning entrants and any judge, never a winner; confirm each from `closedIds`.

Report back: each entry with its votes and total; each voter's de-blinded choice and reason; eligible, received and valid vote counts; who judged (peer votes are only partly blind, since voters wrote entries); the winner, co-winners or tie; missing, late or removed participants; the terminals left open.
