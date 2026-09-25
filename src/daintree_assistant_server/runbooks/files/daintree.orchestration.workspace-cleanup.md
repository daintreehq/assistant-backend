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
  - terminal.interruptOwned
  - terminal.notifyWhenIdle
  - worktree.delete
  - worktree.resource.status
---
Use when: the user has explicitly asked to tear workspace state down: close a set of terminals, stop waiting on agents, delete worktrees or branches, close out a finished run.

This is a confirmation gate, not permission to tidy up. Never close on your own initiative, and never widen a narrow request ("close the two that failed") into a sweep. Showing evidence (a worktree's `git status`, unpushed commits) is fine; judging whether the user's work is finished is not.

Availability: Core for what this connection created; Daintree asks the user to confirm every worktree deletion. Branch deletion is your host's `git`. `worktree.resource.status` raises an approval; tearing a resource down alone is daintree.worktree.manage-resource. A cleanup never passes `force`.

Five separate actions, each needing its own request:
- Stop observing: no notice cancel exists. Drop the targeted units' pending steps so a late notice does nothing; the user's Stop on the pane's notice chip clears all of them. Nothing stops running.
- Interrupt a turn: `terminal.interruptOwned` keeps the panel and conversation; refused on an idle agent.
- Close a panel: `terminal.closeOwned` ends its process and moves the panel to the trash, briefly recoverable. Only panels you created: a hand-over never grants close, even though `terminal.list` with `owned:true` shows handed-over terminals.
- Delete a worktree: `worktree.deleteOwned`, only one this connection created with `worktree.createWithRecipe`, after the user confirms. It refuses uncommitted or untracked changes, keeps the branch, closes no terminals, and runs the project's teardown, which can destroy a remote resource.
- Delete a branch: `git branch -d <branch>` from the main checkout, only when asked; force or remote deletes need explicit instruction after the user sees what would be lost.

Procedure:
1. Prerequisites. "Once those finish, close them" authorizes later cleanup, not stopping or hurrying the work: arm `terminal.notifyWhenIdle` on each (unless a notice is already requested), and close on a later activation only after the finish is verified.
2. Inventory fresh: `terminal.list`, `terminal.list` with `owned:true`, one `terminal.getStatus` with `includeOutput` over the candidates, `worktree.list`, and the run's record of what it created. Per candidate worktree: `git -C <path> status --porcelain`, whether the branch is pushed or merged, and its resource status if configured.
3. Classify: authorized (the request covers it; a finished agent at its prompt looks `waiting`); consequences to settle first (still working or asking, dirty files, teardown or a remote resource, the active worktree); not yours (you did not create it); unknown (left running and reported, joining a "close everything" set only if the user names it).
4. Present, then act. A precise request is acted on as given: Daintree's delete confirmation is the only confirmation. A broad one gets the proposed set, each item's action and consequences, and what you leave and why, then one question. Never ask twice for the same action.
5. Execute in order, skipping empty phases: mark only the targeted units cancelled (tombstone the whole run only when the whole run is being closed, and keep siblings with pending work advancing; a completed unit stays completed even when its panel closes); interrupt where asked; close approved panels, one call each; delete a worktree only after its panels are closed and the user called it merged or abandoned; delete branches if asked.

```json tool=terminal.closeOwned
{"terminalId":"<approved terminal id>"}
```

```json tool=worktree.deleteOwned
{"worktreeId":"<worktree id this session created>"}
```

6. Refusals are answers, not retry signals. `RESOURCE_NOT_OWNED`: leave it and report it by title. A dirty worktree: list the files and let the user commit, stash or delete; never `git clean`, `rm` or `git worktree remove --force`. A declined confirmation leaves it standing; `CONFIRMATION_TIMEOUT` is neither yes nor no, so ask rather than loop. A panel you could not close keeps its worktree standing.
7. A failed teardown may have changed part of a resource: read back what you can (`worktree.resource.status`, `worktree.list`) and report the observed state, not "unchanged"; carry on with independent items.
8. Verify: a close is proven by its `closedIds`, and the panel may remain in the trash listing (`location:"trash"`), which is not a failed close. A delete is proven by the worktree's absence from a successful `worktree.list`.

On notice or resume: for a deferred cleanup, check each named terminal's quote and state; close only those whose finish is verified, and keep waiting on the rest.

Stop and recovery: never retry a refused or declined action through another route; a late notice for a cancelled unit does nothing.

Report back: what was closed, deleted, interrupted or no longer observed (with ids); what was left and why (working, not yours, unknown, dirty); what the user declined or what timed out; any partial teardown with its observed state; and which units of the run are still pending.
