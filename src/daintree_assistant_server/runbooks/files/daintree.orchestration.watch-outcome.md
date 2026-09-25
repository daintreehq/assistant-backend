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
  - agent.launch
  - worktree.waitForPullRequest
---
Use when: the user wants to hear about something later, or wants something done at a later time or when a condition holds. Choose a continuation that really exists, or say plainly that none does. Deferred work is never started now.

Availability: Core. Daintree's only wake is a notice when an agent terminal stops working (`notify` on a launch or send, or `terminal.notifyWhenIdle`). It has no timer, scheduler, condition watcher, and nothing that runs after the app or your pane closes. An API-key client gets no notices. Anything more must be a host mechanism you have verified here.

Procedure:
1. Name the ask. A wake when work stops, so you can read and report: a notice. Running something at a time or when a condition holds: scheduled execution. Telling the human: a reminder (a notice types into your prompt, not into the user's attention). Keep the requested delay or condition: a spawn asked for in ten minutes does not spawn now.
2. Wake on stop. If you are about to prompt the agent, `notify` on that launch or send is all it takes. For an agent already working, resolve the exact terminal (`terminal.list`; an agent in this project, not your own pane; a plain shell never reports working) and arm with your step in the note:

```json tool=terminal.notifyWhenIdle
{"terminalId":"<working agent terminal id>","note":"read the build result and report it"}
```

   `armed:false` means it is not working: `state` says where it is, so read it now instead of waiting. The notice fires on the first stop for any reason: finished, a question, an approval, an error, exit or close.
3. Anything else needs a verified host mechanism or an honest no.
   - A time or after-shutdown work: use your host's scheduler only after confirming it exists here, will start a model turn or the command at that time, and can reach this Daintree instance. A new headless session owns none of your terminals and gets none of your notices.
   - A condition ("when CI turns green", "when the PR merges"): a stop notice is not a watcher. What works is a command that waits for the condition and exits (`gh pr checks <number> --watch --repo <owner/repo>`), run where its exit reaches you: a host background command that re-invokes you, or a visible agent the user agrees to launch with `notify` whose task is that wait. `worktree.waitForPullRequest` sees only that a PR exists, for up to 25 seconds.
   - No verified mechanism: say Daintree cannot act or remind at a time or on a condition, and offer what is real (do it now, check now, a stop notice on running work, ask again later). Never arm a notice and call it a timer.
4. Report the mechanism, its terminal id or host handle, what fires it and its limits: no deadline (an agent that never stops never notifies), and it ends with your pane or a Daintree restart. Then end your turn.

On notice or resume:
1. Match the terminal id and echoed note to what you armed. A stop is not the outcome: read the quote against what "done" meant. A build may have failed, an agent may be asking, a wait command may have timed out.
2. A wait command's notice means it ended, not that the condition holds: read its exit status and output before reporting the condition.
3. Working again: the notice was spent on an earlier stop. Re-arm once only if the user still wants to hear.
4. Work a host mechanism started later: revalidate the target and authority, and repeat no effect already visible in its system of record.

Stop and recovery: one notice per pending step; arming again replaces the earlier one. There is no cancel: to stop waiting, drop the step so a late notice does nothing, and tell the user their pane's Stop on the notice chip clears all pending notices. Stopping observation never interrupts, closes or restarts the work.

Report back: what was asked (wake, scheduled run or reminder), the mechanism actually arranged with its id or handle, what fires it and what does not, how long it lives, and anything that could not be arranged and so was not started.
