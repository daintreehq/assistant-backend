---
id: daintree.review.review-pr-queue
title: Review a queue of open pull requests
summary: Put one read-only reviewer on each of several open pull requests, each in that PR's own worktree at a recorded head commit, and return a verdict per PR with checked findings, posting nothing unless asked.
whenToUse: 'Use when SEVERAL open pull requests each need a review of their own — "review all the open PRs", "go through the review queue", "put an agent on each PR waiting for review", "which of these PRs are good to merge and which need work?", "review everything opened against develop this week". The deliverable is a verdict per PR (ready, needs changes, cannot assess) with file evidence, bound to the head commit its reviewer saw; nothing is posted to the forge unless asked. Do not use for ONE change or PR, even with several reviewers on it (use daintree.review.review-change); for agents reviewing EACH OTHER''s work (use daintree.review.cross-review-cohort); to run or try a PR hands-on (use daintree.git.test-pull-request-locally); for a pile of Dependabot or Renovate bumps (use daintree.forge.batch-dependency-updates); to approve, request changes or merge afterwards (use daintree.forge.land-pull-request); to only list or search PRs (use daintree.workflow.find-issue-work); or to hand out implementation work (use daintree.orchestration.fan-out-work).'
risk: project
requiredTools:
  - forge.listPRs
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Findings only: no edits, commits, pushes, approvals or comments unless asked. Per-PR reads use `gh … --repo <owner/name>`. API-key client (no forge reads, `notify` or `worktree.waitUntilReady`): list with `gh pr list`, read `setupState` from `worktree.list`, run one reviewer at a time with `waitForReply:true, handback:true`. `<remote>` is the one whose URL is `<owner/name>` (`git remote -v`; `upstream` when `origin` is a fork).

On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. The set: the PRs the user named, else one `forge.listPRs` with their filter (`state:"open"`, `search:"-is:draft review:required"` or `"base:develop created:>=<date>"`, `perPage` up to 30); `hasMore`: say so. Leave bot dependency bumps out unless named. At most four reviewers at once (or the user's number); the rest follow in later rounds with no new yes.
2. Freeze each PR: `gh pr view <n> --repo <r> --json number,title,body,author,baseRefName,headRefName,headRefOid,isDraft,state,isCrossRepository`. Skip closed, merged and draft PRs unless named. `headRefOid` is the commit the verdict binds to.
3. One worktree per PR. Reuse a `worktree.list` row whose `prNumber` matches only when clean and `git -C <path> rev-parse HEAD` equals `headRefOid`; one that differs: leave it untouched, go diff-only. Else create with `{"source":{"kind":"pullRequest","pullRequestNumber":<n>}}` and call `worktree.waitUntilReady` on its `worktreeId` (`timedOut:true`: call again) until `setupState:"ready"` (`needs-approval`: the user approves it in Daintree; `failed`: "cannot assess"). A fetch error naming the head branch: `git fetch <remote> pull/<n>/head:pr-<n>`, then create with `existingBranch` `pr-<n>`. Then `git -C <path> fetch <remote> <base>`; HEAD must equal `headRefOid`.
   Outside contributor or fork (`isCrossRepository`, or an author the user does not vouch for): creating its worktree runs the project's setup commands, often an install, on their code. Say so; without the user's yes, go diff-only.
   Diff-only: the reviewer runs in a worktree you already have; its prompt says the change is `gh pr diff <n> --repo <r>` at `headRefOid`, read against `<remote>/<base>` source, and that this checkout's `HEAD` is not the PR.
4. One `agent.launch` per PR, separate calls issued together (`agent.launchMany` shares one prompt), each with its `worktreeId`, a distinct `name` and a fresh `requestedId`, then end the turn:

```json tool=agent.launch
{"agentId":"codex","name":"Review #<n>","worktreeId":"<PR worktree id>","requestedId":"<run tag>-pr-<n>","prompt":"<intent, base, head, criteria, shape, read-only line>","notify":true,"handback":true}
```

   The prompt: PR number, title and body as the stated intent; base and `headRefOid`; "the change is `git diff <remote>/<base>...HEAD`"; criteria (correctness against that intent, tests for the change, security, plus any the user named); shape (first line `VERDICT: ready`, `needs changes` or `cannot assess`; then findings most severe first, each with severity, `file:line`, trigger and impact; "nothing found" rather than silence; the commit inspected; at most about 40 lines); "Read-only: do not create, modify or delete files, run no install or build, post nothing to the forge, and change no external state." Never name the author or mention other PRs.

On notice or resume:
1. Map the terminal to its PR by terminal id or `requestedId`. The verdict comes from the notice's quote.
2. Check every blocking finding against the source at that commit yourself; drop one without evidence and say so. Files changed in the worktree (`git -C <path> status --porcelain`) are a boundary failure to report.
3. CI is separate evidence: `gh pr checks <n> --repo <r>`, reported beside the verdict, never folded into it. It names no commit: re-read `headRefOid` after it. Moved means both cover an older head: label them stale and offer a review of the new commits.
4. As a slot frees, launch the next PR as in step 4.

Posting, only when asked: one `gh pr review <n> --repo <r> --comment --body-file <f>` per PR with checked findings, read back with `gh pr view <n> --repo <r> --json reviews`. Approve or request changes is daintree.forge.land-pull-request.

Stop and recovery: one recap request (`terminal.sendCommandOwned`) per cut or missing reply, then "cannot assess". An ambiguous launch is reconciled by `requestedId` in `terminal.list`, never relaunched. Close a reviewer once its verdict is checked; worktrees stay until asked (daintree.orchestration.workspace-cleanup).

Report back: per PR its number, title, author, head reviewed and whether it moved, verdict, top findings with `file:line`, CI for that head, and what was not assessed; reviewer terminal and worktree ids; PRs skipped and why; whether the listing was capped; anything posted, as read back.
