---
id: daintree.agents.hand-off-task
title: Hand a task over to another agent
summary: Move an unfinished task from one agent to a different agent or a fresh session in the same checkout — stop the first, build a handoff from git and its own words (done, uncommitted, decided, tried and failed, next), and brief the successor to continue rather than restart.
whenToUse: 'Use when a task an agent has STARTED must be finished by a DIFFERENT agent or a fresh session, in the same checkout: "Claude hit its usage limit, have Codex finish it", "its context window is used up, start a fresh session that carries on where it left off", "it is going in circles, give it to Claude", "swap it for Gemini and keep going", "take over from that agent and finish it yourself". The deliverable is the successor working in the same worktree from a handoff brief checked against git, with the predecessor stopped and left open. Do not use to correct an agent that keeps the task (daintree.agents.steer-running-agent), to reopen the SAME closed session (daintree.orchestration.status-and-resume), to move the same agent to another worktree (daintree.worktree.relocate-agents), to rescue a hung or failed spawn (daintree.agents.recover-agent), to read how much usage is left (daintree.agents.check-usage-limits), to send review findings to a fixer (daintree.agents.relay-review-feedback), to start new work from scratch (daintree.edits.spawn-visible-agent), to compact or clear the same agent''s context (daintree.agents.run-agent-command), to re-plan several agents around urgent work (daintree.orchestration.reprioritize-work), to replace the coordinator of a multi-agent run (daintree.orchestration.hand-off-orchestration), or to carry only the useful part of a failed attempt into a new branch (daintree.orchestration.salvage-failed-attempt).'
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - agent.launch
optionalTools:
  - agent.listAvailable
  - terminal.interruptOwned
  - terminal.notifyWhenIdle
  - terminal.readLastMessageOwned
  - terminal.getOutput
  - terminal.sendCommandOwned
---
The successor continues in the SAME worktree: the uncommitted work lives only there, so no other agent may be writing in it. Failures to prevent: the successor re-running dead ends, reverting deliberate choices, blaming itself for failures that already existed, or "starting clean" with a stash or reset.

Procedure:
1. Predecessor: its `terminal.list` row (agent, `worktreeId`), path and branch from `worktree.list`, and one `terminal.getStatus` with `includeOutput:{"lines":50}`. Why: a limit or auth screen, a full context, looping, or the user's choice. Its last reply says the whole task is done: nothing to hand off; say so, launch nothing.
2. Stop it before anything starts. A limit screen or an idle prompt is already stopped. `working`: "switch now" means `terminal.interruptOwned`, confirmed from its screen as daintree.agents.steer-running-agent says; otherwise arm `terminal.notifyWhenIdle` with note "handoff: predecessor stopped" and end your turn. Not yours (`RESOURCE_NOT_OWNED`) and still working: ask the user to stop it; launch nothing meanwhile.
3. Other writers: `terminal.getStatus` with that `worktreeId`, and again with `location:"background"`. Another agent `working` in this worktree: hold the successor, report its terminal id, and stop nothing unasked.
4. State from disk, not from its summary, every command as `git -C "<path>"`: `rev-parse HEAD`, `status --short`, `diff --stat HEAD`, and `log --oneline <base>..HEAD` only when the base it branched from is known (unknown: say so, never guess a range). Never stash, reset, commit or clean.
5. Intent: the task as authorized (your conversation or the launch prompt, plus every later correction), and its recent reasoning: `terminal.readLastMessageOwned` (`messageIndex` 0 to 3) on a Claude Code pane; `unavailable` (not a refusal) or another agent: `terminal.getOutput` (`maxLines` about 200). Only a predecessor that can still answer gets ONE `terminal.sendCommandOwned` with `waitForReply:true, handback:true`: "Stop work and change no files. In prose: what is done, what is half-done, decisions and why, approaches you tried and abandoned and why, open questions, the next step." Never ask one at its usage limit. Task or acceptance unclear (no prompt or answer of its own states it): ask the user before step 6; never infer it from its screen.
6. The brief, as the successor's prompt: the current task with its acceptance, corrections folded in and completed steps marked; worktree path, branch, HEAD; uncommitted files (step 4); decisions and their reasons; "do not retry: <approach> because <reason>"; the predecessor's claims marked unverified; the next step; "Read this repository's agent instructions yourself" plus any rule the user gave only in conversation. End it with "The tree holds the previous agent's uncommitted work: build on it. Do not reset, stash, check out or restart from scratch. First run git status and confirm it matches this brief, then run the relevant tests once and record which already fail before you change anything."
7. Successor: the agent the user named; else one from `agent.listAvailable` with `availability:"ready"`, never the provider that just hit its limit (a fresh session of the same agent suits a full context). "Finish it yourself" from the user: no launch; do the task now from the same brief, under the runbook that owns that work, and this runbook ends here. Otherwise launch once in the same worktree:

```json tool=agent.launch
{"agentId":"codex","name":"Codex: webhook retries (handoff)","worktreeId":"<predecessor worktree id>","prompt":"<brief from step 6, with its closing lines>","notify":true,"handback":true}
```

   Its `worktreeId` null: read it from `terminal.list` before claiming the takeover. A different worktree: recover as daintree.edits.spawn-visible-agent step 7 says; never leave it working there.
8. Leave the predecessor open: its session resumes after a limit resets. End your turn.

On notice or resume, by what you were waiting for: the predecessor's stop (step 2): reclassify its screen, then step 3. The successor: its first report against the brief; a listed dead end retried, or a recorded decision reverted without reason, gets one correction with `notify`; anything worse goes to the user. Its finish: verify as daintree.edits.spawn-visible-agent says, keeping failures that predate the handoff apart.

Stop and recovery: one handoff question, one successor launch. A successor that cannot start (`missing-cli`, `launched:false`): report it, keeping the brief for the user.

Report back: why it was handed off; predecessor terminal id, its state and that it is kept; successor agent, terminal, worktree path and the HEAD you verified; the brief's verified and claimed parts; failures that predate the handoff; what is still running and what will bring you back.
