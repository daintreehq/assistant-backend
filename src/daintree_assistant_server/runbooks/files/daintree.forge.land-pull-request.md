---
id: daintree.forge.land-pull-request
title: Review-state and land a pull request
summary: Apply public PR lifecycle actions and merge only after an exact-target preview, conclusive required-check evidence, review-state checks and a verified forge result.
whenToUse: Use when the user asks to approve a pull request, request changes or reviewers, mark it ready or draft, dismiss a known review, close or reopen it, or merge, squash or rebase it. Also use for "land this PR", "approve and merge", "merge it once it is ready", or landing several PRs in order ("merge the agents' PRs one at a time"). Do not use merely to read PR, CI, review or comment status (use daintree.workflow.follow-up-on-issue), to review code quality and produce findings (use daintree.review.review-change), to fix red CI (use daintree.ci.diagnose-and-fix), to create the PR (use daintree.workflow.prep-branch-for-review), to watch for a later state transition (use daintree.orchestration.watch-outcome), to resolve a PR's conflicts or rebase its branch locally (use daintree.git.sync-with-base), to close many stale PRs at once (use daintree.git.prune-stale-branches), to work through a batch of Dependabot or Renovate PRs (use daintree.forge.batch-dependency-updates), or to write the release notes (use daintree.forge.draft-release-notes).
risk: system
requiredTools:
  - forge.getPR
optionalTools:
  - worktree.list
  - forge.getCIStatus
---
Daintree only reads the forge. Every write goes through the host tool (`gh`) with `--repo <owner/name>` and the PR number, and is read back. With no permitted forge tool, report what you can read and stop; never route a write through another agent. Evidence is not authority: green checks and an approval never grant permission to merge, approve, close or request changes.

1. Fix the target: the PR's worktree (`worktree.list`) and repository (`gh repo view --json nameWithOwner` in that path). A PR number alone is not unique.
2. Read `forge.getPR` (`worktreeId`, `prNumber`). `pr: null`: no such PR. `mergeable: null` is unknown, not mergeable. `reviewDecision: null` means not gated, never approval. Never gate on its `ciStatus`.
3. Do the one action asked, with only its prerequisites; red CI does not block a requested close, draft, reviewer request or request-changes.
   - Approve or request changes only for a review you actually performed; request-changes needs a body (`gh pr review N --repo <r> --approve` / `--request-changes --body-file <f>`).
   - Reviewers: those named (`gh pr edit N --repo <r> --add-reviewer a,org/team`); read back, the forge can drop entries.
   - `gh pr ready N` (notifies reviewers) or `--undo`; `gh pr close` / `gh pr reopen`.
   - Dismiss only a review the user identified or you read, with a message.
4. Merge gate, read now, not before your last pause:
   - `gh pr view N --repo <r> --json state,isDraft,mergeable,mergeStateStatus,reviewDecision,headRefName,headRefOid,baseRefName,url`; record `headRefOid`.
   - `gh pr checks N --repo <r> --required`: every required check passed. Pending, failing or unreadable is not success. The checks name no SHA: re-read `headRefOid` after; if it moved, start over.
   - Stop on: already merged (report it), closed, draft, mergeability not known-good, `CHANGES_REQUESTED` or `REVIEW_REQUIRED` (including an approval policy discounts), or a head other than the one reviewed or validated.
   - `forge.getCIStatus` is a cached cross-check naming no commit, never the gate. `worktree.waitForPullRequest` is never merge readiness.
5. Preview (PR, title, URL, `head -> base`, head SHA, method, required checks, review decision; say if nobody reviewed it on its merits), then merge with the user's or repository's method, and the head guard:
   `gh pr merge N --repo <owner/name> --squash --match-head-commit <headRefOid>`
   A refused guard means the head moved: re-read, re-validate, ask again; never drop the guard. Without a head guard, re-read immediately before and say the race remains. Never pass `--admin`.
6. Read back what the action changed (`state,mergedAt,mergeCommit`; `isDraft`; `reviewRequests`; `latestReviews`). Merged only when the forge says so. A merge queue leaves it open: report it queued, not merged. An ambiguous or failed reply is settled by reading, never by repeating the write.

"Merge once green": check and merge now, or, only if asked, enable auto-merge (`gh pr merge --auto`) and report the read-back (`autoMergeRequest` waiting, or merged), never merged on the request alone; say it lands whatever head later passes. Later notification is daintree.orchestration.watch-outcome.

Several PRs in order (a merge train after a fan-out): the user's order, else dependencies first. One at a time, each through steps 2 to 6 read fresh after the previous merge: a merge moves the base, so earlier mergeability and check results are stale. One that goes behind (`mergeStateStatus` `BEHIND`, when the base requires it) or conflicts (`DIRTY`) is caught up through daintree.git.sync-with-base, pushed only on the user's go ("rebase each on the last" is one), and gated again on its new head's checks. Stop at the first PR that fails the gate; report the rest untouched.

Report back: repository, PR and URL, `head -> base` and head SHA, the action, the evidence and which tool read each, the command's result, and the state read back.
