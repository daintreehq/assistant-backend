---
id: daintree.orchestration.watch-outcome
title: Watch an outcome unattended
summary: Arrange to be told when running agent work stops — a Daintree notice on a launch, a send or a running terminal — and say plainly when a timed, conditional or after-shutdown trigger has no real mechanism, without starting the deferred work now.
whenToUse: Use when the user wants something to happen LATER rather than now — either to be woken ("run this overnight and wake me", "let me know when the build finishes", "ping me when the PR merges", "remind me in an hour", "keep an eye on it while I'm out") or to have something DONE unattended at a chosen time ("in ten minutes spawn a terminal and run the tests", "in half an hour send npm test to the build terminal", "wait an hour then start the migration", "at 6pm kick off the deploy check"). Any deferred action belongs here, INCLUDING one that spawns an agent — a delayed spawn is scheduled, not immediate (daintree.edits.spawn-visible-agent is for spawning NOW). Also to stop waiting on a notice you arranged. Do not use for a plain read of whether one terminal is still running right now (daintree.foundation), a one-off check of an agent that already finished (daintree.orchestration.verify-agent-work), triage of what is running now (daintree.orchestration.supervise-fleet), a rolling queue of agent work under a concurrency cap even when it has a periodic check (daintree.orchestration.fan-out-work), what happened while you were away (daintree.orchestration.status-and-resume), wholesale teardown (daintree.orchestration.workspace-cleanup), a fixed working time an agent gets starting now, after which it is stopped to report (daintree.orchestration.timebox-attempt), a shell command run now in a visible terminal and read back (daintree.terminals.run-visible-command), or a deploy run now and proven live (daintree.release.deploy-and-verify). A one-off read of CI, checks, reviews or an issue thread is not a watch at all (use daintree.workflow.follow-up-on-issue).
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - terminal.notifyWhenIdle
optionalTools:
  - agent.launch
  - worktree.waitForPullRequest
---
Choose a continuation that really exists, or say plainly that none does. Deferred work is never started now. Daintree's only wake is a notice when an agent terminal stops (`notify` on a launch or send, or `terminal.notifyWhenIdle`). It has no timer, scheduler or condition watcher, and nothing runs after the app or your pane closes.

1. Name the ask: a wake when work stops (a notice); running something at a time or on a condition (scheduled execution); telling the human (a reminder: a notice types into your prompt, not the user's attention). A spawn asked for in ten minutes does not spawn now.
2. Wake on stop. About to prompt the agent: `notify` on that launch or send. Already working: resolve the exact agent terminal (not your own pane; a plain shell never reports working) and arm with your step in the note:

```json tool=terminal.notifyWhenIdle
{"terminalId":"<working agent terminal id>","note":"read the build result and report it"}
```

   `armed:false`: it is not working; read its `state` now. The notice fires on the first stop for any reason.
3. Anything else needs a verified host mechanism or an honest no.
   - A time or after-shutdown: your host's scheduler, only once confirmed to exist, start a turn or command then, and reach this Daintree. A new headless session owns none of your terminals and gets none of your notices.
   - A condition ("when CI turns green"): a command that waits and exits (`gh pr checks <number> --watch --repo <owner/repo>`), run where its exit reaches you: a host background command that re-invokes you, or a visible agent the user agrees to launch with `notify`. `worktree.waitForPullRequest` only sees that a PR exists, for up to 25 seconds.
   - Nothing verified: say Daintree cannot act or remind at a time or on a condition, and offer what is real. Never arm a notice and call it a timer.
4. Report the mechanism, its id or handle, what fires it and its limits (no deadline; it ends with your pane or a Daintree restart), then end your turn.

On notice or resume:
1. Match the terminal and echoed note to what you armed, and read the quote against what "done" meant: a build may have failed, an agent may be asking.
2. A wait command's notice means it ended, not that the condition holds: read its exit status and output.
3. Working again: re-arm once only if the user still wants to hear.
4. Work a host mechanism started: revalidate target and authority, and repeat no effect already visible.

Stop and recovery: arming again replaces the earlier notice. There is no cancel: drop the step so a late notice does nothing, and tell the user the notice chip's Stop clears pending notices. Stopping observation never interrupts, closes or restarts the work.

Report back: what was asked (wake, scheduled run or reminder), the mechanism arranged with its id or handle, what fires it and what does not, how long it lives, and anything not arranged and so not started.
