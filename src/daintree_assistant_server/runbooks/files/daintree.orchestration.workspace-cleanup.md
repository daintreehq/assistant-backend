---
id: daintree.orchestration.workspace-cleanup
title: Tear down a workspace safely
summary: The confirmation gate for teardown — inventory, classify safe/unsafe/unknown, get an explicit go-ahead, then cancel supervision, close terminals, delete worktrees and settle the ledger in dependency order.
whenToUse: Use ONLY when the user explicitly asks to tear down workspace state after a fan-out, competition or long session — "close these", "clean up", "tidy up the terminals", "shut it all down", "disarm everything", "delete that worktree", "mark those workflows done". A cleanup request in the user's own words is required; never select this for a status question, and never treat it as permission to close anything on your own initiative. Do not use for closing ONE named terminal or cancelling ONE watcher (daintree.foundation covers that), to cancel or revoke supervision with nothing else torn down (daintree.orchestration.watch-outcome), to triage or check on a running fleet (daintree.orchestration.supervise-fleet), to answer "what is running" or "where were we" (daintree.orchestration.status-and-resume), or to judge whether an agent's work is good (daintree.orchestration.verify-agent-work).
confirmation: "Tearing down parts of the workspace. The aim is to agree exactly what is in scope before closing anything, because a quiet terminal is not the same as a finished one."
risk: system
requiredTools:
  - context.snapshot
  - agentTask.list
  - agentTask.spawnForEdits
  - worktree.list
  - worktree.getCurrent
  - watcher.list
  - watcher.cancel
  - async.list
  - async.cancel
  - timer.list
  - timer.cancel
  - workflow.list
  - workflow.update
  - queue.digest
  - queue.resolve
  - user.askMultipleChoice
  - terminal.close
  - terminal.disarm
  - terminal.disarmAll
  - scratch.drop
  - tool.search
  - daintree.call
---
Use when: the user has EXPLICITLY asked to tear workspace state down — close a set of terminals, cancel watchers or async waits, disarm the fleet, delete a worktree, settle stale ledger rows.

This is a CONFIRMATION GATE, not permission to tidy up. It does not soften the rule the rest of the catalog leans on: CLOSING IS NEVER YOUR INITIATIVE. terminal.close irreversibly ends the agent or process inside the terminal, and worktree deletion removes a working tree — both are reachable only through a request the user actually made. If you arrived here without one, because a fan-out finished and the workspace looks messy, the correct action is to report what is running and STOP. Never offer cleanup as a favour, and never widen a narrow request ("close the two that failed") into a general sweep.

Non-goals: you are not an engineer here. Do not merge, commit, rebase, or read a diff to decide whether work is finished — that judgment belongs to the user. You may SHOW evidence so they can decide (worktree status, `daintree.call worktree.compareDiff`); you may not form the opinion for them. If work must land before a worktree can go, spawn an agent with agentTask.spawnForEdits, verify the outcome, and come back to teardown afterwards.

Procedure:

1. INVENTORY FIRST — never propose from memory or from last turn's roster. In one batch: `context.snapshot` (terminals, worktrees, open attention queue), `worktree.list({})` and `worktree.getCurrent({})`, `watcher.list({})`, `async.list({})`, `timer.list({})`, `workflow.list({"status":"active"})`, `queue.digest({})`. Add `agentTask.list({"scope":"session"})`: the spawn sagas record which terminalIds and watcherIds THIS session actually created, and that is your only reliable attribution evidence. Copy every id verbatim.

2. CLASSIFY every item safe / unsafe / unknown.
   - SAFE: it appears in this session's spawn sagas as yours AND its agentState is `exited` — or the user named the id outright.
   - UNSAFE: agentState `working` or `waiting`, or any terminal you did not spawn and the user did not name. IDLE IS NEVER PROOF A TERMINAL IS DISPOSABLE: an agent reads waiting before it starts, while paused, and when its window is backgrounded, and a quiet tab may be the user's own shell, a dev server, or a live session. Also unsafe: the current worktree, a worktree with uncommitted work, and a watcher attached to a workflow row still `active`.
   - UNKNOWN: anything you cannot attribute. Unknown DEFAULTS TO LEAVE RUNNING and is reported as such. It never rides along inside a "close everything" set unless the user names it after seeing it listed.

3. PRESENT, THEN GET AN EXPLICIT GO-AHEAD. Before touching anything, show the proposed teardown set — each id, what it is, why it is safe — plus the items you are deliberately leaving and why. For a finite decision use `user.askMultipleChoice({"question":"How much of this should I tear down?","options":["Everything listed","Only the terminals I spawned this session","Cancel supervision but leave the terminals open","Nothing — leave it all"]})`. Call it ALONE, never in the same batch as another tool, and write options as plain text with no A/B/C prefixes (the client assigns letters). If the request is already precise ("close t-1 and t-2"), skip the question and act on exactly those ids. An unsafe item joins the set only when the user names it AFTER you flagged the risk.

