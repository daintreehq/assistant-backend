---
id: daintree.forge.land-pull-request
title: Review-state and land a pull request
summary: Apply public PR lifecycle actions and merge only after an exact-target preview, conclusive required-check evidence, review-state checks and a verified forge result.
whenToUse: Use when the user asks to approve a pull request, request changes or reviewers, mark it ready or draft, dismiss a known review, close or reopen it, or merge, squash or rebase it. Also use for "land this PR", "approve and merge", or "merge it once it is ready". Do not use merely to read PR, CI, review or comment status (use daintree.workflow.follow-up-on-issue), to review code quality and produce findings (use daintree.review.review-change), to fix red CI (use daintree.ci.diagnose-and-fix), to create the PR (use daintree.workflow.prep-branch-for-review), or to watch for a later state transition (use daintree.orchestration.watch-outcome).
risk: system
requiredTools:
  - forge.getPR
optionalTools:
  - worktree.list
  - forge.getCIStatus
---
Use when: the user wants an existing pull request's public review state changed (approve, request changes, request reviewers, ready or draft, dismiss a review, close, reopen) or wants it landed.

Availability: Hybrid. Daintree's app MCP only reads the forge (`forge.getPR`, `forge.getCIStatus`, `forge.listPRs`); at core tier each read asks the user first, and an API-key client or a session with no Daintree connection has none, so the host tool reads instead. Every write here (review, reviewer request, ready or draft, close, reopen, merge) goes through the host's own permitted forge tool (for GitHub, `gh`), always with an explicit repository and PR number, and is read back afterwards. With no permitted forge tool, report the state you can read and stop; never route a write through another agent's terminal. Evidence is not authority: green checks and an approval never grant permission to merge, approve, close or request changes.

Procedure:
1. Fix the target. Resolve the worktree the PR belongs to (`worktree.list`, id and path) and its repository (`gh repo view --json nameWithOwner` run in that path). Carry both on every read and write: `worktreeId` for Daintree reads, `--repo <owner/name>` for the host tool. A PR number alone is not unique across repositories.
2. Read the PR:

```json tool=forge.getPR
{"worktreeId":"<resolved worktree id>","prNumber":42}
```

   `pr: null` means no such PR in that repository, not a failure to retry. Read `state`, `merged`, `isDraft`, `headRef`, `baseRef`, `url`, `mergeable` (`null` = not computed yet, which is unknown, not mergeable) and `reviewDecision` (`APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or `null` when reviews are not gated; an absent field is unreported and never approval). Never gate on its `ciStatus`.
3. Do the one action asked for, with only that action's prerequisites. Red CI or requested changes do not block a requested close, draft conversion, reviewer request or request-changes review.
   - Approve or request changes: only for a review you actually performed; request-changes needs a body saying what to change. Approving your own PR usually fails. (`gh pr review 42 --repo <r> --approve`, or `--request-changes --body-file <file>`.)
   - Request reviewers: the users or teams the user named (`gh pr edit 42 --repo <r> --add-reviewer <a>,<org/team>`); read back the requested list, because the forge can drop entries.
   - Ready or draft: `gh pr ready 42 --repo <r>` (this notifies reviewers), or `gh pr ready 42 --repo <r> --undo`.
   - Close or reopen: `gh pr close` / `gh pr reopen` with the same repository and number.
   - Dismiss a review: only a review the user identified or you read from the forge, with a message; never invent a review id.
4. Merge gate (merges only). Take head-bound evidence from the host tool, read now, not before your last pause:
   - `gh pr view 42 --repo <r> --json state,isDraft,mergeable,mergeStateStatus,reviewDecision,headRefName,headRefOid,baseRefName,url`. Record `headRefOid` as the intended head.
   - `gh pr checks 42 --repo <r> --required` lists the required checks on that head. Every one must have passed. Pending, failing, or a required-check list you could not read is not success.
   - Stop on: already merged (report it), closed, draft, not mergeable or mergeability unknown, `CHANGES_REQUESTED` or `REVIEW_REQUIRED`, or a head that differs from the one the user reviewed or you validated. An approval repository policy does not count (dismissed by a later push, missing a code owner) still leaves `REVIEW_REQUIRED` or a blocked merge state: that is a stop, not a technicality.
   - `forge.getCIStatus` is a cheap cross-check, never the gate: its counts cover required checks only, a zero total also appears when the required list could not be read, it is cached and can lag a minute, and it names no commit.
   - `worktree.waitForPullRequest` only says Daintree detected a PR. It is never merge readiness.
5. Preview, then merge. State the PR number, title and URL, `headRef -> baseRef`, the head SHA, the method, the required-check result and the review decision. If nobody has reviewed the change on its merits, say so (daintree.review.review-change does that when asked). Use the method the user chose or the repository's policy (`merge`, `squash`, `rebase`), asking one tight question only if neither decides; do not invent a commit title or message. Merge with the head guard:
   `gh pr merge 42 --repo <owner/name> --squash --match-head-commit <headRefOid>`
   A refused guard means the head moved: re-read, re-validate the new head, and ask again; never drop the guard to get through. With a provider that has no head guard, re-read the head immediately before merging and say the race remains.
6. Read back after every write (`gh pr view 42 --repo <r> --json state,mergedAt,mergeCommit,isDraft,reviewDecision`, or `forge.getPR`). The merge is done only when the forge reports it merged. An ambiguous or failed reply is settled by reading, never by repeating the write: an already-merged PR is the success you wanted.

"Merge it once it is green" is not a promise this book can keep by waiting. Check and merge now, or, only if the user asks for it, enable the provider's own auto-merge (`gh pr merge --auto` with the chosen method) and report it as queued, not merged. Being told of a later transition is daintree.orchestration.watch-outcome.

Report back: repository, PR number and URL, `headRef -> baseRef` and head SHA, the exact action, the evidence used (required checks, review decision, mergeability, and which tool read each), the command's result, and the state read back from the forge.
