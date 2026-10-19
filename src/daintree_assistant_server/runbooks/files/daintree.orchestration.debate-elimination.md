---
id: daintree.orchestration.debate-elimination
title: Run agent debate with elimination vote
summary: Facilitate a multi-agent debate, collect final positions, run a peer vote to eliminate the weakest position, and report the tally.
whenToUse: Use when the user asks several agents to debate a topic and then eliminate, vote off, remove, or identify the weakest agent/position after the debate, including when the elimination request arrives mid-debate. Do not use for a collaborative debate that ends in one synthesized answer with no vote-off (use daintree.orchestration.multi-agent), an open-ended visible conversation among agents with no elimination vote (use daintree.orchestration.conversation), a one-shot independent scored contest or best-entry vote (use daintree.orchestration.competition), a simple fleet poll with no debate (use daintree.agents.poll-pinned-agents), or a runoff after a tied vote (use daintree.orchestration.runoff-tiebreaker).
risk: project
requiredTools:
  - agent.launchMany
  - terminal.sendCommandMany
optionalTools:
  - agent.launch
  - terminal.sendCommandOwned
  - terminal.list
  - terminal.getStatus
  - terminal.closeOwned
---
You relay every hand-off. The vote is attributed: debaters know each position by name, so fairness comes from identical treatment.

On a host: call these tools on each debater's machine.

1. Scope: topic, participants (identities, not kinds), debate rounds, the elimination criterion in the user's exact words, and the self-vote rule (default: any active position, one's own only if sincerely weakest). "Eliminate" means removed from the active cohort; telling the loser and closing its terminal are separate, and closing happens only if the user says close.
2. Cohort: named open agents as they are (yours or handed over), otherwise fresh launches in one worktree. Track per participant: terminal id, active or eliminated, a one-line position per round, and attributed votes (voter, target, reason).
3. Openings: one `agent.launchMany` with `waitForReply:true, handback:true`: "Debate on <topic> among <participants>. State your opening position in at most 20 lines. The facilitator relays the others' positions. Change no files."
4. Rounds are simultaneous: once every active position is in or recorded missing, one `terminal.sendCommandMany` (`waitForReply:true, handback:true`) gives each debater the others' positions, condensed and labelled by name, plus the next prompt.
5. Vote, the same way: every active position with equal detail and neutral wording, and only the user's criterion.

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<debater terminal id>","command":"Elimination vote, round <r>. Active positions: <Name>: <position>. <Name>: <position>. Criterion: <the user's exact words>. <self-vote rule>. Reply with exactly one name and a one-line reason."}],"waitForReply":true,"handback":true}
```

6. A reply counts only from an active participant for the current round; an eliminated participant's late reply is recorded, never counted. A dialog or error is not a vote: never answer it with debate text or "choose anyway". Map each vote to exactly one active target; an ambiguous one gets one clarification to that voter alone, then is invalid. An elimination request mid-debate votes on the latest positions and says whose input is missing.
7. Tally mechanically: the most votes is eliminated. A tied top is a tie: the user chooses co-losers, their own pick, or a runoff between the tied; never pick a sole loser. A missing vote is never a vote to eliminate. A result an unresolved vote could change is provisional: eliminate, tell and close no one on it.
8. Apply a final result: remove the loser from the cohort, send it a short verdict (`terminal.sendCommandOwned`, no wait), and relay the result to survivors only if the debate continues. A requested close comes after the verdict, confirmed from `closedIds`; report the verdict as sent, not as read.

Report back: the tally with each vote and reason; who was eliminated, or the tie and the choice put to the user; valid and missing counts; who remains active; whether the loser was sent the verdict; any terminal closed.
