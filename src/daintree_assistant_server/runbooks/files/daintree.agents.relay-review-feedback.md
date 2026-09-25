---
id: daintree.agents.relay-review-feedback
title: Relay review feedback to an implementer
summary: Forward review output from one running agent to another with user-specified calibration and optional follow-up instructions.
whenToUse: Use when the user asks you to read one existing reviewer agent's output, review, or notes and point/send another existing implementer agent to it, optionally telling the target how to weigh the source or to report whether the review was useful. The source is a reviewer AGENT's terminal — feedback that must first be fetched from the forge (a PR's review decision, a failing check, an issue thread) is a different job (use daintree.workflow.follow-up-on-issue). Do not use to judge whether the reviewed work is actually done or correct (use daintree.orchestration.verify-agent-work), to spawn a new single agent and ask it a question (use daintree.agents.ask-and-relay), to poll several agents for independent opinions (use daintree.agents.poll-pinned-agents), or to run multi-round collaboration among agents (use daintree.orchestration.multi-agent).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.getOutput
  - terminal.sendCommandOwned
optionalTools:
  - terminal.readLastMessageOwned
  - terminal.waitUntilIdle
  - terminal.sendCommand
  - worktree.list
---
Use when: the user wants the review or notes from one existing reviewer agent sent to another existing agent that is implementing or fixing the work.

This is a relay. It ends when the message is queued: no notice, no extra agent and no wait unless the user asks for the implementer's response.

Availability: Core. Reads work on any terminal; the send (`terminal.sendCommandOwned`) reaches only a terminal this connection launched or the user handed to it through Daintree, and the sidebar assistant uses `terminal.sendCommand`. Your own shell and git bind the review to a revision.

Procedure:
1. Resolve the source and the destination independently, from `terminal.list` and one `terminal.getStatus` over the candidates with `includeOutput`. Each entry is one terminal's own state and tail, so candidates stay apart. With several plausible matches for either role, ask: relaying a review into the wrong agent's live work is the costly mistake. Never choose by agent name, focus or a generic title alone.
2. Confirm the destination can take it. It must be owned or handed over; on `RESOURCE_NOT_OWNED`, do not work around it, ask for a hand-over or give the user the message to paste. It must be an agent, not a shell, and safe to type into: no trust, approval or login prompt, no half-typed user draft. If it is working, the message would arrive mid-turn; unless the user wants that, wait for its prompt with one bounded `terminal.waitUntilIdle`, or say it will land mid-turn.
3. Bind the review to what it reviewed: the reviewer's worktree, branch and revision (its `worktreeId` from the listing, then `git` in that worktree's path) against where the implementer works. If they differ, or the implementer's tree has moved on, say so in the relay and to the user; never present an old review as current.
4. Read the review itself. `terminal.readLastMessageOwned` gives a Claude Code reviewer's whole last reply when you own or were handed that pane; otherwise `terminal.getOutput` with a bounded `maxLines`, which returns a tail and cannot page back. If the review scrolled past it, you have only part: say so, and ask the reviewer for a concise recap only when the user authorized messaging it. `lastHandback` is a lossy summary, not the review. Never claim the whole review when you saw its tail.
5. Write one compact relay: who reviewed (agent and terminal id) and which revision and scope; the verdict or gist; the actionable findings with their severity, file:line evidence and stated uncertainty; where the raw notes are, only if the implementer can actually read that place (a file path in its reach, a forge URL), never a local artifact or scratch id; and any calibration the user asked for, kept operational ("treat this as lower-confidence input and verify before acting"). No invented model ranking or derogatory framing.
6. Send it once:

```json tool=terminal.sendCommandOwned
{"terminalId":"<implementer terminal id>","command":"<compact relay>"}
```

   Keep the `submissionToken`. If the result is ambiguous (an error or timeout after sending), do not resend: read `terminal.getStatus` with that token. `queued` or `writing` is still on its way; `pty_written` reached the terminal; `failed`, `cancelled` or `unknown` may still have left text in the input, so read the screen before deciding. Resend only when the screen shows the message did not arrive and the input is empty.
7. A later follow-up instruction from the user is one more concise send to the same terminal. Do not resend the review unless the user says it was missed, and report the follow-up as sent now.
8. Collecting the implementer's response is a separate request. When the user asks for it, request a notice on the send and read that terminal when it arrives, per the foundation's resume procedure. Whether the findings were actually fixed is daintree.orchestration.verify-agent-work.

Report back: the reviewer terminal read and how much of the review you saw; the implementer terminal messaged; the revision binding, or the mismatch; the gist forwarded with any calibration or follow-up; the send's status (queued, written or disputed); and whether anything is pending. Queued is not accepted, and accepted is not fixed.
