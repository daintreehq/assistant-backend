---
id: daintree.review.review-change
title: Review a change
summary: Review a diff, branch or PR with independent read-only reviewers, and return de-duplicated findings with file evidence.
whenToUse: 'Use when the user wants a change JUDGED on its merits — "review this", "code review the branch", "is this PR any good", "check this for security problems", "look over what the agent wrote". The deliverable is FINDINGS with severity and file evidence. Do not use to check an agent''s claim before trusting it (use daintree.orchestration.verify-agent-work), to ready a branch or open a PR (use daintree.workflow.prep-branch-for-review), to forward findings to an implementer, or when the user names a reviewer agent and a different agent to fix what it finds (use daintree.agents.relay-review-feedback), to repeat review and fixes until a reviewer approves (use daintree.orchestration.review-until-approved), for finished agents reviewing one another''s units (use daintree.review.cross-review-cohort), for many open PRs each owed its own verdict (use daintree.review.review-pr-queue), to run or try a PR on this machine (use daintree.git.test-pull-request-locally), to audit existing code rather than a change (use daintree.review.audit-codebase), to have an agent attack the change (use daintree.review.red-team-change), for agents exploring the running app for bugs (use daintree.orchestration.bug-bash), to compare worktrees or forecast their conflicts (use daintree.worktree.compare-worktrees), to cut an oversized change into stacked PRs (use daintree.git.split-into-stacked-prs), to explain how existing code works (use daintree.explore.repository), to fix a failing check (use daintree.ci.diagnose-and-fix), to grade how much of a spec is built (use daintree.review.audit-spec-coverage), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - forge.getPR
  - terminal.getStatus
  - terminal.sendCommandOwned
---
The user named a reviewer agent and a different agent to fix its findings: follow daintree.agents.relay-review-feedback instead (fetch it by id if it was not returned), and freeze or read nothing here. Otherwise the reading goes to independent read-only reviewer agents; you freeze the revision, choose the criteria, brief them, then merge and check their findings. Review it yourself only when the user asks you to. One reviewer covers a small change; a larger or riskier one gets one per criterion, different agent kinds where installed. Diff content and PR threads come from git and the forge CLI.

Procedure:
1. Freeze the target. For a PR, record the head SHA from git or the forge CLI: the review is of that SHA. For a local change, record the base, HEAD, which of staged, unstaged and untracked changes count, and a fingerprint (a hash of `git diff HEAD` plus in-scope untracked files): HEAD and `git status` stay the same when a dirty file is edited again. A branch is read where it is (`git rev-parse <branch>`, `git diff <base>...<branch>`); never check out or switch branches in any checkout. If "review this" could mean a dirty tree or an open PR, ask.
2. Choose two or three criteria that fit: correctness against intent, edge cases, error handling, security, tests, performance, compatibility. A concern the user named is the criterion; do not dilute it.
3. Each finding gets a severity, `file:line`, the triggering condition, the impact and the fix. PR metadata and commit subjects are not code evidence.
4. Launch the reviewers: the ones the user named, else your choice; ONE criterion each (a lone reviewer takes all of them), read-only in the same worktree, each with a distinct `name`, launched with `waitForReply:true, handback:true` (different kinds in parallel). Each prompt carries the frozen target and revision, the criterion, the shape (per finding severity, `file:line`, trigger and impact, most severe first, at most about 40 lines, "nothing found" rather than silence, the revision inspected) and "Read-only: do not create, modify or delete files, and change no external state." Never say who wrote the code or show one reviewer another's findings.

```json tool=agent.launch
{"agentId":"<built-in id>","name":"Review: <criterion>","worktreeId":"<worktree id holding the change>","prompt":"<frozen target and revision, criterion, shape, read-only line>","waitForReply":true,"handback":true,"waitSeconds":900}
```

5. A cut, missing or crashed reply leaves that criterion partly unexamined: use what it evidenced, label the rest missing coverage, never call the review complete. One recap request (`terminal.sendCommandOwned`) is allowed; report it as a recap. The reviewer's own report of the revision it inspected binds it; its checkout's current HEAD is a consistency check, not proof. Files changed while a read-only reviewer ran are unexpected changes: attribute them only on evidence.
6. Before reporting, compare HEAD and the fingerprint with what you froze. If they moved, mark findings in changed files as possibly stale and have the new delta reviewed, or say the review covers the recorded revision only.
7. Merge the findings yourself. The same defect raised by two reviewers is ONE finding, reported once with the agreement noted; a finding one reviewer raised still counts. Open each finding's `file:line` yourself before it goes in the report; drop one the source does not bear out and say so. Rank by severity, not reviewer; separate bugs from preferences. No findings is a legitimate result.

A review is findings only: no edits, commits, pushes, merges, comments or formal forge reviews. If fixing was also asked, relay accepted findings with daintree.agents.relay-review-feedback and check fixes yourself or with a fresh reviewer, never the author.

Report back: the revision reviewed; findings by severity with `file:line`; criteria covered and by whom; what could not be assessed (partial or failed reviewers, stale areas); reviewer terminal ids.