4. EXECUTE IN DEPENDENCY ORDER. The order is the substance of this runbook — teardown out of order leaves the workspace worse than it was.
   a. SUPERVISION FIRST. `watcher.cancel({"id":"<watcherId>"})` for live watches, `async.cancel({"asyncId":"asy_…"})` for stale waits, `timer.cancel({"id":"<timerId>"})` for scheduled timers. Why first: an async wait is pinned to the EXACT terminalIds it was issued with — a frozen list, not a live set — so closing a watched terminal first leaves a wait that may not fire until its deadline expires and then wakes you about terminals that no longer exist; a live watcher over a closed terminal can post a spurious completion or failure into the attention queue. `async.cancel` only stops the runtime polling: it kills nothing and closes nothing, which makes "stop tracking that" satisfiable without any destruction at all.
   b. ARMING, if the fleet was left armed. `terminal.disarmAll({})` clears the whole broadcast set; `terminal.disarm({"terminalId":"…"})` drops one. Both confirm and report the resulting armed set, so the change is never silent. Do this before closing, so the user's keystrokes stop being routed at a terminal you are about to trash.
   c. TERMINALS. ONE `terminal.close({"terminalIds":["…","…"]})` over the approved cohort, not a call per id — a single confirmation for a set the user already approved. Only approved ids. Never reach for `terminal.kill` through the escape hatch: it deletes permanently instead of trashing.
   d. WORKTREES. There is no typed wrapper — use `daintree.call {"name":"worktree.delete","arguments":{"worktreeId":"…"}}`, the system-tier escape hatch. The other three arguments are `force`, `deleteBranch` and `closeTerminals`, all optional booleans. `force` is the one to be careful with: the delete REFUSES by default when the tree has uncommitted or untracked changes, and forcing it past that destroys them irreversibly with no copy anywhere — so send `force` only when the user has been shown what is uncommitted and asked for it anyway, never to clear a refusal you hit. The main worktree cannot be deleted at all. Pass the identifier exactly as `worktree.list` reported it. Delete only worktrees the USER called merged or abandoned, and only after the terminals living in them are closed: removing a directory out from under a running shell corrupts that session and can fail outright.
      This action is confirmation-gated in Daintree itself: unless the user has an automation grant covering it in Daintree's own settings, a dialog opens in the app and the turn waits for a human. That gate is NOT yours to skip: it is enforced before the action runs, an auto-approve setting on the client side does not reach it, and a timeout or a decline is a real answer. Report a declined or timed-out delete as left standing and move on — never re-issue the same delete to try for a different outcome.
   e. LEDGER. `workflow.update({"id":"<id>","status":"done"})` — or `"cancelled"` — for rows whose real work is over. Pass ONLY `id` plus `status`: provided array fields REPLACE the stored arrays, so a stray `notes` or `terminalIds` silently wipes that part of the record.
   f. INBOX. `queue.resolve({"id":"<eventId>"})` for items you have actually reported to the user, not merely read. Leave open anything still awaiting them.
   g. SCRATCH. `scratch.drop({"storeId":"…"})` for stores whose task is finished, freeing session slots. Never drop a store an in-flight step still reads.
   Skip any phase with nothing in it. A confirmation the user declines is not an error: record it as left running and continue with the rest.

5. VERIFY FROM STATE, NOT ASSUMPTION. Re-read `context.snapshot`, plus `watcher.list` / `async.list` / `timer.list` and `worktree.list({})` for whatever you touched, and confirm each id is genuinely gone. A call that returned without error while the item is still listed is a real result — report it instead of claiming success.

Notes:
- Never tear down something you are still using. If a terminal belongs to a wait you are about to issue, or a worktree hosts work you have not verified yet, teardown comes after the report, on a later, fresh request.
- Partial requests stay partial. "Stop tracking those" means step 4a only; "disarm everything" means step 4b only. Do not escalate a supervision request into closing terminals.
- If the inventory turns up nothing to clean, say so and stop. Do not manufacture a teardown set to look useful.

Report back: what was torn down (terminal ids closed, watchers/async waits/timers cancelled, worktrees deleted, ledger rows settled, queue items resolved), what was deliberately LEFT running and why (still working, unattributed, or the user's own), anything the user declined at a confirmation, and the verified end state read back from the refreshed roster.
