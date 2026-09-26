---
id: daintree.review.review-change
title: Review a change
summary: Review a diff, branch or PR with independent read-only reviewers, and return de-duplicated findings with file evidence.
whenToUse: Use when the user wants a change JUDGED on its merits — "review this", "code review the branch", "is this PR any good", "check this for security problems", "what would you change about this diff", "look over what the agent wrote". The deliverable is FINDINGS with severity and file evidence. Do not use to check whether an agent did what it claimed before you trust it (use daintree.orchestration.verify-agent-work — that asks "is the claim true", this asks "is the change good"), to ready a branch or open a PR (use daintree.workflow.prep-branch-for-review), to forward findings you already have to an implementer (use daintree.agents.relay-review-feedback), to explain how existing code works (use daintree.explore.repository), or to fix a failing check (use daintree.ci.diagnose-and-fix), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - forge.getPR
  - agent.listAvailable
  - agent.launch
  - terminal.list
  - terminal.getStatus
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.sendCommandOwned
---
Use when: the user wants a change reviewed: a working tree, a branch, or a PR.

Availability: Hybrid. You can read the diff and the surrounding source yourself with git and your file tools; that is the default for a requested review. `forge.getPR` gives a PR's base and head branches, draft state and review decision (it raises an approval in a core pane). Diff content, PR review threads and anything published to the forge come only from git and the host's permitted forge CLI. Use independent reviewers, as visible Daintree agents, when the user asks for them or names agents, or when a large or risky change justifies separate lenses and the user agrees to them; each continues on its own notice.

Procedure:
1. Freeze what is reviewed. Resolve the worktree id and path with `worktree.list`. For a PR, read `baseRef` and `headRef`, fetch, and record the head SHA from git or the forge CLI: the review is of that SHA. For a local change, record the base, `git rev-parse HEAD`, whether staged, unstaged and untracked changes are included, and a fingerprint of that content (a hash of `git diff HEAD` plus the in-scope untracked files): HEAD and `git status` stay the same when a dirty file is edited again. If "review this" could mean a dirty tree or an open PR and the conversation does not already say which, ask.
2. Choose two or three criteria that fit this change: correctness against intent, edge cases, error handling, security, tests, performance, compatibility. A concern the user named is the criterion; do not dilute it with a general pass.
3. Review directly: read the whole diff and enough surrounding code to judge it. Each finding gets a severity, `file:line`, the concrete triggering condition, the impact and what would fix it. PR metadata and commit subjects are not code evidence.
4. With independent reviewers, give each ONE criterion. Launch them read-only in the same worktree (readers may share a checkout), one after another for the same agent kind. Each prompt carries the frozen target (path, base, head SHA, whether uncommitted changes count), the criterion, the output shape (severity, `file:line`, trigger and impact per finding, most severe first, short enough to read back whole, an explicit "nothing found" rather than silence, and the revision it inspected), and "Read-only: do not create, modify or delete files, and change no external state." Never say who wrote the code, and never show one reviewer another's findings. Record the target, criteria and terminal ids in a checkpoint, then end the turn.

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"review: <criterion>","worktreeId":"<resolved worktree id>","prompt":"<target and revision, one criterion, output shape, read-only boundary>","handback":true,"notify":true}
```

5. Before reporting, check the target has not moved: compare HEAD and the fingerprint with what you recorded. If it changed, mark findings in the changed files as possibly stale and review the new delta, or state that the review covers the recorded revision only.
6. Merge the findings yourself. The same defect raised by two reviewers is ONE finding, reported once, with the agreement noted. A finding only one reviewer raised still counts. Check each finding against the source before reporting it; drop one with no evidence and say you dropped it. When weighing reviews against each other, strip reviewer identity and shuffle their order first.
7. Rank by severity, not by reviewer. Separate "this is a bug" from "I would have written it differently". No findings is a legitimate result; do not manufacture one.

A review is findings only. Do not edit the code, commit, push, merge, post comments or submit a formal forge review because a review was requested. If fixing is also in the request, relay accepted findings with daintree.agents.relay-review-feedback, and check the fixes yourself or with a fresh reviewer, never the author.

On notice or resume:
1. Follow the foundation's resume procedure for each reviewer that stopped. Read its full report: `terminal.readLastMessageOwned` for a Claude Code pane, otherwise `terminal.getOutput`, which is only a tail (at most 1000 lines within 50 KiB, no paging back). A report whose start is not in what you read is partial, however neat its last line.
2. A partial report, a reviewer still working, a crash or an exit leaves that criterion partly or wholly unexamined. Use what it evidenced, label the rest as missing coverage, and never call the review complete. One request for a recap is allowed (`terminal.sendCommandOwned`, with `notify`); report it as a recap, not the full review.
3. Confirm from its report that the reviewer inspected the recorded revision; its checkout's current HEAD is a consistency check, not proof. Files that changed while a read-only reviewer ran are unexpected changes: the user or another process may have made them, so attribute them only on evidence, and do not accept them as a result.
4. When every reviewer has reported or been accounted for, run steps 5 to 7. A repeated notice for a reviewer already read changes nothing.

Stop and recovery: one reviewer per criterion, one round. A reviewer that fails is reported as a coverage gap, not silently relaunched. An ambiguous launch is reconciled with `terminal.list`, never repeated.

Report back: the revision reviewed, findings ranked by severity with `file:line` evidence, which criteria were covered and by whom, what could not be assessed (partial or failed reviewers, stale areas), and the terminal ids of any reviewers.
