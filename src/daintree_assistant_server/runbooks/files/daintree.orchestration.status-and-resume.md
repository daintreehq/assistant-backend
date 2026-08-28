---
id: daintree.orchestration.status-and-resume
title: Status sweep, ledger hygiene and session resume
summary: Reconstruct what is in flight across turns and sessions from the workflow ledger, live supervisors and session history — then close the stale ledger rows nothing else will.
whenToUse: Use when the user asks where were we, what was I working on, catch me up, what is still open or in flight, what happened while I was away, or to pick up / resume something from an earlier session. Also for the workflow list or ledger itself — why it still shows finished work as active, tidying stale rows, recording a PR onto a row — and for after-the-fact forensics over the audit log. Do not use for a plain what-exists-now read of terminals, worktrees or queues (daintree.foundation), to triage the agents running this minute (daintree.orchestration.supervise-fleet), to set up a watcher or timer that wakes you later (daintree.orchestration.watch-outcome), to verify one agent's output (daintree.orchestration.verify-agent-work), to close terminals or delete worktrees (daintree.orchestration.workspace-cleanup), or to start an issue (daintree.workflow.start-work-on-issue).
confirmation: "Picking up where the work was left. The aim is an honest picture of what is still live across sessions, since a record left open is not evidence that anything is still running."
risk: project
requiredTools:
  - context.snapshot
  - workflow.list
  - workflow.get
  - workflow.update
  - terminal.focus
  - watcher.list
  - async.list
  - timer.list
  - queue.digest
  - queue.resolve
  - agentTask.list
  - agentSessionHistory.list
  - agentTask.spawnForEdits
  - agentTask.superviseTerminal
  - git.getProjectPulse
  - forge.listPRs
  - terminal.extract
  - terminal.extract.json
  - artifact.read
  - audit.export
  - daintree.call
---
Use when: the user wants the cross-turn, cross-session picture — where were we, what is still in flight, resume that session — or when the workflow ledger needs reconciling against reality.

Two jobs, one loop: reconstruct the durable record, and repair it. The ledger is a spawn log that nobody closes, so every sweep both reports AND fixes. This is a read-and-reconcile sweep: if it surfaces work, spawn an agent for it (foundation) rather than doing it here. Remote state is reportable only once you have actually read it, and that is a per-PR read: keep the sweep BROAD, so do not pull a CI roll-up or review decision for every row — report the ledger, and offer a focused check on the one row that matters (daintree.workflow.follow-up-on-issue). Job logs, check output and the bodies of reviews left on a PR stay out of reach of any forge read, so never report on those.

Procedure:

1. Sweep the durable record in ONE batch of reads — they dispatch concurrently and are all read-only: `workflow.list({"status":"active"})` (repeat for "blocked"/"pending" when the user asked broadly), `context.snapshot` for live worktrees / terminals / attention queue, `watcher.list`, `async.list` and `timer.list` for live supervision, `queue.digest` for what still needs the user, and `agentTask.list` for spawn sagas (defaults to THIS session; pass `{"scope":"all"}` for older history). Never assemble this from memory or from what you did earlier in the conversation — read it.

2. Reconcile every interesting row against reality; the ledger lies by default. `workflow.get({"id":"<row id>"})` returns the full record — terminalIds, watcherIds, worktreeId, branch, issue/PR fields, notes, nextAction. Classify each row by evidence — not by its `status` field, and not by an agent state:
   - its terminalIds are still in the roster AND one of its watcherIds is still in `watcher.list` (or an async handle covers it) — LIVE and supervised. Leave it be; its supervisor will wake you.
   - its terminalIds are GONE from the roster — ENDED. The terminal is closed, so nothing is running any more and the row must close; HOW it ended is a separate question (step 3).
   - the terminal is still in the roster but no watcher and no async handle covers it — LIVE-UNSUPERVISED, outcome UNKNOWN. A gone watcher is not a gone terminal, so this row has NOT ended. Never read an idle terminal as "done and nobody closed the row": idle is not a finish (foundation, finish detection), and the agent may be mid-startup, paused or backgrounded.
   To learn what a terminal actually did — the evidence step 3 runs on — take one bounded `terminal.extract` per terminal id, batched in a single turn (they run concurrently). Do not put several ids in one text extract expecting one answer each — a multi-id text extract MERGES every tail into ONE answer; use `terminal.extract.json` with an array schema keyed by terminalId if you want them attributed in a single call.

3. Close the rows the runtime cannot — on evidence, never on a hunch. This is the load-bearing half. Daintree inserts a ledger row with status "active" on every `agentTask.spawnForEdits` and every `workflow.startWorkOnIssue`. The only things that ever close one are a watcher completion, a `watcher.cancel`, and a startup reconcile that skips any row with a live terminal — yet fan-outs, polls and competitions deliberately spawn WITHOUT watchers and leave their terminals open. So a five-agent poll leaves five rows "active" forever, and the injected turn state replays them to you every subsequent turn as work in flight. Close them yourself:

       workflow.update({"id":"<row id>", "status":"done"})

   "done" asserts the work FINISHED, so it needs proof: a watcher's confirmed completion for that row, the step-2 tail read showing the work actually landed, or a finish you drove and read yourself earlier in this session. An idle terminal is not proof (foundation, finish detection) — and neither is the row's own age. Without proof, close on what you DO know: "cancelled" for a run whose terminal is gone with no surviving outcome, or one abandoned or superseded; "failed" for a spawn that never produced anything; "blocked" when it is genuinely waiting on the user. A LIVE-UNSUPERVISED row does not close at all — offer to adopt it with `agentTask.superviseTerminal({"terminalId":"<id>","goal":"<what completion looks like>","spawnMode":"edit"})` (use "explore" for a read-only run so the supervisor does not expect file changes), or report it as needing a look (step 6c). WARNING — provided array fields REPLACE the stored arrays: if you touch `notes`, `terminalIds`, `watcherIds` or `queueEventIds`, send the FULL intended list read back from `workflow.get`, never a fragment. Safest edit is `status` alone. Do this as the closing act of any fan-out, poll or competition too, not only during a sweep — there you already hold the evidence, having just read those agents' output.

