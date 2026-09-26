---
id: daintree.orchestration.workspace-cleanup
title: Tear down a workspace safely
summary: The confirmation gate for teardown — inventory, classify safe/unsafe/unknown, get an explicit go-ahead, then retire observation, close terminals, delete worktrees and close the run's checkpoint in dependency order.
whenToUse: Use ONLY when the user explicitly asks to tear down workspace state after a fan-out, competition or long session — "close these", "clean up", "tidy up the terminals", "shut it all down", "disarm everything", "delete that worktree", "mark those workflows done". A cleanup request in the user's own words is required; never select this for a status question, and never treat it as permission to close anything on your own initiative. Do not use for closing ONE named terminal (daintree.foundation covers that), to stop waiting on a notice with nothing else torn down (daintree.orchestration.watch-outcome), to triage or check on a running fleet (daintree.orchestration.supervise-fleet), to answer "what is running" or "where were we" (daintree.orchestration.status-and-resume), or to judge whether an agent's work is good (daintree.orchestration.verify-agent-work).
risk: system
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - terminal.closeOwned
  - worktree.deleteOwned
optionalTools:
  - terminal.closeMany
  - terminal.interruptOwned
  - terminal.notifyWhenIdle
  - worktree.delete
  - worktree.resource.status
---
A confirmation gate, not permission to tidy up. Never close on your own initiative or widen a narrow request ("close the two that failed") into a sweep. Showing evidence (`git status`, unpushed commits) is fine; judging whether the user's work is finished is not. A cleanup never passes `force`.

Separate actions, each needing its own request:
- Stop observing: no notice cancel exists. Drop the targeted units' pending steps so a late notice does nothing. Nothing stops running.
- Interrupt a turn: `terminal.interruptOwned` keeps panel and conversation.
- Close a panel: ends its process, moves it to the trash. Only panels you created; a hand-over never grants close.
- Delete a worktree: `worktree.deleteOwned`, only one this connection created, after the user confirms. It refuses uncommitted or untracked changes, keeps the branch, closes no terminals, and runs the project's teardown, which can destroy a remote resource.
- Delete a branch: `git branch -d` from the main checkout, only when asked; force or remote deletes need explicit instruction after the user sees what would be lost.

Procedure:

Fast path: the user precisely names terminals you launched in this conversation ("close all three", "close them", "close every agent except Codex" right after launching them, meaning those you launched). Skip steps 2 to 4: no listing, status read or worktree listing. If the last you saw of one was `working` or a question, say so first; otherwise close them all in one call with the ids you hold and verify by `closedIds`:

```json tool=terminal.closeMany
{"terminalIds":["<terminal id>","<terminal id>"]}
```

A sweep, a worktree deletion, or any panel you did not launch takes the full procedure.

1. "Once those finish, close them" authorizes later cleanup, not hurrying the work: arm `terminal.notifyWhenIdle` on each (unless a notice is pending) and close later only after the finish is verified.
2. Inventory fresh: `terminal.list` (also `owned:true`), one `terminal.getStatus` with `includeOutput` over the candidates, `worktree.list`, and the run's record. Per worktree: `git -C <path> status --porcelain`, whether the branch is pushed or merged, its resource status if configured.
3. Classify: authorized; consequences to settle first (still working or asking, dirty files, teardown or a remote resource, the active worktree); not yours; unknown (left running and reported).
4. A precise request is acted on as given: Daintree's delete confirmation is the only confirmation. A broad one gets the proposed set, each item's action and consequences, and what you leave and why, then one question. Never ask twice.
5. Execute in order, skipping empty phases: mark only the targeted units cancelled (keep siblings with pending work advancing); interrupt where asked; close approved panels in one `terminal.closeMany`; delete a worktree only after its panels are closed and the user called it merged or abandoned; delete branches if asked.
6. Refusals are answers, not retry signals. `RESOURCE_NOT_OWNED`: leave it, report it by title. A dirty worktree: list the files and let the user decide; never `git clean`, `rm` or `git worktree remove --force`. A declined confirmation leaves it standing; `CONFIRMATION_TIMEOUT` is neither yes nor no, so ask rather than loop. A panel you could not close keeps its worktree standing.
7. A failed teardown may have changed part of a resource: read back what you can and report the observed state, not "unchanged".
8. Verify: a close by its `closedIds` (a panel still in the trash listing is not a failed close); a delete by the worktree's absence from a successful `worktree.list`.

On notice or resume: for a deferred cleanup, close only terminals whose finish is verified; keep waiting on the rest.

Stop and recovery: never retry a refused or declined action through another route.

Report back: what was closed, deleted, interrupted or no longer observed (with ids); what was left and why; what the user declined or what timed out; any partial teardown with its observed state; which units are still pending.
