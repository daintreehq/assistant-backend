---
id: daintree.forge.land-pull-request
title: Review-state and land a pull request
summary: Apply public PR lifecycle actions and merge only after an exact-target preview, conclusive required-check evidence, review-state checks and a verified forge result.
whenToUse: Use when the user asks to approve a pull request, request changes or reviewers, mark it ready or draft, dismiss a known review, close or reopen it, or merge, squash or rebase it. Also use for "land this PR", "approve and merge", or "merge it once it is ready". Do not use merely to read PR, CI, review or comment status (use daintree.workflow.follow-up-on-issue), to review code quality and produce findings (use daintree.review.review-change), to fix red CI (use daintree.ci.diagnose-and-fix), to create the PR (use daintree.workflow.prep-branch-for-review), or to watch for a later state transition (use daintree.orchestration.watch-outcome).
confirmation: "Changing a pull request's public state. The aim is to apply exactly the action that was asked for, once its preconditions hold, and confirm the result on the pull request itself."
risk: system
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - forge.getPR
  - forge.getChecks
  - daintree.call
---
Use when: the user wants to change an existing pull request's public review state or land it.

Every PR write here is a real Daintree MCP action reached through the Assistant CLI's system-risk, always-confirmed and ungrantable `daintree.call`. Its shape is `{name, arguments, requestKey?}` with the action fields flat inside `arguments`. Evidence is not authority: green checks and an approval never grant permission to merge, approve, close, reopen, draft, or request changes.

Procedure:

1. FIX THE EXACT PR AND REPOSITORY. Resolve an exact `worktreeId` from the current context or `worktree.getCurrent`; carry it through every read and write. In one batch, read:
   - `forge.getPR({"worktreeId":"<id>","prNumber":42})`
   - `forge.getChecks({"worktreeId":"<id>","prNumber":42})`
   These two typed tools take FLAT top-level arguments and run in parallel. `forge.getPR` returns an ENVELOPE, `{ pr }` — read the fields off `pr`, not off the top level. `pr` carries the title/body, `state`, `isDraft`, `merged`, URL, `headRef`, `baseRef`, optional `mergeable`, and optional `reviewDecision`; an absent PR comes back with `pr` empty rather than as a failure. For several known numbers, batch one typed forge.getPR per number; there is no Assistant forge.getPRs wrapper. Do not use its optional `ciStatus` as the merge gate.

2. UNDERSTAND THE TWO DIFFERENT CHECK CALLS. Direct Assistant `forge.getChecks` is the typed CI ROLL-UP wrapper; internally it reads Daintree `forge.getCIStatus` and adds `conclusive`. By contrast, raw `daintree.call` with action name `forge.getChecks` reaches Daintree's PER-CHECK list. Never confuse their shapes.
   - Require BOTH `conclusive:true` and `state:"success"`. `conclusive` means the roll-up is settled enough to interpret; it does not itself mean green. Reject a positive `failed` or `pending` count and `requiredChecksPassing:false` when those fields are reported. `conclusive:false` means missing/unknown state or zero reported required checks — including the dangerous `state:"success", total:0` case. Counts cover required checks only and can lag by about a minute.
   - When identity matters, call `daintree.call({"name":"forge.getChecks","arguments":{"worktreeId":"<id>","prNumber":42}})`. It returns `{checks:[{name,status,conclusion?,required?,detailsUrl?}]|null}`. A missing conclusion is not a pass; `checks:null` means no such PR, while an empty array means no checks were reported. It provides names and links, not job-log bodies.

3. APPLY MERGE PRECONDITIONS ONLY WHEN MERGING. Red CI or changes requested do not prevent the user’s requested close, draft conversion, reviewer request or request-changes review. For a merge, stop on a bad or unknown precondition: Report already merged when `merged:true`. Do not merge a closed, draft, or `mergeable:false` PR. Treat absent/null mergeability as unknown, not success. `CHANGES_REQUESTED` and `REVIEW_REQUIRED` block merging. An ABSENT `reviewDecision` is unreported and cannot be treated as approval; an explicit `null` means Daintree reports no review gate. Do not treat a just-pushed cached success as settled evidence: the typed CI wrapper can lag by about 60 seconds and neither check read binds its result to a reported head SHA. After a recent push, wait for fresh provider evidence in a later interactive check; even then, provider branch protection is the final race safeguard.

