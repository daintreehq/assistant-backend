---
id: daintree.review.review-change
title: Review a change
summary: Review a diff, branch or PR with independent read-only reviewers, and return de-duplicated findings with file evidence.
whenToUse: Use when the user wants a change JUDGED on its merits — "review this", "code review the branch", "is this PR any good", "check this for security problems", "what would you change about this diff", "look over what the agent wrote". The deliverable is FINDINGS with severity and file evidence. Do not use to check whether an agent did what it claimed before you trust it (use daintree.orchestration.verify-agent-work — that asks "is the claim true", this asks "is the change good"), to ready a branch or open a PR (use daintree.workflow.prep-branch-for-review), to forward findings you already have to an implementer (use daintree.agents.relay-review-feedback), to explain how existing code works (use daintree.explore.repository), or to fix a failing check (use daintree.ci.diagnose-and-fix), or when the user names a specific agent to ask and just wants its answer relayed back (use daintree.agents.ask-and-relay).
confirmation: "Reviewing the change. The goal is defects and regressions that would matter after it lands, judged against what the change is trying to do rather than style preference."
risk: project
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - git.getProjectPulse
  - forge.getPR
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.await.async
  - terminal.extract
  - terminal.summarize
  - artifact.read
---
Use when: the user wants a change reviewed — a working tree, a branch, or a PR.

You do not read the diff yourself and form an opinion. Reviewers are explore-mode agents with their own context; you set the criteria, keep them independent, and synthesize what comes back. A review assembled from fragments you skimmed is the failure this runbook prevents — it misses what it did not open, and reads as thorough anyway.

Procedure:

1. FIX WHAT IS BEING REVIEWED, exactly. `git.getProjectPulse` for the branch, uncommitted and staged changes, and recent commits; `worktree.getCurrent` for the `worktreeId` to spawn into. For a PR, `forge.getPR({"prNumber":<int>})` — note it returns `reviewDecision` but NOT CI status, so do not report on checks from it. If the user said "review this" and both a dirty tree and an open PR exist, ask which; reviewing the wrong thing wastes the whole round.

2. CHOOSE THE CRITERIA before spawning. A reviewer told "review this" returns style opinions. Name what matters for THIS change — correctness against intent, edge cases, error handling, security, tests, performance, API compatibility — and pick the two or three that fit. If the user named a concern ("check it for injection"), that is the criterion; do not dilute it with a general pass.

3. SPAWN REVIEWERS, read-only. `agentTask.spawnForEdits({"mode":"explore","worktreeId":"<id>","title":"review: <criterion>","taskPrompt":"<criterion, target, and required output shape>"})`. Explore mode INSTRUCTS the agent not to edit; it is a prompt constraint rather than a sandbox, which is why "a reviewer reported editing a file" below is a real thing to watch for and not a contradiction. Spawning is still confirmed per the active tier. Require in the prompt: a severity per finding, `file:line` evidence for every claim, and an explicit "nothing found" rather than silence.
   Pick the agent deliberately: `agentId` is OPTIONAL and defaults to one particular agent, so omitting it silently chooses for the user. Resolve the best fit from the available-agent registry in your startup context and pass it — a name the user SPOKE is only a hint, since transcription drops letters; an unknown id is rejected with the real roster and a suggestion, so re-spawn with the right id rather than the typo.
   ONE criterion per reviewer when you use several. Two reviewers with different lenses find different things; two reviewers with the same lens find the same things twice and cost double.

4. KEEP THEM INDEPENDENT — and BLIND if you will compare them. A reviewer must not see another reviewer's findings, and must not be told who wrote the code. Authorship leaks bias in both directions: "an agent wrote this" invites nitpicking, "the user wrote this" invites politeness. If you present the reviews side by side, strip the author and shuffle the order before judging, so a verdict cannot ride on which review you read first.

5. SUPERVISE with ONE mode. In-turn waiting shares an ENFORCED cumulative 120-second budget across every `terminal.awaitAll` in the turn, so wait only for a review expected to settle in a minute or two; a broad review of a large diff goes to `terminal.await.async` and you end the turn. On `budgetExhausted`, do NOT re-await — hand it to the async path. Never stack a watcher on top of a wait.

6. READ AND DE-DUPLICATE. `terminal.extract` per reviewer with a targeted question; agents answer in prose, so do not demand JSON from them. Then merge YOURSELF — that judgement is not delegable:
   - the same defect found by two reviewers is ONE finding, reported once, and the agreement is worth stating;
   - a finding only one reviewer raised still counts; independent coverage is why you ran several;
   - drop findings with no file evidence behind them, and say you dropped them.

7. RANK AND REPORT. Order by severity, not by reviewer. Each finding: what is wrong, where (`file:line`), why it matters, and what would fix it. Separate "this is a bug" from "I would have written it differently" — conflating them is how a review becomes noise the user learns to skip.

Then, only if the user asks: relay accepted findings to an implementer with the relay runbook, and check the fixes with a fresh reviewer rather than the one that found the problem — an author reviewing its own fix agrees with itself.

Notes:
- Reviewers change nothing. If a reviewer reports having edited a file, that is a bug to surface, not a result to accept.
- Never claim a review is complete when a reviewer crashed or returned nothing. Say which lens went unexamined.
- Do not spawn a reviewer for a two-line diff you can see in the pulse output. Say what you see and offer a deeper pass.
- A review with no findings is a legitimate result. Report it plainly rather than manufacturing something to justify the round.

Report back: findings ranked by severity with `file:line` evidence, which criteria were covered and by how many reviewers, anything a reviewer could not assess, and the terminal ids you spawned.
