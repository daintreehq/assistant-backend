---
id: daintree.agents.poll-pinned-agents
title: Poll pinned agents
summary: Put the same question to several agents (named ones, the pinned or the configured fleet) independently, collect each answer from its notice, and report every answer, the vote tally or a consensus.
whenToUse: 'Use when the user wants the SAME question put to several agents and each independent answer brought back: "ask Claude, Codex and Gemini what they think of this", "get three agents to answer this and show me their answers", "have the pinned agents vote on my three options and tally it", or a poll of the pinned or configured agent fleet for opinions, votes or rankings among options, verdicts or recommendations with a consensus. The deliverable is their answers, not work, and the agents never see each other. Do not use when each agent is instead to be given its own unit of real work (use daintree.orchestration.fan-out-work), for a single agent (use daintree.agents.ask-and-relay), for multi-round debate or collaboration (use daintree.orchestration.multi-agent), for an open-ended dialogue where the agents talk to each other (use daintree.orchestration.conversation), for a judged competition over the agents'' own submissions or an interview (use daintree.orchestration.competition or daintree.orchestration.interview), or for breaking a tie (use daintree.orchestration.runoff-tiebreaker).'
risk: project
requiredTools:
  - agent.listAvailable
  - agent.launchMany
optionalTools:
  - agent.launch
  - terminal.getStatus
  - terminal.closeMany
---
The deliverable is each independent answer; with no vote or verdict asked for, report the answers and skip the tally. The voters are fresh launches of the agents named, pinned or configured, never existing open terminals.

1. Roster from `agent.listAvailable`. Pinned means `pinned:true` (`toolbarVisible` is something else). A missing `pinned` is an unknown pin state, not "unpinned": with no entry carrying it, say the pinned subset cannot be identified and offer all configured or named agents; with some carrying it, poll the `pinned:true` ones and list the rest as excluded for unknown pin state. Never report "no pinned agents" from missing fields. Skip one not `launchable` or not ready, with the reason. If the user only asked whether you can see them, report the count and names and stop.
2. One neutral prompt: the question and context, that the agent is polled independently and must change no files, and to answer in at most 15 lines, ending (for a vote) with a clear verdict and its two to five strongest reasons.
3. A vote among options: option text only, equal detail, no origin or status labels, neutral labels A/B/C rotated per voter; keep each voter's label-to-option map to yourself. A tiebreak the user gave (such as a runner-up) is asked for on the same ballot. Rotated ballots differ per voter, so launch each with its own `agent.launch` in one turn; an identical prompt for distinct kinds goes in one `agent.launchMany`. Pass `waitForReply:true, handback:true`. A repeated kind starts only after the previous one, so give it `notify:true, handback:true` instead and end your turn once the others are back.
A plain question to distinct kinds:

```json tool=agent.launchMany
{"agentIds":["claude","codex","gemini"],"name":"Poll: <topic>","prompt":"<neutral poll prompt>","waitForReply":true,"handback":true}
```

4. `missing-cli` or `launched:false` excludes that voter, with the reason.
5. Translate each label through that voter's own map. An answer still missing after the foundation's recheck, ambiguous, off-ballot or unreadable is excluded, never counted against the majority, and no vote is made up for it. A voter at any question, approval or error other than the launch-directory trust dialog is excluded with the reason.
6. Tally once every voter has answered or been excluded. A tie: apply the tiebreak the user already gave before reporting; ask only when none was given or it still ties. A later answer is a marked provisional update, never a silent change to a result already given.

If the user flags bias, rerun from scratch with fresh voters; never mix biased and corrected votes. Close only this poll's terminals, only on request.

Report back: each agent's answer, attributed; requested, launched, answered and excluded counts with reasons; for a vote, the tally over answered voters, the majority verdict with dissent, the mapped options and a synthesized recommendation; anyone still running.