4. APPLY THE ONE PUBLIC ACTION THE USER REQUESTED:
   - Approve: `forge.approvePR` with `prNumber` and optional `body`. Approval can fail on one's own PR.
   - Request changes: `forge.requestChanges` with `prNumber` and a REQUIRED non-empty `body` explaining the changes.
   - Request reviewers: `forge.requestReviewers` with `prNumber` plus at least one of `users:[...]` or `teams:[...]`; read its returned complete requested lists because the forge may reject entries.
   - Mark ready: `forge.markPRReadyForReview` with `prNumber`; this notifies reviewers.
   - Convert to draft: `forge.convertPRToDraft` with `prNumber`.
   - Close or reopen: `forge.closePR` / `forge.reopenPR` with `prNumber`.
   - Dismiss review: `forge.dismissReview` with positive numeric `reviewId` and non-empty `message`. Nothing on the Daintree read surface lists review IDs, so use only a user-supplied or otherwise evidenced forge review id; never invent one.
   Invoke each as `daintree.call({"name":"forge.<action>","arguments":{"worktreeId":"<id>",...}})`. Use a stable `requestKey` for approval/request-changes if an ambiguous transport result may need an identical retry. Do not combine distinct public acts under one key or one assumed consent.

5. VERIFY PUBLIC REVIEW-STATE WRITES. Approval/request-changes returns `{id,state,rawState,body,url,author?,submittedAt,commitId}`; draft toggles return `{prNumber,isDraft}`; reviewer requests return complete requested-user/team lists; close/reopen returns the normalized PR. Re-read typed `forge.getPR` after the write because one approving review may still leave `REVIEW_REQUIRED`, and a transport success is not the repository's final gate state.

6. PREVIEW A MERGE before calling it. State the exact PR number/title/URL, `headRef -> baseRef`, merge method, roll-up state and counts, `conclusive`, review decision, and mergeability. If the change has not been reviewed on its merits, say so; use `daintree.review.review-change` when the user asks for that judgment. A PR description and green CI are not a code review.

7. REQUIRE AN EXPLICIT MERGE METHOD. The action accepts `merge`, `squash`, or `rebase`; do not silently accept an invisible provider default. Use a method already chosen by the user or established repository policy. Ask one tight question only if neither resolves it. Do not invent `commitTitle` or `commitMessage`; omit them unless the user supplied them. Only after explicit merge authority and every precondition above is satisfied call:
   `daintree.call({"name":"forge.mergePR","arguments":{"worktreeId":"<id>","prNumber":42,"mergeMethod":"squash"},"requestKey":"<stable key including repository/worktree, PR and method for this logical merge>"})`.
   Do not copy a fixed example key such as `merge-pr-42-squash`: explicit deduplication is scoped by action rather than repository, so bare PR numbers can collide across repositories in one session.
   The result is `{prNumber,sha,merged,message}`. Require `merged:true`, then re-read `forge.getPR` and require `merged:true`. Report provider refusal as a blocker, not as a malformed request guess.

8. NEVER PROMISE AUTOMATIC "MERGE WHEN GREEN". `watcher.watchPR` does not observe CI or review decisions, and `daintree.call` cannot be granted to a watcher, timer or wake turn. You can check and merge interactively now, or use the watch-outcome runbook for observable PR state/activity and tell the user the eventual merge still needs a fresh check and confirmation. A reminder is not an automatic merge.

Treat `conclusive:true` as interpretable roll-up data, not head-SHA freshness or complete branch-protection evidence. A re-read after one minute can still be stale; never manufacture freshness from elapsed time alone. Preserve exact method/target authority across continuations. For an ambiguous public-write response, reconcile remote state before another attempt, and distinguish an already-landed result from a retryable failure. Do not post approval or request-changes text that misrepresents a review you never performed.

Report back: PR number/URL and head-to-base target, the exact requested lifecycle action, CI `state` plus `conclusive` and required-check counts, review decision and mergeability, the confirmed result, and the final PR state read back from the forge.
