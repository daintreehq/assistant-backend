---
id: daintree.agents.poll-pinned-agents
title: Poll pinned agents
summary: Put the same question to several agents (named ones, the pinned or the configured fleet) independently, collect each answer from its notice, and report every answer, the vote tally or a consensus.
whenToUse: 'Use when the user wants the SAME question put to several agents and each independent answer brought back: "ask Claude, Codex and Gemini what they think of this", "get three agents to answer this and show me their answers", "have the pinned agents vote on my three options and tally it", or a poll of the pinned or configured agent fleet for opinions, votes or rankings among options, verdicts or recommendations with a consensus. The deliverable is their answers, not work, and the agents never see each other. Do not use when each agent is instead to be given its own unit of real work (use daintree.orchestration.fan-out-work), for a single agent (use daintree.agents.ask-and-relay), for multi-round debate or collaboration (use daintree.orchestration.multi-agent), for an open-ended dialogue where the agents talk to each other (use daintree.orchestration.conversation), for a judged competition over the agents'' own submissions or an interview (use daintree.orchestration.competition or daintree.orchestration.interview), or for breaking a tie (use daintree.orchestration.runoff-tiebreaker).'
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned
---
Use when: the user puts one question to several named agents, or the pinned or configured ones (not existing open terminals), for independent answers, opinions, a vote or verdicts. With no vote or verdict asked for, report each answer and skip the tally.

Availability: Core, one round of notices. An API-key client has no `notify`: one `terminal.waitUntilIdleBatch` (`mode:"all"`), then report who answered and who is pending.

Procedure:
1. Roster from `agent.listAvailable`; report only ids, names, pin state and availability.
   - Pinned means `pinned:true` (`toolbarVisible` is something else). A missing `pinned` is an unknown pin state, not "unpinned": with no entry carrying it, say the pinned subset cannot be identified and offer all configured or named agents; with some carrying it, poll the `pinned:true` ones and list the rest as excluded for unknown pin state. Never report "no pinned agents" from missing fields.
   - "All my agents" or named agents mean exactly those. Skip one not `launchable` or not ready, with the reason; `availabilityComplete:false` leaves some unknown.
2. If the user only asked whether you can see them, report the count and names and stop.
3. Write one neutral prompt: the question and its context, that the agent is polled independently, that it must change no files, and to answer in at most 15 lines of plain prose, ending (for a poll or vote) with a clear verdict and its two to five strongest reasons.
4. For a vote among options: option text only, equal detail, no origin or status labels, neutral labels A/B/C in a rotated order per voter. Keep each voter's label-to-option map to yourself.
5. Launch one fresh voter per agent in this turn, each with a distinct `name`, even two of the same kind; never reuse a terminal that wrote an option or that the user did not name. Record voter, terminal id and ballot map, then end your turn.

```json tool=agent.launch
{"agentId":"<voter's agent id>","name":"Poll <n>: <topic>","worktreeId":"<worktree id>","prompt":"<neutral poll prompt with this voter's ballot>","focusPolicy":"preserve","notify":true,"handback":true}
```

   `missing-cli` excludes that voter. `launched:false` created nothing: retry that voter once after the same-kind launch before it has started, or exclude it.

On notice or resume:
1. Each notice names a terminal and quotes its answer: take the verdict from the quote. Where a quote is missing or cut, read all such terminals in one `terminal.getStatus` with `includeOutput`; an answer you still cannot read whole is excluded as unreadable.
2. Translate each label through that voter's own map. A missing, ambiguous or off-ballot answer is excluded, never counted against the majority, and no vote is ever made up for it. A voter at a trust dialog for the launch directory is answered as the foundation says; any other question, approval or error excludes it with the reason.
3. Tally once every voter has answered or been excluded. While a notice is pending, end your turn instead of waiting or reading: a notice held back while you were working lands as soon as you stop. Report early only when the user asked for it or a voter is stuck at a dialog, marking the rest still running. A later answer is a marked provisional update, never a silent change to the result already given.

Stop and recovery: one launch per voter, one answer per terminal, never merged across terminals. If the user flags bias, rerun from scratch with fresh voters; never mix biased and corrected votes. Close only this poll's terminals, only on request. Peer judging, debate, interviews or tie-breaks belong to daintree.orchestration.competition, daintree.orchestration.multi-agent, daintree.orchestration.interview and daintree.orchestration.runoff-tiebreaker.

Report back: each agent's answer, attributed; requested, launched, answered and excluded counts (with reasons); for a poll or vote, the tally over answered voters, the majority verdict with the dissent, the mapped options and the synthesized recommendation; anyone still running.
