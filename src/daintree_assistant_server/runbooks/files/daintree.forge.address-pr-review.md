---
id: daintree.forge.address-pr-review
title: Address review comments on a pull request
summary: Fetch every unresolved review thread and requested change on a PR from the forge, have an agent in the PR's worktree fix or answer each, verify its commits yourself, push, and reply or resolve threads only when asked.
whenToUse: 'Use when a pull request has review feedback ON THE FORGE — inline review threads, requested changes, bot reviewer comments — and the user wants it dealt with: "address the review comments on #88", "fix what the reviewer asked for", "work through CodeRabbit''s comments and fix the real ones", "respond to the PR feedback". The deliverable is every unresolved thread accounted for (fixed in a commit, answered, or declined with a reason), the fixes verified and pushed with CI on the new head reported as observed, and replies or resolutions posted only when asked. Do not use for findings sitting in a reviewer AGENT''s terminal (use daintree.agents.relay-review-feedback); to only read what the reviewer said or relay blockers (use daintree.workflow.follow-up-on-issue); to write a review yourself (use daintree.review.review-change); for a red check with no review comment behind it (use daintree.ci.diagnose-and-fix); for a writer agent and reviewer agent looping until approval (use daintree.orchestration.review-until-approved); for merge conflicts with the base (use daintree.git.sync-with-base); or to approve or merge afterwards (use daintree.forge.land-pull-request).'
risk: external
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - project.detectRunners
  - project.runCheck
---
An agent in the PR's worktree does the fixing: the PR's own agent if it is open there and yours (owned or handed over), else one you launch; you brief, verify and read back. Forge reads and writes are `gh … --repo <owner/name>`. Pushing is part of the job unless the user asked for local fixes only; replies, resolutions and review requests happen only when asked.

Procedure:
1. Bind the PR: `gh pr view <n> --repo <r> --json state,headRefName,headRefOid,baseRefName,isCrossRepository,headRepository,headRepositoryOwner,maintainerCanModify,url`. Closed or merged: stop. Push target: `headRefName` on `<headRepositoryOwner>/<headRepository>`. `<head remote>` and `<base remote>` are the remotes with those repositories' URLs (`git remote -v`), never `origin` by assumption. A fork with `maintainerCanModify:false`, or no write access: the fixes stay local as commits.
2. Worktree: the `worktree.list` row whose `prNumber` matches, else create `{"source":{"kind":"pullRequest","pullRequestNumber":<n>}}` and `worktree.waitUntilReady` (`timedOut:true`: call again) until `setupState:"ready"`. Ahead of `headRefOid`, dirty or diverged is someone's unpushed work: report it and stop. An agent `working` there (`terminal.list`) is mid-task: do not brief over it.
3. Fetch every thread with its state, to the last page (review bodies miss inline threads; REST comments carry no resolved flag):

```
gh api graphql --paginate -F owner=<o> -F name=<r> -F number=<n> -f query='query($owner:String!,$name:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:50,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated path line comments(first:100){totalCount nodes{databaseId author{login} body url}}}}}}}'
```

   A thread with `totalCount` over 100 is pending. Add top-level asks from `gh pr view <n> --repo <r> --json reviews,comments`. Skip resolved threads. Read each thread to its last comment: a later reply can withdraw or change the ask.
4. Ledger, one row per thread or top-level ask: thread id or URL, `file:line`, the ask in one line, and a class: change, question, nit, or wrong/out of scope. Opposite asks, or one that widens the PR's scope or changes public behaviour: ask the user first. Nits: in when cheap and uncontroversial, or the user said fix everything.
5. Brief: one `agent.launch` in that `worktreeId` (or `terminal.sendCommandOwned` to the PR's agent), `notify:true, handback:true`; end the turn. The brief: PR, `headRefOid`, the rows; "If behind, `git fetch <base remote> pull/<n>/head` then `git merge --ff-only <headRefOid>`; stop if that fails. Fix each change row narrowly, one commit per fix naming its thread. A bot's claim or an outdated thread: check the current code first; a wrong one is declined with the reason. Never delete, skip or weaken a test to satisfy a comment. Draft an answer per question row. Run the tests and lint on the final commit. Do not push, force, reply or resolve. Reply per row: commit SHA, drafted answer or reason; then the checks and results."

On notice or resume:
1. The commits are claims. Per row, `git -C <path> show <sha>` against the ask; `git -C <path> status --porcelain` clean. Re-run the checks yourself on the final commit (`project.runCheck`, `cwd` = the path). A row still open goes back once with the specific gap.
2. Push, unless local only: one send, `waitForReply:true, handback:true`: "`git push <head remote> HEAD:<headRefName>`, no force; on rejection reply with the error." A rejection means the PR moved: re-read its head and threads (new commits may settle or change rows), then brief once to rebase only its own commits onto the new head, re-run the checks and push again without force. Read back `headRefOid` (it must be the verified commit) and `gh pr checks <n> --repo <r>`, then `headRefOid` again; pending is pending.
3. Only when asked: reply with what changed and the commit, or the answer or reason (`gh api repos/<o>/<r>/pulls/<n>/comments/<first comment databaseId>/replies -f body=<text>`); resolve only threads a verified commit fixed (`gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`); re-request review with `gh pr edit <n> --repo <r> --add-reviewer <login>`. A lost reply: re-read the thread before posting again.

Stop and recovery: one corrective round per row, then it needs the user. Never reply twice to a thread, resolve one without its verified fix, or resolve a human reviewer's thread you declined.

Report back: PR, URL, head before and after; the ledger with each row's disposition and commit; checks you ran and on which commit; the push and CI on the new head; replies and resolutions as read back, or the drafted text; the agent's terminal id; rows that need the user.
