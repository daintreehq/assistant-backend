---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: 'Run a scored contest among several named agents: ask each for one independent submission, relay all submissions for blind peer votes, tally the points, and declare the winner or tie — you run the ballot, the agents (or a neutral judge agent) supply the verdict.'
whenToUse: 'Use when the user names several agents (or the fleet) and asks each to produce one independent submission — an interesting fact, a design idea, a quick answer — and then wants them scored: award points, send each agent the others'' submissions, ask each to pick the best, tally, declare a winner. The signal is two-stage: the agents'' OWN submissions become the ballot they peer-judge. A vote over options YOU supplied, even one scored and tallied, is a fleet poll, not a competition. Do not use when each agent instead gets its own unit of real work (daintree.orchestration.fan-out-work), for a judge-led interview (interview runbook), a fleet poll with no scoring (poll-pinned-agents), collaborative debate toward one synthesized answer (multi-agent), a debate that ends by voting a position off (debate-elimination), or a runoff after a tie (runoff-tiebreaker).'
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.sendCommandOwned
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - agent.listPresets
  - worktree.list
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdleBatch
  - terminal.sendCommand
  - terminal.closeOwned
  - terminal.close
---
Use when: several agents each produce one independent submission, and those submissions are then judged (peer vote, a neutral judge agent, or a judge the user agreed on) to name a winner or a tie.

Availability: Core. In an agent pane, `notify` on each launch and ballot send brings every entry and vote back as it lands. An API-key client has no `notify`: close each phase with one bounded `terminal.waitUntilIdleBatch` (`mode:"all"`) and tell the user what remains pending for their continuation. The sidebar assistant uses `terminal.sendCommand` and `terminal.close`.

Judging follows the foundation's "Judging even-handedly"; the rules below are what this format adds.

Procedure:
1. Fix the rules before any dispatch and record them: the entry task; the judging method (peer vote by default, a neutral judge agent, or you only when the user made you the judge or agreed to it, which you then state); the scoring rule, applied mechanically exactly as the user gave it; whether a voter's ballot includes its own entry; the self-vote rule; the tie rule (default: report a tie). With no agreed judge, you do not pre-score entries on merit; a shortlist uses only the user's mechanical criterion (meets the constraint, within the length, first N).
2. Cohort: fresh entrants, so each entry comes from an independent context. Naming agent kinds ("Claude, Codex and Gemini") asks for fresh entrants, not the open terminals of those kinds. Use an existing terminal only if the user picked that one and it is yours or handed over. Resolve each to its real `agentId`; one worktree for all.
3. Checkpoint (the foundation's), adding: the rules; the phase (entries, ballots, result); per entrant its agent, terminal id, status (active, removed, disqualified) and entry; per voter its ballot generation, `submissionToken`, private tag-to-entrant map, vote and validity.
4. Entry phase. Launch each entrant with the entry task as its `prompt`, same-kind agents one at a time (the next launch of an `agentId` only after the previous call returned):

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Contest entry: <agent>","worktreeId":"<resolved worktree id>","prompt":"Entry round for <contest id>. <task>. Produce ONE <entry>, in your own words, here. Do not look at or contact any other agent. Change no files.","notify":true,"handback":true}
```

   Record each `terminalId` and end your turn. The phase closes when every active entrant has an entry or is recorded missing (failed, removed, or the user says go on). Entries lock when the first ballot is sent.
5. Ballot phase. Build each voter's ballot from the locked, eligible entries in their own wording, presented in one identical format, with no author, agent kind or status. Rotate the neutral tags per voter and record that voter's map before sending. Never mark "(you)". Then:

```json tool=terminal.sendCommandOwned
{"terminalId":"<voter terminal id>","command":"Ballot <contest id>, round <r>. Task was: <task>. Entry A: <entry>. Entry B: <entry>. Entry C: <entry>. Judge these on merit. Reply with exactly one label and a one-line reason.","notify":true,"handback":true}
```

   Record each token and end your turn. For a neutral judge, launch one fresh agent with the same neutral ballot as its `prompt`. It is not an entrant and casts no tallied vote, and its verdict replaces the peer tally only when that is the agreed method.

On notice or resume:
1. Follow the foundation's resume procedure, for each terminal the notice names.
2. Correlate by phase. An entry counts only from an active entrant whose entry prompt is pending. A vote counts only from an eligible voter whose current ballot is pending and settled after that ballot's send (`terminal.getStatus` with its `submissionToken`).
3. A late entry that lands after ballots went out is recorded as late and changes no issued ballot. It joins only if the user reopens entries, and then every voter gets a new ballot generation and every earlier vote is void.
4. Read each vote from the voter's own words (`terminal.readLastMessageOwned` for Claude Code, otherwise `terminal.getOutput`), map its label through that voter's map, and attribute it by terminal id. An ambiguous or off-ballot answer gets one clarification to that voter alone; if it is still unclear, the vote is invalid. A forbidden self-vote: quietly ask that voter for its next choice, never revealing authorship.
5. A voter at an approval, trust or login screen has not voted; that screen is the user's. A removed or disqualified agent leaves the active set at once: send it nothing, count none of its later replies, and score nothing from it.
6. Result, once every eligible vote is in or recorded missing: tally mechanically from the recorded votes and give eligible, received and valid counts. A missing voter is not counted and no vote is manufactured; a result a missing or invalid vote could change is provisional. The top score alone wins; a tied top score is a tie, never broken by you. A neutral judge's verdict is reported as given; any doubt you have is an observation, not a re-ranking.
7. On a tie, a winner-only follow-up ("close all but the winner", "winner gives a speech") waits for the user's choice: one of the tied, co-winners, or a runoff (daintree.orchestration.runoff-tiebreaker). Keep all tied terminals open and hand the checkpoint over intact for a runoff.

Stop and recovery: two phases and no extra rounds unless the rules say so. At most one clarification per ambiguous vote. Never resend a ballot to a voter whose ballot is pending, and never relaunch an entrant after entries lock. Reconcile a lost launch reply with `terminal.list`. Close only on request: "all but the winner" closes the recorded non-winning entrants and any judge (never a winner) and keeps every winner or co-winner; confirm each close from `closedIds`, since finished is not closed. A winner follow-up goes only to the resolved winner terminals, with `notify`.

Report back: each entrant's entry, votes received and total; each voter's de-blinded choice and reason; eligible, received and valid counts; who decided (peer vote, the judge agent, or you as the agreed judge), noting that peer voting was only partially blinded; the winner, co-winners or tie; removed, missing or late participants; anything pending; and any cleanup, with the terminals left open.
