---
id: daintree.agents.relay-review-feedback
title: Relay review feedback to an implementer
summary: Forward review output from one running agent to another with user-specified calibration and optional follow-up instructions.
whenToUse: Use when the user asks you to read one existing reviewer agent's output, review, or notes and point/send another existing implementer agent to it, optionally telling the target how to weigh the source or to report whether the review was useful. The source is a reviewer AGENT's terminal — feedback that must first be fetched from the forge (a PR's review decision, a failing check, an issue thread) is a different job (use daintree.workflow.follow-up-on-issue). Do not use to judge whether the reviewed work is actually done or correct (use daintree.orchestration.verify-agent-work), to spawn a new single agent and ask it a question (use daintree.agents.ask-and-relay), to poll several agents for independent opinions (use daintree.agents.poll-pinned-agents), or to run multi-round collaboration among agents (use daintree.orchestration.multi-agent).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
optionalTools:
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.notifyWhenIdle
  - worktree.list
---
Use when: the user wants the review or notes from one existing reviewer agent sent to another existing agent that is implementing or fixing the work.

This is a relay: it ends when the message is queued, unless the user also asks for the implementer's response. Decide that before sending.

Availability: Core. Reads work on any terminal; the send needs a terminal you launched or were handed. Your own shell and git bind the review to a revision.

Procedure:
1. Resolve the source and the destination independently, from `terminal.list` and one `terminal.getStatus` over the candidates with `includeOutput`. With several plausible matches for either role, ask; never choose by agent name, focus or a generic title.
2. The destination must be an owned or handed-over agent, safe to type into: no trust, approval or login prompt, no user draft. On `RESOURCE_NOT_OWNED`, ask for a hand-over or give the user the message to paste. If it is working, the message lands mid-turn: say so unless the user wants that.
3. Bind the review to what it reviewed: the reviewer's worktree and the commit or diff it saw, against where the implementer works now (`git` in each path; HEAD alone does not identify uncommitted work). If they differ or the implementer's tree has moved on, say so in the relay and to the user.
4. Read the review: `terminal.readLastMessageOwned` gives a Claude Code reviewer's whole last reply; otherwise `terminal.getOutput` with a bounded `maxLines`, a tail that cannot page back. If the start scrolled away, you have part of it: say so, and ask the reviewer for a recap only if the user allowed messaging it, labelled as a recap. `lastHandback` is a summary, not the review.
5. Write one compact relay: who reviewed (agent, terminal id) and which revision; the verdict; the actionable findings with severity, file:line evidence and stated uncertainty; where the raw notes are, only if the implementer can reach that place; any calibration the user asked for, kept operational.
6. Send it once, with `notify` only if the user wants the response:

```json tool=terminal.sendCommandOwned
{"terminalId":"<implementer terminal id>","command":"<compact relay>"}
```

   If the reply is ambiguous (an error or timeout after sending), do not resend: read `terminal.getStatus` with its `submissionToken` and the screen. A blank prompt does not prove the message was never consumed; resend only on positive evidence it never arrived.
7. A later follow-up from the user is one more concise send. A later "bring back its answer" observes the same task: `terminal.notifyWhenIdle` if it is working, or read it if it has stopped. Never resend the review to attach a notice.

On notice or resume: the notice quotes the implementer's reply; relay it as its account. Whether the findings were actually fixed is daintree.orchestration.verify-agent-work.

Stop and recovery: one relay per request; ambiguous delivery is read, and you do not resend. Queued is not accepted, and accepted is not fixed.

Report back: the reviewer terminal read and how much of the review you saw; the implementer terminal messaged; the revision binding or the mismatch; the gist forwarded with any calibration; the send's status; and anything pending.
