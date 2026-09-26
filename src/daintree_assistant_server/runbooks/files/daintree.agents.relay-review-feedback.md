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
---
A relay ends when the message is queued, unless the user also wants the implementer's response. Decide that before sending.

1. Resolve source and destination independently from `terminal.list` and one `terminal.getStatus` with `includeOutput`. Several plausible matches for either role: ask; never choose by agent name, focus or a generic title.
2. The destination must be an owned or handed-over agent with no dialog and no user draft. On `RESOURCE_NOT_OWNED`, ask for a hand-over or give the user the message to paste. If it is working, the message lands mid-turn: say so.
3. Bind the review to what it reviewed: the reviewer's worktree and the commit or diff it saw, against where the implementer works now (`git` in each path; HEAD alone does not identify uncommitted work). A mismatch goes in the relay and the report.
4. Read the review: `terminal.readLastMessageOwned` for a Claude Code reviewer's whole last reply, otherwise `terminal.getOutput` with a bounded `maxLines`. If the start scrolled away, say you saw part of it; ask the reviewer for a recap only if the user allowed messaging it. `lastHandback` is a summary, not the review.
5. Write one compact relay: reviewer (agent, terminal id) and revision; verdict; actionable findings with severity, file:line evidence and stated uncertainty; any calibration the user asked for.
6. Send it once with `terminal.sendCommandOwned`; add `waitForReply:true, handback:true` only if the user wants the response, and relay that reply as the implementer's account. Whether findings were fixed is daintree.orchestration.verify-agent-work.
7. If the send is ambiguous (an error or timeout after sending), do not resend: read `terminal.getStatus` with its `submissionToken` and the screen. A blank prompt does not prove the message was never consumed. A later "bring back its answer" observes the same task; never resend the review to get a reply.

Queued is not accepted, and accepted is not fixed.

Report back: the reviewer terminal and how much of the review you saw; the implementer terminal; the revision binding or mismatch; the gist forwarded with calibration; the send's status; the reply if requested; anything pending.
