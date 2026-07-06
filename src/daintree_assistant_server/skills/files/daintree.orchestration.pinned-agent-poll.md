---
id: daintree.orchestration.pinned-agent-poll
title: Poll pinned agents for a design verdict
summary: Poll the configured or pinned agent fleet for independent opinions, collect structured verdicts, and synthesize a consensus.
whenToUse: Use when the user asks to ask/poll the pinned or configured agent fleet, or several named agents, for a design opinion, recommendation, or yes/no verdict.
priority: 100
risk: project
tags:
  - daintree
  - orchestration
  - agents
  - pinned-agents
  - poll
  - design-review
  - consensus
requiredTools:
  - context.snapshot
  - daintree.call
  - agentTask.spawnForEdits
  - terminal.sendCommand
  - terminal.awaitAll
  - terminal.await.async
  - terminal.extract.json
  - terminal.read
  - terminal.summarize
---
Use when: the user asks you to ask/poll the pinned agent fleet, configured agent fleet, or several named agents for an independent design opinion, recommendation, or verdict.

Procedure:
1. Scope the poll. Extract the exact question, the allowed choices if the user supplied them, and the output you need back: verdict, reasons, confidence, risks, or recommendation. This is a one-shot independent poll: do not make agents debate or vote on each other's answers unless the user asks for that pattern. If the user wants only one agent, use the single-agent ask-and-relay pattern instead.
2. Do not answer or research the design question yourself first when the user asked for an agent poll. Send the user-supplied context to the agents promptly. Only read project files yourself if the user explicitly asks you to ground the poll or if you need a tiny missing fact to write a correct prompt.
3. Discover the target roster. First call context.snapshot to see open terminals. If the user says pinned agents, configured agents, agent fleet, or toolbar agents, call agentSettings.get through daintree.call with empty args and keep only the agent ids/display names relevant to the request, usually those with pinned:true. Pinning is an agent setting, not an open-terminal state; do not waste time searching docs for "pinned terminals". Ignore all env, token, custom preset, and secret fields returned by settings, and never quote or store them.
4. Choose fresh spawns vs existing terminals. If the user explicitly says to use already-open terminals, map the named/open terminals by full terminalId and send the poll with terminal.sendCommand. Ask before commandeering an ambiguous pre-existing user terminal. Otherwise spawn fresh read-only/explore terminals for the target agents; pinned agents may have zero open terminals.
5. Spawn the poll cohort with agentTask.spawnForEdits, one call per target agent, in one batch if small or groups of about 4-6 if large. Use mode:"explore" unless the user explicitly wants file changes. Give each a distinct short title like "poll: <topic> - <agent>". Keep taskPrompt short and self-contained: include the question, the key context, "Do not edit files", and ask for JSON-ish output containing verdict, 2-5 reasons, and confidence. Do not attach watchers for an in-turn poll.
   - Validate each spawn call's JSON arguments before sending. If one spawn fails from invalid JSON or launch error, retry only that failed target once; do not respawn agents that already launched successfully.
6. On the next turn after spawns return, record every full terminalId exactly with its agent name. Do not try to use a terminalId in the same batch that minted it. If using existing terminals, likewise keep a terminalId→agent map so the final tally is attributable.
7. Wait for answers with ONE terminal.awaitAll over the cohort: terminal.awaitAll({"terminalIds":["<full-id>","<full-id>"],"maxAttempts":60}). If some are stillWorking, read/extract the finished agents and re-await only the stillWorking ids, at most twice. If a straggler remains hung, either continue with the finished answers and note the omission, or hand off the lone wait with terminal.await.async and tell the user you will report when it finishes.
8. Extract the verdicts in one structured pass over the finished terminalIds. Use terminal.extract.json with a real JSON Schema, not an example object, for example: {"type":"object","properties":{"responses":{"type":"array","items":{"type":"object","properties":{"terminalId":{"type":"string"},"agent":{"type":"string"},"verdict":{"type":"string"},"reasons":{"type":"array","items":{"type":"string"}},"confidence":{"type":"string"}},"required":["terminalId","verdict","reasons"]}}},"required":["responses"]}. If the result is sparse, malformed, or misses earlier long output, fall back to terminal.read or terminal.summarize for the affected terminal. Do not re-extract per agent after a successful cohort extract already returned attributed answers.
9. Tally and synthesize. Normalize obvious verdict synonyms, count the sides, identify the majority and any dissenters, and preserve the strongest minority argument. If there is no clear majority, report the split rather than forcing consensus. Do not override the agents with your own unsupported verdict; your job is to synthesize the poll.
10. Leave spawned terminals open and idle unless the user explicitly asks to close them. Do not close, rename, or repurpose unrelated pre-existing terminals.

Confirmation: No extra confirmation is needed after the user asks you to run a fresh read-only poll. Ask before using edit mode, sending into an ambiguous pre-existing user terminal, or closing any terminal.

Report back: the agents polled with full terminalIds, the vote/consensus tally, the majority conclusion, notable dissenting reasoning, any failed/omitted agents, and the practical recommendation.

Notes:
- If the user later asks for debate, peer voting, or a judged competition, switch to the multi-agent/competition orchestration pattern instead of this one-shot poll.
- A pinned agent is not necessarily an open terminal; zero open terminals can still mean several pinned agents are available to spawn.
