---
id: daintree.orchestration.watch-outcome
title: Watch an outcome unattended
summary: Arrange to be told when running agent work stops — a Daintree notice on a launch, a send or a running terminal — and say plainly when a timed, conditional or after-shutdown trigger has no real mechanism, without starting the deferred work now.
whenToUse: Use when the user wants something to happen LATER rather than now — either to be woken ("run this overnight and wake me", "let me know when the build finishes", "ping me when the PR merges", "remind me in an hour", "keep an eye on it while I'm out") or to have something DONE unattended at a chosen time ("in ten minutes spawn a terminal and run the tests", "in 10 minutes' time spawn a terminal and run the test suite", "in half an hour send npm test to the build terminal", "wait an hour then start the migration", "at 6pm kick off the deploy check"). Any deferred action belongs here, INCLUDING one that spawns an agent — a delayed spawn is a scheduled action, not an immediate one, so do not fall through to daintree.edits.spawn-visible-agent just because the verb is "spawn" — that runbook is for spawning NOW. Also to stop waiting on a notice you arranged. Do not use for a plain read of whether one terminal is still running right now (daintree.foundation), a one-off check of an agent that already finished (daintree.orchestration.verify-agent-work), triage of what is running now (daintree.orchestration.supervise-fleet), a rolling queue of agent work under a concurrency cap even when it has a periodic check (daintree.orchestration.fan-out-work), what happened while you were away (daintree.orchestration.status-and-resume), or wholesale teardown (daintree.orchestration.workspace-cleanup). A one-off read of CI, checks, reviews or an issue thread is not a watch at all (use daintree.workflow.follow-up-on-issue).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.notifyWhenIdle
optionalTools:
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - worktree.waitForPullRequest
  - forge.getCIStatus
  - forge.getPR
---
Use when: the user wants to hear about something later, or wants something to happen at a later time or when a condition holds. Your job is to choose a continuation that really exists, or to say plainly that none does. It is never to do the deferred work now.

Availability: Core, in an agent pane with a Daintree connection. Daintree offers exactly one way to be woken later: a notice when an agent terminal stops working (`notify` on a launch or send, or `terminal.notifyWhenIdle` for work already running). It has no timer or scheduler, no watcher for output text, CI, reviews or PR state, nothing that runs after the app or your pane closes, and no way to list or cancel a notice. An API-key client gets no notices at all. Anything beyond a stop notice must be a mechanism of your own host that you have verified here. `forge.getCIStatus` and `forge.getPR` are one-off reads that raise an approval in a core pane; your host's `gh` is the alternative.

Procedure:
1. Name what is being asked. Three different things hide behind "later":
   - Wake you when work stops, so you can read it and report: a notice.
   - Run a command or start work at a time, or when a condition holds: scheduled execution.
   - Tell the human at a time or on an event: a reminder. A notice types into your pane, not into the user's attention; they hear only what you report after it, and only if they look at your pane.
   Keep the requested delay or condition. Deferred work is never started now "to be safe": a spawn asked for in ten minutes does not spawn now.
2. Wake-on-stop, the supported path.
   - You are about to prompt the agent: request `notify` on that launch or send, in the runbook that does it. Nothing else to arm.
   - It is already running: resolve the exact terminal with `terminal.list` and `terminal.getStatus`. It must be an agent terminal in this project and not your own pane; a plain shell never reports working or idle, so it cannot be watched this way. When the follow-up spans activations, record the pending step and what "done" means (the foundation's checkpoint). Then arm, putting the run and step in the note, which comes back in the notice:

```json tool=terminal.notifyWhenIdle
{"terminalId":"<working agent terminal id>","note":"run <run id>: read the build result and report it"}
```

   `armed:true`: tell the user what you armed and its limits, then end your turn. `armed:false`: it is not working now and `state` says where it is. It may already have finished, so read it now instead of waiting.
   - The notice fires once, the first time the agent stops working for any reason: finished, asking a question, at an approval, at an error, exited or closed.
3. Anything else needs a verified host mechanism, or an honest no.
   - A time ("at 6pm", "in ten minutes", "remind me in an hour") or after-shutdown work ("overnight while the laptop is closed"): Daintree has no timer. Use your host's scheduler only after confirming it exists in this environment, that it will really start a model turn (or run the command) at that time, and that what it starts can reach this Daintree instance with the authority needed. A cloud routine cannot see local panes. A new headless session is a new connection: it owns none of your terminals and receives none of your notices.
   - A condition ("when CI turns green", "when the PR merges", "when the output says ERROR"): Daintree has no condition watcher, and a stop notice is not one. What works is a command that waits until the condition holds and then exits (for example `gh pr checks <number> --watch --repo <owner/repo>`), run where its exit reaches you: your host's background command, if it re-invokes you when the command ends, or a visible agent the user agrees to launch, with `notify`, whose task is that wait. The notice then means the command ended; read why before reporting the condition. `worktree.waitForPullRequest` only sees that a PR exists, for up to 25 seconds.
   - When no verified mechanism exists, say so: Daintree cannot run anything or remind anyone at a time or on a condition. Offer what is real: do it now, check right now, a stop notice on work already running, or the user asking again later. Do not start the deferred work, and never arm a notice and call it a timer.
4. Report what you arranged, then end the turn: the mechanism, the terminal id or host handle, what fires it, and its limits. A notice has no deadline (an agent that never stops never sends one), ends if your pane exits or Daintree restarts, and is cleared only by the user's Stop on your pane's notice chip, which clears all of them.

On notice or resume:
1. Follow the foundation's resume procedure, correlating by the terminal id and the note echoed in the line.
2. A stop is not the outcome. Read the terminal and apply the acceptance you recorded: a build that stopped may have failed, an agent that stopped may be asking a question, a wait command may have timed out. Report what you observed with its evidence, never just "it finished".
3. If the terminal is working again, the notice was spent on the earlier stop. Arm once more only if the user still wants to hear, with a fresh note.
4. Work that a host mechanism started later: revalidate the target and the user's authority before acting, and do not repeat an effect already visible in its system of record.

Stop and recovery: one notice per pending step. Arming again for the same terminal replaces the earlier notice, and a pane holds at most 32. There is no cancel: to stop waiting, record a tombstone so a later notice produces nothing, and tell the user their pane's Stop clears every pending notice. Stopping observation never interrupts, closes or restarts the work; those are separate requests. A deadline written in a checkpoint wakes nothing: if the user needs to know by a time, say that only a verified timeout mechanism can promise it.

Report back: what the user asked for (a wake, a scheduled run or a reminder), the mechanism actually arranged with its terminal id or handle, what fires it and what does not, how long it lives, and plainly anything that could not be arranged and was therefore not started.
