---
id: daintree.agents.recover-agent
title: Recover a broken agent or spawn
summary: Triage an agent that went wrong — reconcile the launch from what Daintree lists, tell a hang from a bad id, and re-arm observation of an unwatched agent instead of restarting it.
whenToUse: Use when something has gone WRONG with an agent or its spawn — "my agent is stuck", "it has been sitting there for ages", "did that agent even start?", "the spawn failed", "nothing happened when you launched it", "that terminal id does not work", "this agent has no watcher on it", "there is an agent running from earlier that nobody is watching", "is it hung or just slow?", "my agents were cut off when the laptop slept, get them going again". Covers a launch that returned no terminalId or an ambiguous result, a hung or unresponsive agent, a turn cut off by an error (the machine slept, the network dropped) that must be told to continue, a stale or mistyped terminal id, and adopting an orphaned or unsupervised agent. Do not use when the agent is healthy and merely ASKING something — answer it with daintree.agents.answer-waiting-agent; for routine triage of a fleet where nothing is broken use daintree.orchestration.supervise-fleet or daintree.orchestration.status-and-resume; to start fresh work use daintree.edits.spawn-visible-agent; to redirect or stop a healthy agent the user wants on another course use daintree.agents.steer-running-agent; to have a different agent continue its task use daintree.agents.hand-off-task; when the run is over and the user asks why it went wrong use daintree.agents.postmortem-run; when agents are slow because the machine itself is overloaded use daintree.runtime.relieve-resource-contention. Do NOT use when the agent and its launch are both healthy and the user is questioning something YOU did — "why did you not wait", "why did you spawn that" — a complaint about your own handling is not a broken agent, so answer it directly rather than opening a recovery; for a task delegated to another machine's assistant that never came back use daintree.hosts.recover-and-clean-up.
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
optionalTools:
  - agent.listAvailable
  - agent.launch
  - worktree.list
  - terminal.waitUntilIdle
  - terminal.notifyWhenIdle
  - terminal.interruptOwned
  - terminal.sendCommandOwned
---
Recover observation and the task, not the work: one corrective action, then hand it to the user. Never resend the original prompt into a terminal you do not understand.

On a host: call these tools on the agent's machine; delegate any shell step there.

1. Evidence: what you recorded of the launch (agent, `name`, worktree, `requestedId`, time), its result, `terminal.list` (`owned:true`; `location:"trash"` or `"background"` for panels off the grid), and one `terminal.getStatus` with `includeOutput`.
2. Name the shape, then act:
   - Launch reply lost or ambiguous: look it up by `requestedId`; a match on worktree, agent, title and recent `spawnedAt` is only a candidate, and with several ask. Nothing after a second read: unknown. Relaunch under the same identity only when a refusal proves nothing was created, or the user agrees.
   - `launched:false`: usually a same-kind launch still starting; retry once after it has started.
   - Unknown agent or worktree error: correct exactly what it names, checking the listing first.
   - Null destination fields are unknown, not wrong.
   - Startup screen: trust as the foundation says; anything else goes to the user. Then check whether the launch prompt ran before sending anything.
   - Exited, or a CLI error at a shell prompt: report the error with credentials removed. A bare prompt with no error is ambiguous, not grounds to relaunch.
   - Slow: `working` with recent output.
   - Asking or at an approval: not hung; use daintree.agents.answer-waiting-agent.
   - Provider error (rate limit, auth, network): report it; waiting fixes nothing.
   - One turn cut off ("went to sleep mid-response", a dropped connection) with the CLI back at its prompt, unlike a failure that persists: a send reaches the same conversation. Once continuing is authorized, one send (`terminal.sendCommandOwned`, `notify:true, handback:true`) naming where it stopped and asking it to check `git status` first. Two stopped in one checkout: continue only the one that owns its changes; unclear is daintree.orchestration.resolve-task-collisions.
   - Wrong locator (per-entry `error`, id missing from the listing): check for a truncated id, a filter or the trash and resume the same observation. Never relaunch a healthy agent because an id was wrong.
   - Lost ownership: RESOURCE_NOT_OWNED from an owned tool is not evidence the terminal is missing; status reads show whether it exists. Ask for a hand-over or leave the action to the user.
   - Unknown (`source:"pty"`, `unavailableFields`): say exactly what is unknown.
3. Hung or slow: one `terminal.waitUntilIdle` of at most 60 seconds, then report the id, the screen and time since output changed.
4. Interrupt only when the user authorized stopping this turn (`terminal.interruptOwned`); `requested` is not a stop, so read after.
5. Healthy, working and unobserved: do not restart it; arm one notice and end your turn.

```json tool=terminal.notifyWhenIdle
{"terminalId":"<terminal id>","note":"recovered: check <task>"}
```

   `armed:false`: it was not working; classify its returned `state` now. Another coordinator's notices are invisible to you, so never call an agent unsupervised because you hold none for it.

On notice or resume: classify again with the shapes above. A recovered agent's result is still a claim.

Stop and recovery: one corrective action; if it does not hold, stop and report ids and evidence. A second relaunch is a loop. Never close a terminal to fix a failed launch.

Report back: the shape and its evidence; terminal, worktree and launch details; what you recovered and deliberately did not do; what is still running and what will bring you back, or what you need from the user.
