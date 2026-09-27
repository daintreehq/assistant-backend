---
id: daintree.review.review-change
title: Review a change
summary: Review a diff, branch or PR with independent read-only reviewers, and return de-duplicated findings with file evidence.
whenToUse: 'Use when the user wants a change JUDGED on its merits — "review this", "code review the branch", "is this PR any good", "check this for security problems", "what would you change about this diff", "look over what the agent wrote", "re-audit the fix with fresh Codex sessions for second-order effects". The deliverable is FINDINGS with severity and file evidence. Do not use to check whether an agent did what it claimed before you trust it (use daintree.orchestration.verify-agent-work — that asks "is the claim true", this asks "is the change good"), to ready a branch or open a PR (use daintree.workflow.prep-branch-for-review), to forward review findings to an implementer, or when the user names a reviewer agent and a different agent to fix what it finds (use daintree.agents.relay-review-feedback), to explain how existing code works (use daintree.explore.repository), or to fix a failing check (use daintree.ci.diagnose-and-fix), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).'
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - forge.getPR
  - agent.launch
  - terminal.getStatus
  - terminal.sendCommandOwned
---
The user named a reviewer agent and a different agent to fix its findings: follow daintree.agents.relay-review-feedback instead (fetch it by id if it was not returned), and freeze or read nothing here. Otherwise review directly by default: read the diff and surrounding source yourself. Use independent reviewers, as visible Daintree agents, when the user asks for or names them, or a large or risky change justifies separate lenses and the user agrees. Diff content and PR threads come from git and the forge CLI.

Procedure:
1. Freeze the target. For a PR, record the head SHA from git or the forge CLI: the review is of that SHA. For a local change, record the base, HEAD, which of staged, unstaged and untracked changes count, and a fingerprint (a hash of `git diff HEAD` plus in-scope untracked files): HEAD and `git status` stay the same when a dirty file is edited again. If "review this" could mean a dirty tree or an open PR, ask.
2. Choose two or three criteria that fit: correctness against intent, edge cases, error handling, security, tests, performance, compatibility. A concern the user named is the criterion; do not dilute it.
3. Each finding gets a severity, `file:line`, the triggering condition, the impact and the fix. PR metadata and commit subjects are not code evidence.
4. Reviewers: ONE criterion each, read-only in the same worktree, launched with `waitForReply:true, handback:true` (different kinds in parallel). Each prompt carries the frozen target and revision, the criterion, the shape (per finding severity, `file:line`, trigger and impact, most severe first, at most about 40 lines, "nothing found" rather than silence, the revision inspected) and "Read-only: do not create, modify or delete files, and change no external state." Never say who wrote the code or show one reviewer another's findings.
5. A cut, missing or crashed reply leaves that criterion partly unexamined: use what it evidenced, label the rest missing coverage, never call the review complete. One recap request (`terminal.sendCommandOwned`) is allowed; report it as a recap. The reviewer's own report of the revision it inspected binds it; its checkout's current HEAD is a consistency check, not proof. Files changed while a read-only reviewer ran are unexpected changes: attribute them only on evidence.
6. Before reporting, compare HEAD and the fingerprint with what you froze. If they moved, mark findings in changed files as possibly stale and review the new delta, or say the review covers the recorded revision only.
7. Merge yourself. The same defect raised by two reviewers is ONE finding, reported once with the agreement noted; a finding one reviewer raised still counts. Check each against the source; drop one with no evidence and say so. Rank by severity, not reviewer; separate bugs from preferences. No findings is a legitimate result.

A review is findings only: no edits, commits, pushes, merges, comments or formal forge reviews. If fixing was also asked, relay accepted findings with daintree.agents.relay-review-feedback and check fixes yourself or with a fresh reviewer, never the author.

Report back: the revision reviewed; findings by severity with `file:line`; criteria covered and by whom; what could not be assessed (partial or failed reviewers, stale areas); reviewer terminal ids.
