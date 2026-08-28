---
id: daintree.agents.relay-review-feedback
title: Relay review feedback to an implementer
summary: Forward review output from one running agent to another with user-specified calibration and optional follow-up instructions.
whenToUse: Use when the user asks you to read one existing reviewer agent's output, review, or notes and point/send another existing implementer agent to it, optionally telling the target how to weigh the source or to report whether the review was useful. The source is a reviewer AGENT's terminal — feedback that must first be fetched from the forge (a PR's review decision, a failing check, an issue thread) is a different job (use daintree.workflow.follow-up-on-issue). Do not use to judge whether the reviewed work is actually done or correct (use daintree.orchestration.verify-agent-work), to spawn a new single agent and ask it a question (use daintree.agents.ask-and-relay), to poll several agents for independent opinions (use daintree.agents.poll-pinned-agents), or to run multi-round collaboration among agents (use daintree.orchestration.multi-agent).
opener: "Feedback handoff — the right implementer, with findings and weight intact"
risk: project
requiredTools:
  - context.snapshot
  - terminal.extract
  - terminal.read
  - terminal.sendCommand
  - artifact.read
---
Use when: the user wants you to take review/feedback from one existing agent terminal and send it to another existing agent terminal that is implementing or fixing the work.

Procedure:
1. Identify the source reviewer terminal and target implementer terminal from the user's description. Start with `context.snapshot` to see live terminals, titles, agent ids, focus, and worktree context. Use full terminalIds internally; abbreviated ids are only for user-facing summaries.
2. If there are multiple similar terminals, resolve ambiguity before sending anything — relaying a review into the wrong agent's live work is the expensive mistake here. Emit ONE short `terminal.extract` PER candidate id (`{"terminalIds":["<one-id>"],"instruction":"What is this terminal working on right now?"}`), all batched in a single turn: no-wait extracts dispatch concurrently, so the whole set costs roughly one call's wait. Do NOT put several candidate ids into one plain-TEXT `terminal.extract` — over multiple terminalIds it MERGES every terminal's tail into ONE answer instead of returning one per terminal, so it cannot tell the candidates apart. Do not message an irrelevant terminal just because it has the right agent name, is focused, or has a generic title.
3. Read the review from the source terminal. For a quick visible review, use `terminal.extract` for the final verdict/summary and main findings. If the extract is partial, truncated, or only points to a separate artifact/large scrollback, use `terminal.read` and page any returned archived content with `artifact.read` as needed. Do not claim you read the full review when you only saw the visible tail.
4. Prepare a compact relay message for the target implementer. Include:
   - who produced the review;
   - the review verdict or gist;
   - the specific actionable risks/findings;
   - where to find raw notes if the target can access them;
   - any calibration the user explicitly requested about the source's reliability/capability.
   Keep calibration operational: e.g. “treat this as lower-confidence input and verify before acting.” Do not invent a model ranking or add derogatory framing unless the user specifically asked for that wording.
5. Send the relay with `terminal.sendCommand({"terminalId":"<target-full-id>","command":"<message>"})`. Send it once. Do not use raw terminal commands, spawn a new agent, attach a watcher, or wait for completion when the user only asked you to point the implementer at the review.
6. If the user later asks you to add a follow-up instruction, send one additional concise `terminal.sendCommand` to the same target terminal. Do not re-send the original review unless the user says it was missed. Report accurately that the follow-up was sent now; do not say it was already sent unless it actually was.
7. Do not wait for the target's response unless the user asks you to wait, collect the report, or continue coordinating. If they ask for a later report from the target, either send the instruction now or use the appropriate waiting/agent-answer runbook when the target is actually done or asking.

Confirmation: sending instructions to an existing implementer terminal can change ongoing project work; confirm before sending if the active confirmation tier requires it. A direct user request to send the message is the operational go-ahead.

Report back: which reviewer terminal you read, which implementer terminal you messaged, the gist of what you forwarded, any calibration/follow-up instruction you included, and whether you are waiting for anything.
