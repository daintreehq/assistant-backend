---
id: daintree.workflow.follow-up-on-issue
title: Follow up on a forge issue
summary: Read where an issue actually stands right now — its PR, the CI roll-up, the review decision, the comment thread — then relay what is blocking it, or comment and close it out.
whenToUse: Use for a one-off READ of an issue or PR's current forge state and the writes that close it out — "is that PR green?", "did CI pass?", "what's blocking it?", "did anyone reply on that issue?", "what did the reviewer say?", "comment on it and close it". Covers checking CI or check status, reading a review decision, reading an issue's comment thread, forwarding forge-sourced blockers to the agent still working, and posting a closing comment. Do not use to leave something watching and wake the user on a LATER PR transition (use daintree.orchestration.watch-outcome); to commit, push, open or ready a PR (use daintree.workflow.prep-branch-for-review); to search or triage the backlog for new work (use daintree.workflow.find-issue-work); to start work on an issue (use daintree.workflow.start-work-on-issue); or to forward a review that is already sitting in a reviewer agent's terminal rather than on the forge (use daintree.agents.relay-review-feedback). Do not use to DIAGNOSE or FIX a failing check — reading that a check is red is here, working out why and getting it green is daintree.ci.diagnose-and-fix.
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
  - terminal.readLastMessageOwned
  - terminal.getOutput
---
Use when: the user wants to know where an issue or its PR stands on the forge right now (CI, review decision, the thread), wants forge-sourced blockers relayed to the agent still working on it, or wants the issue commented on or closed.

Availability: Hybrid. Daintree's app MCP reads `forge.getIssue`, `forge.getPR`, `forge.getCIStatus` and `forge.listPRs`; at core tier each read asks the user first, and an API-key client or a session with no Daintree connection has none, so the host tool reads instead. It does not read comment text, formal or inline review bodies, the per-check list or job logs, and it cannot comment or close: use the host's own permitted forge tool for those (for GitHub, `gh`), always with `--repo <owner/name>` and the number. A relay to the implementer uses Daintree's owned send.

Procedure:
1. Fix the issue, the PR and the repository. `worktree.list` rows carry `issueNumber`, `prNumber` and `prUrl` (cached hints). Use that worktree's `worktreeId` on every Daintree read and its repository on every host command. Read the issue with `forge.getIssue`: an empty result means no such issue; `linkedPR` may name the PR, and its absence proves nothing. To match by branch, `forge.listPRs` with `"search":"head:<branch>"`. If the user named one and nothing ties it to the other, ask rather than report on the wrong PR.
2. Read the PR and its CI roll-up:

```json tool=forge.getPR
{"worktreeId":"<resolved worktree id>","prNumber":42}
```

```json tool=forge.getCIStatus
{"worktreeId":"<resolved worktree id>","prNumber":42}
```

   `pr: null` is absence, not an error. `reviewDecision` is `APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or `null` when reviews are not gated; `null` is not anyone's approval. `ciStatus.state` is the verdict, and `ciStatus: null` means no status was reported. Its counts cover required checks only, and a zero total also appears when the required-check list could not be read, so it never means "nothing gates this". The roll-up is cached, can lag a minute, and names no commit. Do not take CI from the PR's own `ciStatus`.
3. Bind CI to the right commit when it matters ("did my fix pass?"). Get the PR head (`gh pr view 42 --repo <r> --json headRefOid`) and compare it with the commit in question (`git rev-parse HEAD` in that worktree, and whether it is pushed). If they differ, the verdict describes another commit: say so instead of calling the fix green or red. `gh pr checks 42 --repo <r>` lists each check on the current head; `--required` narrows to required ones.
4. Fetch only what the question needs, from the tool that has it:
   - The thread: `gh issue view N --repo <r> --comments` (Daintree's issue read gives only `commentCount`). Read to the newest comment before saying what the latest reply is.
   - Reviews: `gh pr view 42 --repo <r> --json reviews,latestReviews`; inline comments with `gh api repos/<owner>/<repo>/pulls/42/comments`. A review decision is a state; the review body is the feedback.
   - Why a check failed: `gh run view <run id> --repo <r> --log-failed`. Diagnosing or fixing it is daintree.ci.diagnose-and-fix.
   A status question ends here with the report. It does not authorize messaging the implementer, commenting or closing.
5. Relay blockers only when the user asked for coordination. Send the actionable content (the specific asks, failing check names, the relevant log lines), never a bare `CHANGES_REQUESTED`, with `terminal.sendCommandOwned`, after reading `terminal.getStatus`: not into an approval, trust or login prompt, not over a draft. The terminal must be one you launched or the user handed you; `RESOURCE_NOT_OWNED` means ask the user to hand it over. Add `notify:true` only when the user wants its result brought back; otherwise report the submission and stop. Feedback that sits in a reviewer agent's terminal rather than on the forge is daintree.agents.relay-review-feedback.
6. Comment and close only on an explicit request, each as its own step:
   - Re-read the issue first. If someone already closed it or posted the same note, do not add another.
   - Comment: `gh issue comment N --repo <r> --body-file <file>`. It is public, notifies watchers and cannot be quietly withdrawn, so get it right once. If the reply is lost, read the thread and look for your text before any retry.
   - Close: `gh issue close N --repo <r> --reason completed` (or `"not planned"`, as the user meant). A close-only request posts no comment.
   - When both were asked for, comment first; if the comment fails, do not close. If the close fails after the comment landed, say exactly that.
   - Read back with `gh issue view N --repo <r> --json state,stateReason,comments`.

On notice or resume (only after a relay sent with `notify`):
1. Restore the checkpoint (issue, PR, terminal, what was relayed) and apply anything the user said since. Read `terminal.getStatus` and make sure the state belongs to your relay, not to earlier work.
2. A question is answered within the relayed scope or taken to the user; an approval or login screen is the user's. When it stops at its prompt, read its reply (`lastHandback` matched by your `submissionToken`, `terminal.readLastMessageOwned`, or `terminal.getOutput`), then re-read the PR and CI: a claim that it pushed a fix is checked on the forge, not repeated.

Stop and recovery: one relay per set of blockers; a disputed send is checked with its `submissionToken`, never resent blind. One comment per request; an uncertain comment is found in the thread, not reposted. Never close on green CI or an approval alone: they are evidence, not authority.

Report back: repository, issue and PR numbers with the PR URL; the CI verdict as the roll-up stated it (state and counts, and whether it is bound to the commit in question); the review decision and any review text read; the newest thread reply and whether you read to the end; what you relayed and to which terminal; and for each write exactly what landed and what the read-back shows. Never report "closed out" when only the comment posted.
