---
id: daintree.workflow.prep-branch-for-review
title: Prepare a branch for review
summary: Get a read-only review-readiness verdict, then do the real prep — commit, push, open the PR — branching on what the verdict reports.
whenToUse: Use when the user asks to prepare a branch for review, open or ready a PR, wrap up work on an issue for review, or check whether the current branch is ready to push. Do not use to judge whether an agent's work is actually correct or complete before trusting it (use daintree.orchestration.verify-agent-work), to START work on an issue rather than finish it (use daintree.workflow.start-work-on-issue), to be notified later when the PR merges (use daintree.orchestration.watch-outcome), to read an EXISTING PR's CI or review state or to comment on and close its issue (use daintree.workflow.follow-up-on-issue), to forward a reviewer's findings to an implementer agent (use daintree.agents.relay-review-feedback), to REVIEW the change rather than ready it (use daintree.review.review-change), or to fix a failing check (use daintree.ci.diagnose-and-fix).
risk: external
requiredTools:
  - context.snapshot
  - project.runCheck
  - worktree.getCurrent
  - forge.listPRs
  - forge.getPR
  - forge.getChecks
  - forge.listIssues
  - forge.getIssue
  - workflow.prepBranchForReview
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.extract
  - terminal.sendCommand
  - daintree.call
---
Use when: the user wants to ready the current branch for review / open a PR.

The trap this runbook fixes: `workflow.prepBranchForReview` is READ-ONLY despite the
name. It does NOT commit, push, or open a PR — it returns a verdict you act on. The
actual prep (commit → push → open PR) is a SEPARATE, confirmed sequence you run after a
good verdict. So call the verdict freely (no confirmation), then do the real work. The
focus-hint tools above are not a fence — every registry tool is callable this turn.

Procedure:

## Step 1 — Fix the target
Call `context.snapshot`, then `worktree.getCurrent`, so you KNOW which worktree/branch
you are prepping. `prepBranchForReview` targets the active worktree unless you nest a
`cwd` inside its `arguments` (step 3); a later commit/push lands wherever you point it,
so be sure it's the branch the user means. `worktree.getCurrent` returns `{ worktree: { id, path,
branch, prNumber, prUrl, ... } | null }` — if `prNumber` is already set, a PR exists on
this branch and the job is usually "ready/update it," not "create a new one."

## Step 2 — Don't open a duplicate PR
`forge.listPRs({ arguments: { state: "open" } })` -> `{ items: [PR...], nextCursor, hasMore }`.
`forge.listPRs`, `forge.listIssues`, and `forge.getIssue` are passthrough wrappers accepting
ONLY `arguments` at the top level — nest every query field inside it; a top-level `state` is
rejected outright. (`forge.getPR` is TYPED: `{ prNumber, cwd? }` at the top level. The
`daintree.call forge.*` actions in step 4 are a different surface: `daintree.call` itself takes
`{ name, arguments, requestKey? }`, and the forge payload goes flat inside its `arguments`.) Each PR
carries `number`/`url`/`headRef` — the source branch is `headRef`, NOT `head`, so a match on
`head` finds nothing and you open a duplicate PR. (`head` IS the right key when you CREATE
one in step 5; it is an argument there, not a result field.) Do NOT branch on a
review-decision or CI-status field from a LIST item — neither is guaranteed on a bulk item;
these summary rows are for duplicate detection, and a field missing here says nothing. (CI and
review state ARE readable, just not from this call: `reviewDecision` comes from typed
`forge.getPR` and the CI verdict from `forge.getChecks`, both a focused read on ONE PR —
daintree.workflow.follow-up-on-issue.) If an open PR's `headRef` is this branch, you are
updating, not creating — skip the PR-create in step 5 and just push.

## Step 3 — Get the verdict (free, read-only, NO confirmation)
`workflow.prepBranchForReview` is a passthrough wrapper like the forge ones in step 2, with one
extra catch: `arguments` is REQUIRED. Call `workflow.prepBranchForReview({ "arguments": {} })` for
the active worktree, or `workflow.prepBranchForReview({ "arguments": { "cwd": "<path>" } })` to
target another one. The top level accepts only `arguments` and `requestKey`, so a top-level `cwd`
is an unknown key and is rejected outright — and omitting `arguments` fails the required check.
It returns:
```
{ verdict, hasUncommittedChanges, hasMergeConflicts,
  stagedCount, unstagedCount, currentBranch, repoState, detectedRunners: [...] }
