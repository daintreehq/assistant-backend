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
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned
---
Use when: agents debate a topic and then vote off the weakest agent or position under the user's criterion, including when the elimination request arrives mid-debate.

Availability: Core. An API-key client has no `notify`: close each round with one `terminal.waitUntilIdleBatch` (`mode:"all"`).

You relay every hand-off. The vote is attributed: debaters know each position by name, so fairness comes from identical treatment. Ideas judged blind by fresh agents is daintree.orchestration.competition.

Procedure:
1. Scope: topic, participants (identities, not kinds), debate rounds, the elimination criterion in the user's exact words, and the self-vote rule (default: any active position, one's own only if sincerely weakest). "Eliminate" means removed from the active cohort; telling the loser and closing its terminal are separate effects, and closing happens only if the user says close.
2. Cohort: named open agents as they are (yours or handed over), otherwise fresh launches in one worktree. Track per participant: terminal id, active or eliminated, pending round and phase, a one-line position per round, and the attributed votes (voter, target, reason).
3. Openings: launch every debater in this turn with the topic and "state your opening position in at most 20 lines", then end it.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Debate: <name>","worktreeId":"<worktree id>","prompt":"You are <name> in a debate with <others> on <topic>. State your opening position in at most 20 lines. The facilitator relays the others' positions. Change no files.","focusPolicy":"preserve","notify":true,"handback":true}
```

4. Debate rounds are simultaneous: once every active debater's position is in or recorded missing, send each the others' positions, condensed and labelled by name, plus the next prompt, in one turn, with `notify` and `handback`.
5. Vote: every active position with equal detail and neutral wording, and only the user's criterion.

```json tool=terminal.sendCommandOwned
{"terminalId":"<debater terminal id>","command":"Elimination vote, round <r>. Active positions: <Name>: <position>. <Name>: <position>. Criterion: <the user's exact words>. <self-vote rule>. Reply with exactly one name and a one-line reason.","notify":true,"handback":true}
```

On notice or resume:
1. Each quote is that debater's position or vote. It counts only from an active participant with that round's prompt pending; an eliminated participant's late reply is recorded, never counted.
2. A dialog or error is not a vote: that vote stays missing until the dialog is resolved as the foundation says. Never answer it with debate text or "choose anyway".
3. Map each vote to exactly one active target. An ambiguous vote gets one clarification to that voter alone; still unclear, or naming someone inactive, it is invalid.
4. An elimination request mid-debate: vote on the latest positions and say whose input is missing.
5. Tally mechanically: the most votes is eliminated. A tied top is a tie: the user chooses co-losers, their own pick, or a runoff between the tied; never pick a sole loser. A missing vote is never a vote to eliminate. A result an unresolved vote could change is provisional: eliminate, tell and close no one on it.
6. Apply a final result: remove the loser from the cohort, send it a short verdict without `notify`, and relay the result only to survivors if the debate continues. A requested close comes after the verdict, on the recorded id, confirmed from `closedIds`; report the verdict as sent, not as read.

Stop and recovery: bounded rounds. Never resend a pending prompt. On cancellation, a late vote triggers no tally or verdict.

Report back: the tally with each vote and reason; who was eliminated, or the tie and the choice put to the user; valid and missing counts; who remains active; whether the loser was sent the verdict; any terminal closed.
