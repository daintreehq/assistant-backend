---
id: daintree.orchestration.multi-agent
title: Orchestrate multiple agents on one problem
summary: Spawn or use explicitly named open agents, relay their work between them with terminal.sendCommandOwned, and synthesize ONE answer — collaborative multi-agent work driven until the cohort converges.
whenToUse: 'Use when several agents must work ONE problem together and you relay between them until they CONVERGE on a single synthesized answer — debate until they converge, collaborate, cross-check, iterate toward one merged result. The signal is a synthesis target: the deliverable is the ONE answer you assemble, not a dialogue. Do not use when each agent gets its OWN independent unit of work with no shared deliverable (use daintree.orchestration.fan-out-work) or separately tests one rival cause of a bug (use daintree.orchestration.hypothesis-split); for an open-ended visible dialogue — let them talk, see where it goes (use daintree.orchestration.conversation); a debate that ends by voting a position off (use daintree.orchestration.debate-elimination); independent submissions scored or peer-voted to pick a winner (use daintree.orchestration.competition); the same question put to several agents with each answer brought back, or a one-shot independent poll (use daintree.agents.poll-pinned-agents); or forwarding one agent''s review to another (use daintree.agents.relay-review-feedback).'
risk: project
requiredTools:
  - agent.launchMany
  - terminal.sendCommandMany
optionalTools:
  - agent.launch
  - agent.listAvailable
  - terminal.getStatus
  - terminal.closeMany
---
The deliverable is ONE synthesized answer, or the named residual disagreement, not a transcript. You are the only conduit: a participant's claim that it reached the others is not a relay. A native subagent never stands in for a requested visible agent.

On a host: call these tools on the work's machine; check code there, per the hosts foundation.

1. Scope: the problem, the cohort, a round bound (default: an opening round plus up to three relay rounds), the convergence test (positions stop moving, or the user's own), and any judge the user appointed. Participants are read-only unless the user wants files changed; editors sharing one worktree must serialize anything shared (index, installs, generated files), or get separate worktrees.
2. Cohort: identities, not kinds ("two Claudes" is two launches). Open agents the user named ARE the cohort, never replaced; new ones share one worktree. Track per participant: terminal id, status (active, dropped), and a one-line gist per round.
3. Round 1: launch new participants in one `agent.launchMany` with `waitForReply:true, handback:true`: "Round 1 of up to <n> on: <problem and context>. State your position with evidence and what you are unsure of, in at most 30 lines. Read-only: change no files. The facilitator relays between participants." Existing participants get it in the same round by send.
4. Relay rounds are simultaneous: once every active reply is in or recorded missing, one `terminal.sendCommandMany` (`waitForReply:true, handback:true`) gives each participant the others' actual claims and objections, condensed and labelled by name, plus the round's one question and "at most 30 lines". Fold user feedback into the next relay; never restart.
5. A reply for an earlier round is late material, relayed only labelled as late. Nothing from a dropped participant counts. A participant whose wait times out: tell the user; proceed without it only if it failed, exited or the user says so, recording it missing, never respawning it.
6. Stop when positions stop moving, the bound is reached, or they clearly will not agree.

Synthesis: apply your own judgment and check claims against the code, naming the commit or working tree you checked. An appointed judge (agent or user) settles disagreements; you relay, not overrule. Residual disagreement is a valid result. Close only the recorded cohort ids, on request.

Report back: the synthesized answer or residual disagreement; each participant (name, terminal id) and its contribution per round, with late or missing replies; what you verified versus what was claimed; anything pending; any cleanup.