```
`verdict` is one of:
- `ready` — tree is in a committable/pushable shape. GREEN LIGHT to prep. (This does NOT
  mean anything was committed or pushed — that's still all on you in step 4/5.)
- `blocked_uncommitted_changes` — there is work to commit (`stagedCount` /
  `unstagedCount` > 0). Commit + push it (step 4), then re-check or proceed.
- `blocked_merge_conflicts` — the branch has conflicts. This is real file work, and you
  NEVER edit files — see "Checks and conflicts" below.
- `blocked_repo_busy` — a git operation is in flight. Wait briefly and re-run step 3; do
  not push into a busy repo.
- `no_runners_detected` — no test/lint runner was found. Not fatal — proceed, but tell
  the user verification is thin (nothing ran the suite). `detectedRunners` lists what, if
  anything, was found.

Use `currentBranch` from this result as the `head` for the PR-create in step 5. Read the
verdict and route; everything below depends on which one you got.

## Step 4 — Commit and push (the REAL prep — confirmed; via daintree.call)
There is NO typed `git.commit` / `git.push` / `getStagingStatus` wrapper — those go
through the raw escape hatch. Do this only when there is work to commit (verdict
`blocked_uncommitted_changes`, or `ready` with pending changes the user wants in):
- `daintree.call git.commit { cwd?, message: "..." }` — `message` is REQUIRED and must be
  non-empty/non-whitespace (the call throws on an empty message). Confirmed.
- `daintree.call git.push { cwd?, setUpstream?: true }` — pass `setUpstream: true` the
  FIRST time you push a brand-new local branch, or the push fails with no upstream.
  Confirmed.
After committing, the tree is clean; you do not need to re-run `prepBranchForReview`
unless you want to re-confirm `ready` before opening the PR.

## Step 5 — Open the PR (confirmed)
Forge WRITES have no typed wrapper — go through the escape hatch:
`daintree.call forge.createPR { head: currentBranch, base: "<target>", title: "...", body?, draft? }`.
This is the one forge mutation here that RETURNS the PR object (`{ number, url, ... }`) —
most forge writes return void — so capture `number`/`url` now rather than re-listing.
Confirmed (external risk). If step 2 found an existing open PR on this head, skip create;
the push already updated it. To flip a draft to ready, `daintree.call forge.markPRReadyForReview`.

## Checks and conflicts — run the check yourself, spawn only for edits
Running a detected runner is a CALL, not a delegation — do NOT spawn an agent merely to
execute a check. `project.runCheck({"projectId":"…","runnerId":"…","cwd":"<worktree path>"})`
is a typed wrapper — call it directly, never through `daintree.call`.
Take `runnerId` from step 3's `detectedRunners` — never invent one, and confirm what an
unfamiliar id runs: detection surfaces every runnable script, not just checks, and pointing
this at a long-lived server blocks until the timeout. Pass `cwd` explicitly for the worktree
you are prepping: it defaults to the project root, so omitting it silently checks the WRONG
tree. A failing command comes back as a FAILED CHECK, not an error, so read `passed` rather
than trusting that the call succeeded; the output tail is diagnostic only, and `command` is
what actually ran — `runCheck` re-detects inside the `cwd` you passed, so it can differ from
what the root advertised. On `no_runners_detected` there is nothing to run; say verification
is thin rather than implying anything passed.

Edit work is different — you NEVER edit files. If the verdict is `blocked_merge_conflicts`,
or a check failed and the user wants it green before review, spawn a visible agent with
`agentTask.spawnForEdits` mode:"edit" and supervise it in exactly ONE mode, never both:
BACKGROUND (spawn WITH `watch: true` + `watchGoal`, then END your turn and react on the
watcher's `completed_*` event), or IN-TURN (spawn with NO watcher, then `terminal.awaitAll`,
read the tail once with `terminal.extract`, relay with `terminal.sendCommand`). Finish is
never a bare `waiting` (an agent shows waiting before it starts, when paused, and when
backgrounded) and never a transient `completed` (it bounces back within seconds). Once the
branch is fixed, return to step 3 → 4/5, and re-run the check above rather than taking the
agent's word for it.

Report back: state the verdict you got, any check you ran (its `command`, `passed`, `exitCode`),
what you committed/pushed, and the PR number + url — or, if
blocked, exactly what is blocking and the next action (and, if you spawned a fixer agent,
its terminalId + watcherId; supervision is durable — the background supervisor keeps watching if the user closes the assistant).
Never claim "the PR is ready" off `prepBranchForReview` alone: that verdict only means the
tree is in a committable shape, not that a PR exists.
