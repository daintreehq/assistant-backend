---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn or use explicitly named open agents, relay their work between them with terminal.sendCommandOwned, and synthesize ONE answer — collaborative multi-agent work driven until the cohort converges.
whenToUse: 'Use when several agents must work ONE problem together and you relay between them until they CONVERGE on a single synthesized answer — debate until they converge, collaborate, cross-check, iterate toward one merged result. The signal is a synthesis target: the deliverable is the ONE answer you assemble, not a dialogue. Do not use when each agent gets its OWN independent unit of work with no shared deliverable (use daintree.orchestration.fan-out-work); for an open-ended visible dialogue — let them talk, see where it goes (use daintree.orchestration.conversation); a debate that ends by voting a position off (use daintree.orchestration.debate-elimination); independent submissions scored or peer-voted to pick a winner (use daintree.orchestration.competition); a one-shot independent poll (use daintree.agents.poll-pinned-agents); or forwarding one agent''s review to another (use daintree.agents.relay-review-feedback).'
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
  - terminal.notifyWhenIdle
  - terminal.waitUntilIdle
  - terminal.waitUntilIdleBatch
  - terminal.sendCommand
  - terminal.closeOwned
  - terminal.close
---
Use when: several agents work one problem together and you relay between them until they converge on ONE answer you synthesize. The deliverable is that answer, or the named residual disagreement, not a transcript.

Availability: Core. In an agent pane, `notify` on every launch and send brings each participant's reply back as it lands. An API-key client has no `notify`: run each round with one `terminal.waitUntilIdleBatch` (`mode:"all"`), and tell the user that a round still running needs their continuation. The sidebar assistant uses `terminal.sendCommand` for the send. Participants are visible Daintree panes; do not assume your host has native team tools, and a native subagent never stands in for a requested visible agent.

You are the only conduit. Participants cannot see or message each other. Never tell one to contact, reply to or coordinate with another; each writes in its own terminal and you route everything. A participant's claim that it reached the others is not a relay.

Procedure:
1. Scope: the problem, the cohort, a round bound (default: an opening round plus up to three relay rounds), the convergence test (positions stop moving, or the user's own), and any judge the user appointed. Participants are read-only unless the user wants files changed; several editors need disjoint files or serialized edits, even on one shared problem.
2. Cohort. Agents the user named that are already open ARE the cohort: do not spawn replacements, and do not summarize what they already said instead of relaying. The owned send reaches only panels you launched or the user handed you (`terminal.list` with `owned:true`); for any other, ask the user to hand it over. Otherwise resolve each requested agent to its real `agentId` (distinct agents, not copies of the default unless asked) and put the whole cohort in one worktree.
3. Start the checkpoint (the foundation's) with the problem, convergence test, round bound, judge, and per participant: role, agent id, terminal id, status (active or dropped), the round its current prompt belongs to, that prompt's `submissionToken`, whether its reply is pending, and a one-line gist per round.
4. Open round 1. Launch new participants with the opening question as `prompt`:

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Cohort: <role> on <problem>","worktreeId":"<resolved worktree id>","prompt":"Round 1 of up to <n>. <problem and context>. State your position with evidence and say what you are unsure of. Read-only: change no files. Answer here; you cannot reach the other participants and must not try.","notify":true,"handback":true}
```

   Start same-kind agents one at a time: launch the next one with the same `agentId` only after the previous call has returned. Different kinds may launch together. Record each `terminalId` as it returns. Existing participants get round 1 through the send in step 5. Then end your turn.
5. Relay rounds are simultaneous: every active participant answers the same round question. Once every active participant's reply to round N is in or recorded missing, build round N+1 for each one: the others' actual claims, drafts and objections, condensed and labelled by name, plus this round's one question. Relay content, never just "the others responded". Send only to a participant sitting at its prompt:

```json tool=terminal.sendCommandOwned
{"terminalId":"<participant terminal id>","command":"Round <N+1> of up to <n>. <the other participants' positions, by name>. <this round's question>. Answer here; do not contact the others.","notify":true,"handback":true}
```

   Record its `submissionToken` and round, mark it pending, then end your turn. Give one participant the floor alone only when the round truly depends on it answering a specific challenge first.

On notice or resume:
1. Follow the foundation's resume procedure. One notice line may name several terminals: handle each terminal it names, not the whole cohort.
2. Correlate before recording. A reply counts for round N only when the participant is active, the checkpoint shows its round-N prompt pending, and the settle follows that send (`terminal.getStatus` with its `submissionToken`; a `lastHandback.submissionToken` equal to it confirms). A reply to an earlier round that lands after later prompts went out is late: file it under its own round and relay it next only as labelled late material, never as that participant's current position. A reply from a dropped participant is not a position at all.
3. Read the reply itself (`terminal.readLastMessageOwned` for a Claude Code pane, otherwise `terminal.getOutput`), record its gist and clear pending. A routine question within scope gets one answer with `notify` again; an approval, trust or login screen is the user's; an error is reported, not waited out.
4. One reply never stalls the others: record it and handle its questions now. The next round still goes out only when the replies it depends on are in. If one participant is still working after the rest have replied, do at most one bounded `terminal.waitUntilIdle`, then end your turn (its notice is still pending) and tell the user which participant holds the round. Proceed without it only if it failed or exited, or the user says so; record it missing for that round and never respawn it in a loop. If it later answers while the next round is still open, send it that round's prompt; its old reply stays late material.
5. User feedback between rounds goes into the next relay. Do not restart.
6. Stop when positions stop moving, the round bound is reached, or they clearly will not agree. Then synthesize.

Synthesis: you may apply your own technical judgment and check claims against the code yourself; keep what you verified separate from what participants claimed. If the user appointed a judge, whether an agent or themselves, the judge settles disagreements; you relay and report, not overrule. Residual disagreement is a valid result.

Stop and recovery: finite rounds only. Never report convergence because a participant says it reached the others. Never resend a round prompt to a participant already pending: a queued send is not a lost one, so check its token first. A lost launch reply is reconciled with `terminal.list`, never answered with a second launch. On cancellation keep a tombstone, send nothing more, and report which participants are still working. Leave terminals open; close only the exact recorded cohort ids on request, with `terminal.closeOwned`.

Report back: the synthesized answer or the residual disagreement, each participant (name and terminal id) with what it contributed per round and any late or missing replies, what you verified versus what participants claimed, anything still pending and the continuation arranged, and any cleanup performed.
