---
id: daintree.workflow.start-work-on-issue
title: Start work on a forge issue
summary: How to pick a forge issue and start work on it through workflow.startWorkOnIssue, which spawns the agent and auto-attaches a BACKGROUND supervisor watcher.
whenToUse: Use when the user asks to start work on ONE forge issue they have already settled on — pick up a ticket, begin an issue, set up a worktree/branch for it and put an agent on it. Do not use when the issue is NOT yet chosen and the job is searching or triaging the backlog for a candidate (use daintree.workflow.find-issue-work); for SEVERAL issues at once, an agent per ticket, or working a batch in parallel (use daintree.orchestration.fan-out-work); to finish work and open a PR (use daintree.workflow.prep-branch-for-review); to check CI, a review decision or the issue thread on work already underway (use daintree.workflow.follow-up-on-issue); for a change with no forge issue behind it (use daintree.edits.spawn-visible-agent); to create a worktree from a workspace recipe rather than an issue (use daintree.recipe.run-or-create); or to pick up / resume work already in flight from an earlier session (use daintree.orchestration.status-and-resume).
confirmation: "Starting work on one chosen issue. The goal is an isolated worktree with an agent running in it, supervised in the background so its finish comes back on its own."
risk: external
requiredTools:
  - context.snapshot
  - forge.listIssues
  - forge.listPRs
  - forge.getIssue
  - forge.getPR
  - daintree.call
  - workflow.startWorkOnIssue
  - terminal.summarize
  - queue.resolve
  - terminal.sendCommand
  - watcher.cancel
---
Use when: the user wants to start work on a forge issue.

This is the one-call "begin this issue" path. workflow.startWorkOnIssue does a COMPLETE
synchronous workflow inside Daintree — fetch the issue, derive the branch, create the
worktree, SPAWN the work agent, (optionally) run a recipe first, best-effort inject
worktree context, (optionally) assign the issue — and then the assistant wrapper
auto-attaches ONE supervisor watcher to the spawned terminal (attachWatcher defaults
true). The supervision choice is therefore ALREADY MADE for you: this path is
BACKGROUND by design. You set it up, end your turn, and react when the watcher's
completed_* event lands on the attention queue — so under the foundation's
one-supervisor rule, never also drive this agent in-turn (terminal.awaitAll /
terminal.sendCommand) and never attach a second watcher on top of the wrapper's.

Procedure:

1. Orient first. Read the worktree entries already injected this turn (else
   context.snapshot) — each carries `issueNumber`/`prNumber`, so you DON'T start a
   duplicate worktree for an issue already in flight. If the project or target worktree
   is ambiguous, resolve it before mutating.

2. Identify the issue. If the user named a number, go straight to step 3. If the user
   omitted it, list candidates with `forge.listIssues({ arguments: { state: "open" } })` ->
   `{ items: Issue[], nextCursor, hasMore }` and confirm WHICH one with the user — but a
   real "find me something to work on" is a triage job with its own runbook, filtering
   and dedupe (daintree.workflow.find-issue-work); come back here once one is picked.
   `forge.listIssues`,
   `forge.listPRs`, and `forge.getIssue` are passthrough wrappers that accept ONLY
   `arguments` at the top level — nest every query field inside it; a top-level
   `state`/`issueNumber` is rejected outright. (`forge.getPR` is the exception: it is TYPED
   and takes `{ prNumber, cwd? }` at the top level.) Then ground the
   task with `forge.getIssue({ arguments: { issueNumber } })` -> the Issue object (title, body, `labels[]`,
   `assignees[]`, `commentCount?`, …). Read it before mutating anything — it shapes the
   agent's prompt and tells you if it's already assigned.

3. Resolve the agentId — this is the silent-failure trap. startWorkOnIssue REQUIRES an
   `agentId`, and its internal spawn does NOT validate it: an unknown or mis-transcribed
   id still creates a terminal, but command generation fails silently -> a dead,
   zero-output panel the watcher then babysits forever. A name the user spoke or typed
   is only a HINT (voice transcription drops letters, so a spoken id arrives subtly
   misspelled), so
   resolve it to a REAL configured id. Start from the agent registry in your startup
   context rather than `daintree.call agentSettings.get`, which is a system-risk escape
   hatch requiring typed confirmation. But the registry is not guaranteed complete — it
   says "complete" or "partial" and reports rows omitted by the size limit — so fall back
   to `agentSettings.get` whenever it is absent, partial, reports omissions, or simply
   does not contain the id you are trying to resolve. Never pass an unverified name through.

