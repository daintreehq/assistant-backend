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
  - terminal.close
  - worktree.delete
  - worktree.resource.status
---
Use when: the user has explicitly asked to tear workspace state down: close a set of terminals, stop waiting on agents, delete worktrees or branches, close out a finished run.

This is a confirmation gate, not permission to tidy up. Closing is never your initiative: if you are here because a fan-out finished and the workspace looks busy, report what is running and stop. Never widen a narrow request ("close the two that failed") into a sweep. Showing evidence so the user can decide (a worktree's `git status`, unpushed commits) is fine; judging whether their work is finished is not yours.

Availability: Core for closing and deleting what this connection created; Daintree itself asks the user to confirm every worktree deletion. Branch deletion is your host's `git` (Hybrid). Reading a remote resource's status (`worktree.resource.status`) raises an approval; tearing a resource down on its own is daintree.worktree.manage-resource. Broadcast arming of the user's typed input is not reachable over MCP: report `armed` from `terminal.getStatus` and leave changes to the user. The sidebar assistant has the unscoped `terminal.close` and `worktree.delete` instead of the owned tools; it never passes `force`.

Five different actions, each needing its own request:
- Stop observing. There is no notice cancel: mark the run cancelled in its checkpoint so a late notice does nothing, and tell the user their pane's Stop on the notice chip clears all of its pending notices. Nothing stops running.
- Interrupt a turn. `terminal.interruptOwned` stops the current turn and keeps the panel and its conversation. It is refused on an idle agent.
- Close a panel. `terminal.closeOwned` ends its process; the panel goes to the trash, briefly recoverable. An empty `closedIds` is a failed close.
- Delete a worktree. `worktree.deleteOwned` removes the directory after the user confirms in Daintree. It works only on a worktree this connection created with `worktree.createWithRecipe`: not one from `workflow.startWorkOnIssue`, a launch, the user or an earlier pane. It refuses uncommitted or untracked changes, never deletes the branch, never closes terminals, and runs the project's teardown commands, which can destroy a remote resource.
- Delete a branch. `git branch -d <branch>` from the main checkout, only when asked. It refuses an unmerged branch; force-deleting one, or deleting a remote branch, needs the user's explicit instruction after they have seen what would be lost.

Procedure:
1. Honor prerequisites. "Once those finish, close their terminals" authorizes later cleanup; it does not stop, hurry or replace the work. Record the cohort and the condition in the run's checkpoint, arrange a notice on those terminals (daintree.orchestration.watch-outcome), and close them on a later activation only after their finish is verified. A unit waiting for a planned next step is unfinished, not idle.
2. Inventory fresh, never from memory: `terminal.list`, `terminal.list` with `owned:true` (what this connection can close), one `terminal.getStatus` with `includeOutput` over the candidates, `worktree.list`, and the run's checkpoint for what this task created. For each candidate worktree, read `git -C <path> status --porcelain`, whether its branch is pushed or merged, and its resource status if one is configured.
3. Classify each item:
   - Authorized: the request covers it. The user's own word ("close the lint runner, it's finished") settles that terminal; a `waiting` prompt there is the normal look of finished work.
   - Consequences to resolve: still `working` or waiting on a question or approval; uncommitted or untracked files; teardown commands or a remote resource; the active worktree; anything else the user could not have known.
   - Not yours: absent from the owned listing. The owned tools will refuse it, so it stays with the user.
   - Unknown: cannot be attributed. It is left running and reported, and joins a "close everything" set only if the user names it after seeing it listed.
4. Present, then act. Act on a precise request as given: Daintree's delete confirmation is the only confirmation, so put no question in front of it. A broad request gets the proposed set, each item's action and consequences, and what you are leaving and why, then one question. Surface newly discovered consequences and settle them before touching those items. Never ask twice for the same fully specified action.
5. Execute in this order, skipping empty phases: record the cleanup and tombstone in the checkpoint; interrupt only where the user asked for work to be stopped; close the approved panels, one call each; delete a worktree only after every panel in it is closed and the user called it merged or abandoned; delete branches if asked; close the run's checkpoint.

```json tool=terminal.closeOwned
{"terminalId":"<approved terminal id>"}
```

```json tool=worktree.deleteOwned
{"worktreeId":"<worktree id this session created>"}
```

6. Refusals are answers, not retry signals:
   - Foreign panel (`RESOURCE_NOT_OWNED`): leave it and report it by title; the user can close or delete it in Daintree. Never route around it through the shell, a kill, another agent or another surface.
   - Untracked or uncommitted files refuse the delete: list them from `git status` and let the user commit, stash, or delete it themselves. Never `git clean`, `rm`, `git worktree remove --force`, or a forced delete from another surface.
   - A declined confirmation leaves the worktree standing. `CONFIRMATION_TIMEOUT` is neither yes nor no. Do not reissue the same delete in a loop; ask the user.
   - A failed teardown: report its error, leave that worktree and resource as they are, and carry on with independent items. Unrelated work is never touched to make cleanup succeed.
   - A panel you could not close keeps its worktree standing: never remove a directory under a running process.
7. Verify from state: re-read `terminal.list` and `worktree.list` and confirm each id is gone. A call that returned while the item is still listed is a real result; report it.

Partial requests stay partial: "stop watching those" is observation only, and "disarm everything" is the user's to do in Daintree. If there is nothing to clean, say so and stop.

Report back: what was closed, deleted or stopped (with ids), what was deliberately left and why (still working, not yours, unknown, dirty, the user's own), what the user declined or what timed out, any teardown failure, and the end state read back from the listings.
