---
id: daintree.orchestration.timebox-attempt
title: Give an agent a fixed time, then stop it and get a report
summary: Launch an agent on a "try it for N minutes" task, hold a wait bounded by the time box, interrupt it at the deadline if it is still working, and collect and check a report on how far it got.
whenToUse: 'Use when an agent gets a fixed amount of TIME on a task and is stopped at the deadline with a report of how far it got — "give it 20 minutes max", "try this for ten minutes, then tell me where it got", "give Codex a quarter of an hour on the upgrade and stop it if it isn''t done", "time-box this attempt". The clock bounds how long it works, starting now. The deliverable at the deadline: finished early, or stopped with its progress report and the partial work checked on disk, the panel left open to continue. A Remote Host''s assistant given its deadline in another assistant''s brief follows this too. Do not use when the time says when to START ("in 20 minutes spawn…") or when to be told it finished (use daintree.orchestration.watch-outcome), for an open-ended task with no time bound (use daintree.edits.spawn-visible-agent), to redirect or stop an agent that is already working with no time box (use daintree.agents.steer-running-agent), for an agent that is stuck or broken (use daintree.agents.recover-agent), for several approaches tried in parallel (use daintree.orchestration.spike-options), or for time-boxed work this Shell sends to other machines (use daintree.hosts.timebox-swarm).'
risk: project
requiredTools:
  - agent.launch
  - terminal.getStatus
  - terminal.interruptOwned
  - terminal.sendCommandOwned
optionalTools:
  - terminal.waitUntilIdle
  - worktree.createWithRecipe
  - worktree.waitUntilReady
---
The held wait is what fires at the deadline: Daintree has no timer, and a foreground `sleep` is no substitute.

Procedure:
1. Size the box. One held wait covers up to 30 minutes (`waitSeconds` max 1800: the box plus two minutes for the agent's report). For a longer box, or a host that cannot hold a call that long, keep holding after the launch wait with `terminal.waitUntilIdle` (`timeoutMs` 60000, its cap) until the deadline, or use a host background command that exits at the deadline and re-invokes you, if your host has one. Neither possible: tell the user before launching that it is best effort: the agent is asked to stop by its own clock, nothing enforces it, and you check at their next message.
2. Prepare. Edits go in the worktree the user named; for a throwaway attempt while their checkout holds changes of their own, a new worktree (`worktree.createWithRecipe` with a new branch, `worktree.waitUntilReady` with its `worktreeId` until `ready`; otherwise report it, launch nothing). Baseline there: `git rev-parse HEAD` and `git status --porcelain`. Only then start the clock: `date`, and the deadline as a clock time with date and timezone. Tell the user your turn is held for the box.
3. Launch once, with the deadline in the prompt: "You have until 14:35 (check `date`). Work in small steps that leave the tree working. At the deadline stop, even if unfinished, and reply with: what is done, what is not, what you would do next, your confidence, and any file left half-edited." `waitSeconds`: seconds to the deadline plus 120, at most 1800.

```json tool=agent.launch
{"agentId":"<agent>","name":"Timebox 20m: <task>","worktreeId":"<worktree id>","prompt":"<task, deadline, report shape>","waitForReply":true,"handback":true,"notify":true,"waitSeconds":1320}
```

   `notify` stays on so that, if the host cuts the call short, the agent's stop still wakes you.
4. Read the outcome. After every return compare `date` with the deadline.
   - A reply before the deadline: finished early, or stopped to ask. A question inside the task's scope: answer with `terminal.sendCommandOwned`, `waitForReply:true, handback:true, notify:true`, `waitSeconds` the time left plus 120. Outside it: the attempt ends there; report the question.
   - `outcome:"timeout"` before the deadline is a wait cut short, not the box ending: keep holding as in step 1 on the same terminal; never resend the task.
   - Past the deadline: `terminal.getStatus` (`terminalIds:[<id>]`, `includeOutput:{"lines":30}`); if `agentState` is `working`, call `terminal.interruptOwned`: text typed to a busy agent is not read until its turn ends. Its `requested` is not a stop, and a refusal is not proof it stopped; confirm with `terminal.waitUntilIdle` (`timeoutMs` 20000). Stopped means `waitingReason` `prompt`; `question`, `approval` or `error` is a blocked agent to report; `trackingState` `closed` or `unknown` is gone, not idle. Still working: one more interrupt, then report that the box was not enforced; never close it.
   - The call failed, or returned early with neither a reply nor a timeout: that is the host's limit, not the deadline. Tell the user nothing fires at the deadline now except the agent's own clock and the notice when it stops, and end your turn.
5. The report. A reply that already holds the report's parts is the report. Otherwise, once the agent is at its prompt, one `terminal.sendCommandOwned` "Time is up. Do not continue or start anything new. Reply with: done, not done, next steps, confidence, and any half-edited file." with `waitForReply:true, handback:true, waitSeconds:300`; nothing by then: report it unavailable. A notice about the stop you caused may arrive; it is not the report.
6. Verify the partial work yourself against the baseline: `git diff <baseline HEAD>`, `git status --porcelain` and new untracked files, setting aside what the baseline already held, and the relevant test in that worktree. An interrupt mid-edit can leave a half-applied file: name it. With the stop unconfirmed, what you saw is provisional.

On notice or resume (only after step 4's early return): compare the time with the deadline. Before it, treat the notice as a reply in step 4. Past it: step 4's past-the-deadline branch, then steps 5 and 6.

Stop and recovery: one launch, at most two interrupts, one report request. Never close, relaunch or discard its changes to enforce the box; "keep going" is a send to the same terminal, with a new deadline held the same way or `notify:true` for no box.

Report back: the box, its start and deadline times and when the stop was seen; finished early, stopped, stopped late, blocked or unconfirmed, and how you confirmed it; the agent's report as its claim; what you verified on disk and by test; half-edited files; the terminal and worktree left open.