4. Confirm (see below), then launch. The wrapper takes the Daintree
   workflow fields NESTED inside an `arguments` object, NOT as top-level keys (the
   top-level schema is strict — only `arguments`, `requestKey`, and `attachWatcher` are
   allowed there, and any stray top-level key is rejected as an unknown field). Call it
   with this LITERAL shape:

       workflow.startWorkOnIssue({
         "arguments": {
           "issueNumber": <int>,            // required
           "agentId": "<resolved id>",      // required
           "branchName": "<optional override>",
           "baseBranch": "<optional, e.g. main>",
           "assignToSelf": true,            // optional
           "injectContext": true,           // optional, best-effort worktree digest
           "recipeId": "<optional>"         // optional: run a recipe in the new worktree first
         },
         "attachWatcher": true,             // optional, assistant-side ONLY, defaults true
         "requestKey": "<optional>"         // optional idempotency key (sibling of arguments)
       })

   Put EVERY Daintree-side field (issueNumber, agentId, branchName, baseBranch,
   assignToSelf, injectContext, recipeId) INSIDE `arguments` — never flatten them to the
   top level. `attachWatcher` is assistant-side only (it controls the auto-watcher and is
   never forwarded to Daintree); leave it out to get the default-true watcher. `requestKey`
   is an optional top-level sibling forwarded to Daintree for idempotency (Daintree dedupes
   on it and strips it before its own validation) — optional, you don't need to invent one.
   If you set `recipeId`, the recipe's terminals spawn synchronously alongside the work
   agent; agent-sourced recipe terminals are capped at 3 (overflow -> failed). Those extra
   recipe terminals are UNSUPERVISED workspace setup — only the issue work agent gets the
   auto-watcher.

5. Read the return and report the REAL values. The call returns:

       { issueNumber, issueTitle, issueUrl, worktreeId, worktreePath, branch,
         terminalId, recipeLaunched, spawnedTerminalCount, failedTerminalCount,
         assignedToSelf, assignedUsername, assignmentError, contextInjected }

   and the assistant wrapper adds the attached `watcherId`. Use `terminalId` (NOT
   worktreeId) for any later follow-up. Report: which issue (issueTitle/issueUrl), the
   worktreePath + branch created, the terminalId, the watcherId, whether it was assigned
   (assignedToSelf / assignmentError), and whether context injected (contextInjected).
   Look ahead for partial failure — DON'T claim a clean launch if you see any of these:
   - `terminalId` is null, or a partialSuccessError surfaced (worktree made but the
     spawn/recipe failed) — say the agent did NOT start.
   - `failedTerminalCount > 0` — a recipe terminal failed; name it.
   - no `watcherId` / a watcherWarning — the watcher did NOT attach (Daintree/storage
     error). Say so; either retry the spawn, or supervise the terminal yourself without
     a tight read-loop (terminal.summarize once it has settled).

6. END your turn. The watcher now supervises the agent in the background and publishes
   to the attention queue when it settles, so proof of finish is its completed_* event —
   never your own glance at the agent's state (the foundation's finish rule). Tell the
   user the work is running and the next checkpoint is that event; supervision is
   durable (the background supervisor keeps watching if the user closes the assistant,
   and the outcome greets them on the next launch). One launch-specific caveat: a
   freshly spawned agent prints NOTHING for several seconds — empty output right after
   launch means "not started yet", never "the terminal is gone".

7. React on the wake (the scheduler wakes you when the queue event arrives):
   - `completed_success` — the agent finished, the acceptance contract (where one was
     set) was met, and the worktree is clean. Relay the gist with terminal.summarize
     (a coding agent's raw TUI scrollback is garbled; don't dump it), and offer the
     obvious next step (prepare the branch for review). Only after this trustworthy
     signal is it safe to propose an irreversible action (commit/push).
   - `completed_unverified` — the agent stopped but the work is UNVERIFIED (contract not
     met, uncommitted changes remain, or git state couldn't be read). Tell the user
     plainly and prompt them to review BEFORE proposing any commit/push/delete. Do not
     silently upgrade it to success.

8. Close the loop. Once you have relayed the result, clear the wake's inbox item with
   `queue.resolve {"id": "<inbox id>"}` (the wake-up note prints the id); do NOT call
   `watcher.cancel` on that finished watch — it already stopped itself and the call is
   refused as "already ended". An agent stopped on a question (`agentState "waiting"`,
   `waitingReason "question"` or `"approval"`) is the leave-OPEN case — answer it with
   terminal.sendCommand. Abandoning a STILL-ACTIVE watch (you got what you needed early,
   or the task was dropped) is the one real cancel: `watcher.cancel {"id":
   "<watcherId>"}`, said plainly, noting you can re-attach later.

Confirmation: workflow.startWorkOnIssue mutates real state and touches the forge —
confirm before launch per the active tier (external risk).

Report back: which issue was started (title + url), the worktree path / branch / the
real terminalId and watcherId, whether it was assigned and context injected, any partial
failure, and that supervision continues even if they close the assistant (the
background supervisor takes over) — the next checkpoint is the watcher's completed_*
event, not a status you poll yourself.
