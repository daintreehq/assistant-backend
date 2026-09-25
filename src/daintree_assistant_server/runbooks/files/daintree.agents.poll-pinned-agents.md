---
id: daintree.agents.poll-pinned-agents
title: Poll pinned agents
summary: Poll the pinned or configured agent fleet with one neutral question or option vote, read their free-form verdicts, and synthesize a consensus.
whenToUse: Use when the user asks to poll the pinned or configured agent fleet (or several named agents) for independent opinions, votes among options, verdicts, recommendations, or short answers and wants a synthesized consensus. The deliverable is opinions, not work. Do not use when each agent is instead to be given its own unit of real work (use daintree.orchestration.fan-out-work), for a single agent (use daintree.agents.ask-and-relay), for multi-round debate or collaboration (use daintree.orchestration.multi-agent), for an open-ended dialogue where the agents talk to each other (use daintree.orchestration.conversation), for a judged scoring competition or interview (use daintree.orchestration.competition or daintree.orchestration.interview), or for breaking a tie (use daintree.orchestration.runoff-tiebreaker).
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdle
  - terminal.waitUntilIdleBatch
  - terminal.closeOwned
---
Use when: the user asks to poll the pinned or configured agents (or several named agents), not existing open terminals, for independent opinions, a vote, verdicts or short answers, and wants a synthesized consensus.

Availability: Core, roster-dependent. Needs `agent.listAvailable`, `agent.launch` and terminal reads. With `notify` on each launch, answers come back one notice at a time. An API-key client has no `notify`: one bounded `terminal.waitUntilIdleBatch`, then report who answered and who is pending.

Procedure:
1. Get the roster from `agent.listAvailable`. Only ids, display names, pin state and availability go in a report; never relay settings or environment.
   - Pinned means an entry with `pinned:true`, the user's toolbar pin; `toolbarVisible` is a different thing. The field is optional and not every entry carries it: a missing `pinned` is an unknown pin state, not "unpinned". If no entry carries it, the pinned subset cannot be identified: say so, and offer to poll all configured agents or the ones the user names. If only some carry it, poll the `pinned:true` agents and list the rest as excluded because their pin state is unknown, offering to add them. Never report "no pinned agents" from entries that lack the field.
   - "The configured fleet", "all my agents" or named agents mean exactly those, without the pinned filter. "Pinned terminals" means pinned agents unless the user says open terminals; a pinned agent may have none open.
   - Skip an agent that is not `launchable` or whose `availability` is not ready, with the reason. With `availabilityComplete:false`, launchability is still unknown for some: say which.
2. If the user only asked whether you can see them, report the count and names and stop. With no agents to poll, report that and stop.
3. Write one neutral, self-contained prompt: the user's question and the context it needs, that the agent is being polled independently, that it must not modify files, and that it should answer in plain prose ending with a clear bottom-line verdict or vote and its two to five strongest reasons. No JSON or other rigid format: its own words are the answer. Do not investigate first unless the user also wants your opinion.
4. For a vote among options, apply the foundation's even-handed judging: option text only, equal detail, no origin or status labels ("original", "synthesis", "combined", "best so far"), neutral labels A/B/C, and the order shuffled or rotated per voter so the presumed favourite never sits in the same slot for everyone. Record each voter's label-to-option map privately.
5. Save the poll as a checkpoint (question, voters, each ballot map), then launch one fresh agent per voter. Freshness is mandatory for an impartial vote: never reuse a terminal that took part earlier or wrote an option, and never commandeer an open terminal the user did not name.

```json tool=agent.launch
{"agentId":"<voter's agent id>","name":"Poll: <topic>","worktreeId":"<resolved worktree id>","prompt":"<neutral poll prompt with this voter's ballot>","handback":true,"notify":true}
```

   Different agents may start together; two voters of the same agent kind start one after the other. Record each `terminalId` against its agent and ballot as it lands. `missing-cli` excludes that voter. `launched:false` created nothing: after reconciling, retry that voter once with a changed `name`, or exclude it. An ambiguous reply is reconciled with `terminal.list`, never answered with a second launch. Retry only the missing voters.
6. Report what launched, with terminal ids, and end your turn.

On notice or resume:
1. Follow the foundation's resume procedure. One notice line may name several terminals; handle each by its own id.
2. Read each settled voter's answer in free form: `terminal.readLastMessageOwned` for a Claude Code pane, `terminal.getOutput` otherwise. Take the verdict from what it actually wrote, with no structured extraction. If `lastHandback` or your own summary disagrees with the text, trust the text.
3. Validate the vote against that voter's ballot, then translate its label through that voter's own map; never assume A is the same option for two voters. A missing, ambiguous or off-ballot answer is excluded, never a vote against the majority. A voter stopped at a question, approval or error is excluded with the reason; do not answer approvals to keep the poll moving.
4. Save each valid answer to the checkpoint as it arrives, so a repeated notice changes nothing.
5. When every voter has answered or been excluded, tally and report. A straggler does not hold the rest hostage: once all the others are in, give it one bounded `terminal.waitUntilIdle`, then report the tally of valid answers with the straggler marked still running. Its later answer is reported as a marked late update, never a silent change to the result already given.

Stop and recovery: one launch per voter, one answer per terminal, never merged across terminals. If the user flags bias in the poll, discard it, rewrite the prompt neutrally and rerun with fresh terminals; never mix biased and corrected votes. If the user corrects you midway that they wanted an agent poll, stop local analysis and poll. Leave the terminals open; close only this poll's exact ids, only on request. Debate, peers voting on each other, a scored competition, an interview or a tie-break is a different runbook (daintree.orchestration.multi-agent, daintree.orchestration.competition, daintree.orchestration.interview, daintree.orchestration.runoff-tiebreaker), not an extension of this one.

Report back: requested, launched, answered and excluded counts separately, with reasons for exclusions; the consensus ratio and majority verdict; each agent's verdict with its strongest reasons, and the dissenters; anyone still running; for option votes, the mapped option identities; and the synthesized recommendation.
