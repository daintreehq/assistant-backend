---
id: daintree.workflow.follow-up-on-issue
title: Follow up on a forge issue
summary: Read where an issue actually stands right now — its PR, the CI roll-up, the review decision, the comment thread — then relay what is blocking it, or comment and close it out.
whenToUse: 'Use for a one-off READ of an issue or PR''s current forge state and the writes that close it out — "is that PR green?", "did CI pass?", "what''s blocking it?", "did anyone reply on that issue?", "why is this issue still open?", "what did the reviewer say?", "comment on it and close it". Covers checking CI or check status, reading a review decision, reading an issue''s comment thread, forwarding forge-sourced blockers to the agent still working, and posting a closing comment. Do not use to leave something watching and wake the user on a LATER PR transition (use daintree.orchestration.watch-outcome); to commit, push, open or ready a PR (use daintree.workflow.prep-branch-for-review); to search or triage the backlog for new work (use daintree.workflow.find-issue-work); to start work on an issue (use daintree.workflow.start-work-on-issue); to fix or answer the review threads left on a PR (use daintree.forge.address-pr-review); for a standup or weekly summary across many PRs (use daintree.git.summarize-activity); to grade an issue''s acceptance criteria against the code (use daintree.review.audit-spec-coverage); or to forward a review that is already sitting in a reviewer agent''s terminal rather than on the forge (use daintree.agents.relay-review-feedback). Do not use to DIAGNOSE or FIX a failing check — reading that a check is red is here, working out why and getting it green is daintree.ci.diagnose-and-fix.'
risk: external
requiredTools:
  - forge.getIssue
  - forge.getPR
  - forge.getCIStatus
optionalTools:
  - worktree.list
  - forge.listPRs
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Daintree's forge reads ask first at core tier and are absent for API-key clients; comment text, reviews, per-check lists, logs, comments and closing go through the host forge tool (`gh`), always with `--repo <owner/name>` and the number.

1. Fix the issue, PR and repository. `worktree.list` rows carry cached `issueNumber`, `prNumber`, `prUrl`; use that worktree's id on Daintree reads. `forge.getIssue` empty means no such issue; a missing `linkedPR` proves nothing; match by branch with `forge.listPRs` `"search":"head:<branch>"`. If nothing ties the user's issue to a PR, ask rather than report on the wrong one. A PR-only question skips the issue read.
2. Read `forge.getPR` and `forge.getCIStatus` (`worktreeId`, `prNumber`). `pr: null` is absence. `reviewDecision: null` means reviews are not gated, never approval. `ciStatus: null` means nothing reported; counts cover required checks only, and a zero total also appears when the required list could not be read. The roll-up is cached, can lag a minute and names no commit. Never take CI from the PR's own `ciStatus`.
3. "Did my fix pass?": compare the PR head (`gh pr view N --repo <r> --json headRefOid`) with the commit in question (`git rev-parse HEAD` there, and whether it is pushed). Different: the verdict is another commit's. Same: read that commit's own checks (`gh pr checks N --repo <r>`, `gh run list --repo <r> --commit <sha>`); report a roll-up without them as unbound.
4. Fetch only what the question needs: the thread (`gh issue view N --repo <r> --comments`, read to the newest comment); reviews (`gh pr view N --json reviews,latestReviews`; inline comments with `gh api --paginate repos/<o>/<r>/pulls/N/comments`); a failed check's log (`gh run view <id> --log-failed`; fixing it is daintree.ci.diagnose-and-fix). A status question ends here: it authorizes no relay, comment or close.
5. Relay blockers only when asked: the specific asks, failing check names and log lines, never a bare `CHANGES_REQUESTED`, to a terminal you launched or were handed, after `terminal.getStatus` shows it at a prompt. If the user wants the result back, send with `notify:true, handback:true` and end your turn; otherwise report the submission and stop. Feedback sitting in a reviewer agent's terminal is daintree.agents.relay-review-feedback.
6. Comment and close only on explicit request. Re-read first; skip a note or close someone already made. Comment with `gh issue comment N --repo <r> --body-file <f>` (public, notifies watchers); a lost reply is checked in the thread before any retry. Close with `gh issue close N --repo <r> --reason completed` (or `"not planned"`); a close-only request posts no comment. Both: comment first, and a failed comment means no close. Read back with `gh issue view N --json state,stateReason,comments`.

On notice or resume (only after a relay with `notify`): answer a question within the relayed scope or take it to the user; a claim that it pushed a fix is checked by re-reading the PR and CI, not repeated.

Stop and recovery: one relay per set of blockers, one comment per request. Never close on green CI or an approval alone: they are evidence, not authority.

Report back: repository, issue and PR with URL; the CI verdict (state, counts, and whether bound to the commit in question); the review decision and any review text read; the newest thread reply and whether you read to the end; what you relayed and where; each write as read back. Never report "closed out" when only the comment posted.
