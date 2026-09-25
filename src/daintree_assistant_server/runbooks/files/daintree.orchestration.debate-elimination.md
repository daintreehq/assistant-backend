---
id: daintree.orchestration.debate-elimination
title: Run agent debate with elimination vote
summary: Facilitate a multi-agent debate, collect final positions, run a peer vote to eliminate the weakest position, and report the tally.
whenToUse: Use when the user asks several agents to debate a topic and then eliminate, vote off, remove, or identify the weakest agent/position after the debate, including when the elimination request arrives mid-debate. Do not use for a collaborative debate that ends in one synthesized answer with no vote-off (use daintree.orchestration.multi-agent), an open-ended visible conversation among agents with no elimination vote (use daintree.orchestration.conversation), a one-shot independent scored contest or best-entry vote (use daintree.orchestration.competition), a simple fleet poll with no debate (use daintree.agents.poll-pinned-agents), or a runoff after a tied vote (use daintree.orchestration.runoff-tiebreaker).
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
Use when: agents debate a topic and then vote off, eliminate or identify the weakest agent or position under the user's criterion, including when the elimination request arrives mid-debate.

Availability: Core. In an agent pane, `notify` on each launch and send brings every position and vote back as it lands. An API-key client has no `notify`: close each round with one bounded `terminal.waitUntilIdleBatch` (`mode:"all"`) and tell the user what remains pending for their continuation. The sidebar assistant uses `terminal.sendCommand` and `terminal.close`.

You relay every hand-off; debaters cannot reach each other. Judging follows the foundation's "Judging even-handedly", with one difference: this vote is attributed. The debaters already know each position by name, so fairness comes from identical treatment, not hidden authorship. If the user wants the ideas judged blind by fresh agents, that is daintree.orchestration.competition.

Procedure:
1. Scope and record: topic, participants, the number of debate rounds, the elimination criterion in the user's exact words, the self-vote rule, and what "eliminate" means. Default: removed from the active cohort and told the verdict. It never means closing a terminal unless the user says close, kill or shut down. If the user set no self-vote rule, state one: any active position may be voted for, one's own only if the voter sincerely judges it weakest.
2. Cohort: named open agents as they are (yours or handed over, no replacements), otherwise fresh agents resolved to their real `agentId`s in one worktree.
3. Checkpoint (the foundation's), adding: the criterion and self-vote rule, the round number and phase (debate or vote), the active cohort, eliminated participants with the round they left, per participant its terminal id, pending prompt round and `submissionToken`, a one-line position per round, and the attributed vote map (voter, target, reason) per vote.
4. Openings: launch each debater with the topic and "state your opening position" as its `prompt`, with `notify` and `handback`, same-kind agents one at a time (the next launch of an `agentId` only after the previous call returned). Record ids, end your turn.
5. Debate rounds are simultaneous. When every active debater's position for the round is in or recorded missing, send each active debater the others' positions, condensed and labelled by name, plus one prompt for the next round: only to a debater at its prompt, with `notify` and `handback`, recording its token.
6. The elimination vote: list every active position with equal detail and neutral wording, no hint of which you or anyone found stronger, and state only the user's criterion:

```json tool=terminal.sendCommandOwned
{"terminalId":"<debater terminal id>","command":"Elimination vote, round <r>. Active positions: <Name>: <position>. <Name>: <position>. <Name>: <position>. Criterion: <the user's exact words>. <self-vote rule>. Reply with exactly one name and a one-line reason.","notify":true,"handback":true}
```

On notice or resume:
1. Follow the foundation's resume procedure, for each terminal the notice names.
2. Correlate: a position or vote counts only from an active participant whose prompt for this round and phase is pending, settled after that send (`terminal.getStatus` with its `submissionToken`). Once a participant is eliminated, its late position or vote is recorded and never counted as an active one, unless the rules expressly let eliminated participants vote.
3. An approval, trust or login screen, or an error, is not a debate response or a vote. The screen is the user's; that vote stays missing until it is resolved. Never answer it with debate text or "choose anyway".
4. Read each vote in the voter's own words (`terminal.readLastMessageOwned` for Claude Code, otherwise `terminal.getOutput`) and map it to exactly one active target. An ambiguous vote gets one clarification to that voter alone; still unclear, or naming someone not active, it is invalid.
5. An elimination request mid-debate: if a debater is still finishing a round that still matters, its notice is pending, so wait for it; otherwise vote on the latest positions and say whose input is missing.
6. Tally mechanically, counting votes received per target. The most votes is eliminated. A tied top is reported as a tie, and the user chooses: co-losers, their own pick, or a runoff vote between the tied. Never pick a sole loser. Silence, a missing vote or a closed terminal is never a vote to eliminate. Give valid and missing counts, and call the result provisional if an unresolved vote could change it.
7. Apply it: remove the loser from the active cohort and send it a short verdict (tally and key reasons) without `notify`; do not wait for a farewell. If the debate continues, relay the result only to survivors and go on with them. Close a terminal only on explicit request: verdict first, then `terminal.closeOwned` on the exact recorded id, confirmed from `closedIds`. Never send to a closed terminal.

Stop and recovery: bounded rounds only. At most one clarification per ambiguous vote. Never resend a prompt to a participant already pending; check its token. Elimination never implies closure. On cancellation keep a tombstone so a late vote cannot trigger a tally or a verdict.

Report back: the vote tally, each voter's vote and reason, who was eliminated (or the tie and the choice put to the user), valid and missing counts, which participants remain active, whether the loser was told, and any terminal closed.
