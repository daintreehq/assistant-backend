---
id: daintree.agents.relay-review-feedback
title: Relay review feedback to an implementer
summary: Get one agent's review findings to another agent that fixes them, reading an existing reviewer's output or launching the reviewer the user named, with calibration and the fixer's reply when asked.
whenToUse: 'Use when review findings from one AGENT must reach another agent that fixes them: read an existing reviewer agent''s output, review or notes and point/send an existing implementer agent to it, or launch the reviewer the user names ("have Codex review this file without changing anything, then pass its findings to Claude and have Claude fix them") and relay its findings to the named fixer; optionally telling the target how to weigh the source or to report what it changed. Do not use for feedback that lives on the forge: PR review threads to fix (use daintree.forge.address-pr-review), or a review decision, failing check or issue thread to fetch and pass on (use daintree.workflow.follow-up-on-issue). Nor to judge whether the reviewed work is actually done or correct (use daintree.orchestration.verify-agent-work), when you review the change yourself and no agent is named to fix it (use daintree.review.review-change), for a correction in the user''s own words to a working agent (use daintree.agents.steer-running-agent), when review and fixes repeat until the reviewer approves (use daintree.orchestration.review-until-approved), for a cohort of agents reviewing each other in rotation (use daintree.review.cross-review-cohort), to spawn a new single agent and ask it a question with no fixer (use daintree.agents.ask-and-relay), to poll several agents for independent opinions (use daintree.agents.poll-pinned-agents), or to run multi-round collaboration among agents (use daintree.orchestration.multi-agent).'
risk: project
optionalTools:
  - agent.launch
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.getOutput
  - terminal.readLastMessageOwned
---
A relay ends when the message is queued, unless the user also wants the fixer's response ("tell me what changed" does). Decide that before sending.

On a host: shell steps run there, per the hosts foundation.

1. Resolve each role. A reviewer or fixer the user named that is not running yet: launch it (steps 4 and 6), and do not review, hash or fingerprint the target yourself. An existing one: from `terminal.list` and one `terminal.getStatus` with `includeOutput`; several plausible matches for either role: ask; never choose by agent name, focus or a generic title.
2. An existing destination must be owned or handed over, with no dialog and no user draft. On `RESOURCE_NOT_OWNED`, ask for a hand-over or give the user the message to paste. If it is working, the message lands mid-turn: say so.
3. An existing reviewer's review is bound to what it saw: its worktree and commit or diff, against where the fixer works now (`git` in each path, on its machine; HEAD alone does not identify uncommitted work). A mismatch goes in the relay and the report.
4. Get the review.
   - New reviewer: launch it once in the worktree under review. The prompt carries the target, the shape (per finding severity, `file:line`, trigger and fix; "nothing found" rather than silence; the revision inspected) and "Read-only: do not create, modify or delete files, and change no external state." Its `reply` is the review; files it changed are a boundary failure to report.

```json tool=agent.launch
{"agentId":"<named reviewer>","name":"Review: <target>","prompt":"<target, shape, read-only line>","waitForReply":true,"handback":true}
```

   - Existing reviewer: `terminal.readLastMessageOwned` for the whole last reply of a Claude Code reviewer you launched or were handed, otherwise `terminal.getOutput` with a bounded `maxLines`. If the start scrolled away, say you saw part of it; ask for a recap only if the user allowed messaging it. `lastHandback` is a summary, not the review.
   A reply that says nothing was found: report that and send nothing. A timeout is not that: the reviewer is still working.
5. Write one compact relay: reviewer (agent, terminal id) and revision; verdict; actionable findings with severity, `file:line` evidence and stated uncertainty; any calibration the user asked for; when a response is wanted, "reply with what you changed for each finding".
6. Deliver once: to an existing fixer with `terminal.sendCommandOwned`, to a new one as the `prompt` of its `agent.launch` in the same worktree. Add `waitForReply:true, handback:true` when the user wants the response, relay the reply as the fixer's account, and check it against `git diff` in that worktree. Whether findings were truly fixed is daintree.orchestration.verify-agent-work.
7. If the send is ambiguous (an error or timeout after sending), do not resend: read `terminal.getStatus` with its `submissionToken` and the screen. A blank prompt does not prove the message was never consumed. A later "bring back its answer" observes the same task; never resend the review to get a reply.

Queued is not accepted, and accepted is not fixed.

Report back: the reviewer terminal and how much of the review you saw; the fixer terminal; the revision binding or mismatch; the gist forwarded with calibration; the send's status; the fixer's reply and the diff if requested; anything pending.
