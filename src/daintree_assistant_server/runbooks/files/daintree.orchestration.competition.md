---
id: daintree.orchestration.competition
title: Orchestrate a competition or peer-judging among agents
summary: 'Run a contest among several named agents: each makes one independent entry for the same task (an answer, or its own version of a change in its own worktree), then blind peer votes or the agreed judge compare them and pick the winner or tie.'
whenToUse: 'Use when the user names several agents (or the fleet) and has each produce its OWN independent entry for the SAME task — an interesting fact, a design idea, a quick answer, or its own version of the same code change in its own worktree — and then wants the entries compared and a winner picked: "have Claude and Codex compete", "each writes its own version, then tell me which is better", award points, send each agent the others'' submissions to pick the best, tally, declare a winner. The signal is two-stage: rival entries for one task, then a verdict on them. A vote over options YOU supplied, even one scored and tallied, is a fleet poll, not a competition. Do not use when each agent instead gets its own DIFFERENT unit of real work (daintree.orchestration.fan-out-work), for independent reviews whose findings are merged rather than ranked (daintree.review.review-change), a judge-led interview (daintree.orchestration.interview), a poll for opinions with no scoring (daintree.agents.poll-pinned-agents), collaborative debate toward one synthesized answer (daintree.orchestration.multi-agent), a debate that ends by voting a position off (daintree.orchestration.debate-elimination), a runoff after a tie (daintree.orchestration.runoff-tiebreaker), or moving agents that are already running into worktrees (daintree.worktree.relocate-agents).'
risk: project
requiredTools:
  - agent.launchMany
  - terminal.sendCommandMany
optionalTools:
  - agent.launch
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
  - terminal.closeMany
---
1. Fix the rules before launching: the entry task; the judge (peer vote by default; "compare them and tell me which is better" makes you the judge); the scoring rule exactly as the user gave it; whether voters may pick their own entry (default no, and ballots then omit it); the tie rule (default: report the tie). Without an agreed judge you never score entries on merit yourself.
2. Entries: one fresh entrant per agent named ("Claude, Codex and Gemini" means new launches, not open terminals). Brief them with the user's task as given: the entrants read the code, so do not study it first. Track per entrant: terminal id, status (active, removed, missing) and entry.
   - Answer entries: all in one worktree, one `agent.launchMany` with `waitForReply:true, handback:true` and the prompt "<task>. Reply with ONE <entry> of at most a few lines, in your own words. Do not contact other agents. Change no files."
   - Code entries (each writes its own version): one worktree per entrant with `worktree.createWithRecipe` (new branch from the same base, no `recipeId`, `collisionPolicy:"error"`; worktrees start from committed code, so say so if the target has uncommitted changes) and `worktree.waitUntilReady`, then one `agent.launch` each with its `worktreeId` and `waitForReply:true, handback:true`, issued together, prompt "<task>. Work only in this worktree; commit nothing. Reply with a short summary of your change." The entry is that worktree's changes (`git status`, `git diff HEAD`, untracked files included), not the reply.
3. You as judge: skip steps 3 to 6. Compare the entries yourself (for code, the diffs: correctness first, the project's tests where cheap, then the user's criterion) and name the better one with evidence, or say they are even.
   Ballots, once every active entrant has an entry or is recorded missing. Entries lock when the first ballot is sent: a later entry is late and changes no issued ballot. Give each voter the eligible entries in one identical format under neutral labels, with no author, agent kind or "(you)", in a different order per voter; keep each voter's label-to-entrant map to yourself. Send all ballots in one `terminal.sendCommandMany` with `waitForReply:true`:

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<voter terminal id>","command":"Ballot. Task was: <task>. A: <entry>. B: <entry>. C: <entry>. Judge on merit. Reply with exactly one label and a one-line reason."}],"waitForReply":true,"handback":true}
```

   A neutral judge is one fresh launch with the same ballot as its `prompt`; it casts no peer vote, and decides only when that was the agreed method.
4. A dialog is not an entry or a vote: that participant stays pending. Map each vote through that voter's map. An ambiguous or off-ballot vote gets one clarification to that voter only, then counts as invalid. Nothing from a removed agent counts; if its entry was on issued ballots, apply the agreed rule or void and reissue that round, never silently remap labels.
5. Tally once every eligible vote is in or recorded missing; never manufacture a vote. The top score alone wins; a tied top score is a tie, never broken by you. A result a missing vote could change is provisional: it crowns no one and closes nothing.
6. On a tie, a winner-only follow-up waits for the user's choice: one of the tied, co-winners, or a runoff (daintree.orchestration.runoff-tiebreaker). Keep the tied terminals open.

Two rounds, no extras unless the rules say so; never relaunch an entrant after entries lock. "Close all but the winner" closes the non-winning entrants and any judge, never a winner; confirm from `closedIds`.

Report back: each entry (for code, its worktree, branch and diff summary) with its votes and total, or your comparison when you judged; each voter's de-blinded choice and reason; eligible, received and valid vote counts; who judged (peer votes are only partly blind, since voters wrote entries); the winner, co-winners or tie; missing, late or removed participants; the terminals left open.
