---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn or use explicitly named open agents, relay their work between them with terminal.sendCommandOwned, and synthesize ONE answer — collaborative multi-agent work driven until the cohort converges.
whenToUse: 'Use when several agents must work ONE problem together and you relay between them until they CONVERGE on a single synthesized answer — debate until they converge, collaborate, cross-check, iterate toward one merged result. The signal is a synthesis target: the deliverable is the ONE answer you assemble, not a dialogue. Do not use when each agent gets its OWN independent unit of work with no shared deliverable (use daintree.orchestration.fan-out-work); for an open-ended visible dialogue — let them talk, see where it goes (use daintree.orchestration.conversation); a debate that ends by voting a position off (use daintree.orchestration.debate-elimination); independent submissions scored or peer-voted to pick a winner (use daintree.orchestration.competition); the same question put to several agents with each answer brought back, or a one-shot independent poll (use daintree.agents.poll-pinned-agents); or forwarding one agent''s review to another (use daintree.agents.relay-review-feedback).'
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
Use when: several agents work one problem together and you relay between them until they converge on ONE answer you synthesize. The deliverable is that answer, or the named residual disagreement, not a transcript.

Availability: Core. An API-key client has no `notify`: close each round with one `terminal.waitUntilIdleBatch` (`mode:"all"`). A native subagent never stands in for a requested visible agent.

You are the only conduit: a participant's claim that it reached the others is not a relay.

Procedure:
1. Scope: the problem, the cohort, a round bound (default: an opening round plus up to three relay rounds), the convergence test (positions stop moving, or the user's own), and any judge the user appointed. Participants are read-only unless the user wants files changed; editors sharing one worktree must serialize anything shared (index, installs, generated files), or get separate worktrees.
2. Cohort: participants are identities, not kinds: "two Claudes" is two launches. Open agents the user named ARE the cohort (yours or handed over), never replaced. Resolve new ones from `agent.listAvailable`, one worktree for all. Track per participant: terminal id, status (active, dropped), the round its pending prompt belongs to, and a one-line gist per round.
3. Round 1: launch every new participant in this turn, then end it. Existing participants get the round-1 question by send, as in step 4.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"Cohort: <role> <n>","worktreeId":"<worktree id>","prompt":"Round 1 of up to <n> on: <problem and context>. State your position with evidence and what you are unsure of, in at most 30 lines. Read-only: change no files. Answer here; the facilitator relays between participants.","focusPolicy":"preserve","notify":true,"handback":true}
```

4. Relay rounds are simultaneous. Once every active participant's reply to round N is in or recorded missing, send each one the others' actual claims and objections, condensed and labelled by name, plus the round's one question, all in one turn, then end it.

```json tool=terminal.sendCommandOwned
{"terminalId":"<participant terminal id>","command":"Round <N+1> of up to <n>. <the others' positions, by name>. <this round's question>. Answer here in at most 30 lines.","notify":true,"handback":true}
```

On notice or resume:
1. Each named terminal's quote is its reply. It counts for round N only from an active participant whose round-N prompt is pending; a reply for an earlier round is late material, relayed next only labelled as late, never as a current position. Nothing from a dropped participant counts.
2. Record each gist as it lands; answer a routine in-scope question at once (with `notify`). When one participant holds a round the others finished, tell the user who; proceed without it only if it failed, exited or the user says so, recording it missing, never respawning it.
3. User feedback between rounds goes into the next relay; never restart.
4. Stop when positions stop moving, the round bound is reached, or they clearly will not agree.

Synthesis: apply your own judgment and check claims against the code, naming the commit or working tree you checked, and keep what you verified apart from what participants claimed. An appointed judge (agent or user) settles disagreements; you relay, not overrule. Residual disagreement is a valid result.

Stop and recovery: finite rounds. Never resend a round prompt to a participant already pending. On cancellation send nothing more. Close only the recorded cohort ids, on request.

Report back: the synthesized answer or residual disagreement; each participant (name, terminal id) and its contribution per round, with late or missing replies; what you verified versus what was claimed; anything pending; any cleanup.
