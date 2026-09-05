---
id: daintree.workflow.prep-branch-for-review
title: Prepare a branch for review
summary: Get a read-only review-readiness verdict, then do the real prep — commit, push, open the PR — branching on what the verdict reports.
whenToUse: Use when the user asks to prepare a branch for review, open or ready a PR, wrap up work on an issue for review, or check whether the current branch is ready to push. Do not use to judge whether an agent's work is actually correct or complete before trusting it (use daintree.orchestration.verify-agent-work), to START work on an issue rather than finish it (use daintree.workflow.start-work-on-issue), to be notified later when the PR merges (use daintree.orchestration.watch-outcome), to read an EXISTING PR's CI or review state or to comment on and close its issue (use daintree.workflow.follow-up-on-issue), to forward a reviewer's findings to an implementer agent (use daintree.agents.relay-review-feedback), to REVIEW the change rather than ready it (use daintree.review.review-change), or to fix a failing check (use daintree.ci.diagnose-and-fix).
confirmation: "Getting the branch ready for review. The goal is a tree that is committed, pushed and free of conflicts, which is a question about the branch's shape rather than whether the work is good."
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
  - git.getProjectPulse
  - worktree.list
  - project.detectRunners
  - tool.schema
  - workflow.update
---
Use when: the user wants a branch prepared, pushed or opened for review, or only a readiness assessment.

`workflow.prepBranchForReview` is a READ-ONLY diagnostic. It neither stages nor commits nor pushes nor opens a PR. A question about readiness stops after the assessment; a request to prepare/open a PR authorizes the necessary scoped steps, subject to ordinary tool confirmation.

Procedure:
1. Bind the target. Resolve the exact worktree id AND path from current context or `worktree.getCurrent` / `worktree.list`. Keep that locator on every git/forge read and write. Do not substitute the current worktree when the user named another. Read `git.getProjectPulse` for branch state and recent commits.
2. Check for an existing PR in that repository: `forge.listPRs({"arguments":{"worktreeId":"<id>","state":"open","search":"head:<verified branch>"}})`. Preserve all filters and locator while paging. Match `headRef` and intended base/repository, not a similar title. If a PR exists, update it; do not create a duplicate. `forge.getPR` takes flat `{worktreeId,prNumber}` and returns `{pr}`. CI is a separate flat `forge.getChecks` call, not evidence from a listing row.
3. Get readiness for the exact checkout:

```json tool=workflow.prepBranchForReview
{"arguments":{"cwd":"<resolved worktree path>"}}
```

   This is `workflow.prepBranchForReview` argument JSON. `arguments` is required; top-level `cwd` is rejected. Read `verdict`, change/conflict flags, staged/unstaged counts, `currentBranch`, `repoState`, `detectedRunners`.
   - `ready`: git shape is suitable; no check has run yet.
   - `blocked_uncommitted_changes`: inspect and stage the intended scope, then commit (step 5).
   - `blocked_merge_conflicts`: delegate conflict resolution to an edit agent in this worktree; recheck afterward.
   - `blocked_repo_busy`: inspect the in-progress operation and retry once after it settles. Do not abort it or push into it merely to get a green verdict.
   - `no_runners_detected`: verification is unavailable through detected runners, not passed. Use existing evidence or a scoped agent check; report the gap.
4. Run the relevant bounded checks before publishing. Take a runnerId from `detectedRunners` or `project.detectRunners` and projectId from actual context. Inspect what the runner does: scripts can deploy, publish or start a server. Call `project.runCheck({"projectId":"<id>","runnerId":"<id>","cwd":"<resolved path>","timeoutMs":600000})` only for the intended check. It re-detects within cwd; report the returned command and `passed`, `exitCode`, `timedOut`, `aborted`, `outputTruncated`. Tool success can contain `passed:false`; timeout/abort is incomplete verification. Delegate failures requiring file edits, use one supervision mode, then run verification on the resulting revision.
5. Stage deliberately, then commit. The raw git actions have no typed wrappers; discover `tool.schema` when their shape is needed and use `daintree.call` with `{name,arguments,requestKey?}`. Read `git.getStagingStatus` in this worktree first: `git.commit` commits ONLY the index, including anything staged before your task. It does not stage automatically.
   - Stage only the task's identified files with raw `git.stageFile` and `filePath`, preserving unrelated staged and unstaged work. If an intended commit would include unrelated staged changes, resolve that scope rather than unstaging another person's selection or committing it silently.
   - `git.stageAll` is appropriate only when ALL changes are intended for this commit; do not use it as a shortcut on a mixed tree.
   - Re-read staging, then raw `git.commit` with a grounded non-empty `message` and explicit `cwd`. Read the commit result; do not assume a clean tree afterward because unrelated changes may remain.
6. Push even when no NEW commit was needed. A clean working tree can contain already-committed, unpushed work. Raw `git.push` takes explicit `cwd` and optional `setUpstream:true` for a new branch. Verify the branch and intended commits before publishing. On rejection or an ambiguous transport result, re-read state; never force-push, rewrite history, or blindly repeat commit/push. A requestKey is a bounded retry guard, not permanent exactly-once execution.
7. Create or ready the PR after a confirmed push. Resolve `base` from repository instructions or an existing PR; do not assume main. Prepare the title/body from the final change and checks actually run. Raw `forge.createPR` takes `worktreeId`, `head` (the actual currentBranch), `base`, `title`, optional `body`/`draft`. Read number/URL from its result. For an existing draft the user wants ready, raw `forge.markPRReadyForReview` takes the same worktree locator and its `prNumber`. If creation times out, look for the matching PR before retrying; retain a unique requestKey for that logical create.
8. Verify the postcondition. Re-read `forge.getPR` for the recorded number in the same repository; confirm head/base and requested draft/ready state. Report local checks separately from provider CI: cached green does not establish checks ran on the newly pushed revision. Neither readiness nor CI is a code review. Record the PR on the matching workflow row without replacing unrelated array fields.

For delegated fixes, give the edit agent the exact worktree, scope and acceptance criteria. Use a background spawn supervisor or one async/in-turn wait; report partial progress and resume these steps on real results. Keep prior authority across that continuation rather than asking the user to approve the same preparation again.

Report back: the readiness verdict, verification command/results and limits, which files/commit were included, whether the push succeeded, PR number/URL and confirmed head/base/draft state, or the concrete blocker and any agent/supervision handle still running.