4. Back-fill so the ledger is an end-to-end record. When a row's branch has a PR, put it on the row: `workflow.update({"id":"<row id>","prNumber":<int>,"prUrl":"<url>"})`. Find the number with `forge.listPRs({"arguments":{"state":"open"}})` and match on branch — that wrapper is a passthrough that accepts ONLY `arguments` at the top level (pass `{}` for none); nest every query field inside it, since a top-level `state` or any other stray key is rejected outright; match on the item's `headRef`, which is the source branch (there is no `head` field on a listing item). Set `branch`, `worktreeId`, `issueTitle`/`issueUrl` when the row was created bare, and `git.getProjectPulse` on the worktree if you need branch state to describe it. Report the PR as this listing gives it (number, url, and at most open/merged/draft/closed); a summary row carries no check or review verdict, and reading one per row is a focused job this sweep hands off (daintree.workflow.follow-up-on-issue).

5. Resumable sessions live outside the ledger. `agentSessionHistory.list({"worktreeId":"<exact id>","limit":20,"offset":0})` is a typed read wrapper — call it directly, never through `daintree.call`. The action MUST resolve a `worktreeId` or `projectId`; without an explicit selector it uses the active action context, and without either scope it FAILS rather than listing every project. Explicit worktree and project ids combine to narrow the result. Records are newest-first and return under `{sessions,total,hasMore}`; page with increasing `offset` while `hasMore`, up to `limit:100`. Records carry sessionId, agentId, worktreeId, title, projectId, savedAt, and sometimes agentLaunchFlags / agentModelId / cwd / branch. CRITICAL and easy to get wrong: there is NO Assistant restore tool and you CANNOT read a closed session's transcript. To bring one back you spawn a FRESH agent in that worktree with `agentTask.spawnForEdits`, reusing the record's `worktreeId` and `agentId` and writing a `taskPrompt` that states what to continue (rebuild that context from the ledger row, the branch state and the user — not from the dead session). Tell the user plainly that this is a new agent in the old worktree, not a faithful resume: its prior conversation is gone. Never present it as "resuming session X".

6. Report ONE ranked picture, not five tool dumps. Order it: (a) needs you now — open queue items, agents asking a question, blocked rows; (b) live and supervised — terminal id, watcher or async handle, and what will wake you; (c) live but UNsupervised — say the outcome is unknown and offer to adopt it (step 3) or wait; (d) closed — which rows you closed, to what status, and on what evidence; (e) resumable from history, newest first. Give real ids verbatim. Then `queue.resolve {"id":"<id>"}` per the foundation's inbox rule — only what you actually relayed here. If the user then wants to LOOK at one of these, `terminal.focus({"terminalId":"<id>"})` reveals that terminal — focus is per-terminal and by id; there is no "next thing needing attention" call.

7. Optional forensics for "what happened while I was away". `audit.export({"format":"json","tsFrom":<epoch ms>,"tsTo":<epoch ms>,"limit":200})` with optional `actor` (main, watcher, timer, workflow, system), `toolName` (exact dotted name), and `outcome` (ok, error, denied, dedup, grant_ok). Filters AND-combine and rows return newest-first, so one filtered read answers the question — narrow it with `outcome:"error"` or a single toolName rather than exporting everything. Summarize what it shows; never paste raw rows back.

Confirmation: the sweep and its reads are free. `workflow.update` rewrites durable state — say which rows you are closing and why as you report it, and ask first if a row's outcome is genuinely ambiguous rather than guessing "done". `daintree.call` is the system-risk escape hatch and is confirmed per the active tier. Spawning a resume agent mutates a real worktree — confirm it like any spawn.

## Recovering detail the checkpoint rounded off

Auto-compaction replaces a long transcript with a lossy summary, but it preserves the FULL discarded transcript as an artifact, and the CURRENT [checkpoint] note carries its breadcrumb. When resuming needs an exact detail the checkpoint generalized — the literal id that was spawned, the exact wording of a user decision, which of two branches was chosen — page that artifact surgically: `artifact.read` a few pages from the relevant offset, following `nextOffset`, never a full replay of the transcript you just compacted away. Start ONLY from an artifactId in the current checkpoint note or your latest results; an id remembered from an earlier turn may be pruned, and if it is not found the content is gone — say so and resume from what the checkpoint does carry.

Report back: the ranked standing picture with real ids (what needs the user, what is live and how it is supervised, what is unsupervised and therefore unknown), which ledger rows you closed or back-filled — to what status and on what evidence, which sessions are resumable and the honest caveat that resuming means a fresh agent, and the next checkpoint — what will wake you or what you need from the user.
