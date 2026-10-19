---
id: daintree.git.resolve-semantic-conflict
title: Resolve a behaviour clash between branches
summary: When two branches each pass alone but behave wrongly once combined, pin the clash as a failing test on the combined tree, gather each side's intent, have the user decide the combined behaviour, and have an integrator implement it with a test that holds it.
whenToUse: 'Use when two branches or agents'' finished work each pass on their own but behave wrongly TOGETHER — "both branches pass on their own but together checkout totals are wrong", "the auth branch and the session branch implement incompatible token lifetimes", "the merge was clean but the combined behaviour is broken", "integration held back a clash where both sides changed the same logic". The deliverable is the intended combined behaviour decided (by the user when the authors'' intents truly conflict), implemented on the integration branch by an integrator agent with a test that pins it, and each side''s own tests still passing there. Do not use for the merge mechanics of combining branches (use daintree.git.integrate-agent-branches), for textual conflicts with a base (use daintree.git.sync-with-base), to predict conflicts before merging (use daintree.worktree.compare-worktrees), when two agents are still WORKING on the same thing (use daintree.orchestration.resolve-task-collisions), to judge one change on its merits (use daintree.review.review-change), or for a failing check with one intended behaviour (use daintree.ci.diagnose-and-fix).'
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - agent.launch
  - terminal.sendCommandOwned
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
  - terminal.sendCommandMany
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You freeze, gather intent, put the decision to the user and verify; one integrator agent reproduces the clash, then implements the decision. You never pick a side; nothing is resolved in a source worktree.

On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Freeze each side: A and B to SHAs (`git rev-parse <ref>`), their worktrees from `worktree.list`, no agent `working` there (`terminal.list` with each `worktreeId`), each `git -C <path> status --porcelain` recorded. The combined tree: the existing integration worktree if both are merged there, it is clean and idle, no merge or rebase in progress; I0 = its HEAD. Otherwise create one off the base SHA and call `worktree.waitUntilReady` with the returned `worktreeId`, at most three calls, until `setupState` is `ready` before any agent works there; else report id, state, error and stop; never a second create. Record the branch and R0 = `git ls-remote origin refs/heads/<branch>`.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"integrate/<name>","baseBranch":"<base SHA>","collisionPolicy":"error"}}
```

2. Round one, reproduce: reuse the integration agent you own (`terminal.sendCommandOwned`), or, once `ready`, launch one there by `worktreeId`:

```json tool=agent.launch
{"agentId":"claude","name":"Integrate: <name>","worktreeId":"<integration worktree id>","prompt":"<brief>","notify":true,"handback":true}
```

   The brief: path, A and B with SHAs, the symptom in the user's words, and "If not merged yet, merge <A> then <B> with `git merge --no-ff --no-edit`. A textual conflict that needs this behaviour decided: `git merge --abort`, report both versions, stop. Otherwise write a persistent failing test or repro script showing expected against actual, and commit only that. Report its exact command and output, the merged HEAD before that commit, and what each side's code does in that area with file:line and the commits responsible. Change no behaviour. Cannot reproduce: report what you tried and stop."
3. In the same turn, intent per side, labelled by source. Author agents open and yours: one `terminal.sendCommandMany` with `waitForReply:true, handback:true`: "Integration found: <symptom>. What behaviour did you intend for <area>, and what in your branch relies on it (tests, callers)? Do not change your branch." Otherwise: `git log <base>..<A>` messages, the PR, the issue, each one's brief. Then end the turn.
4. On round one's notice: unset I0 is the merged HEAD it reports; a merge it aborted: the two versions are the evidence, and I0 is the HEAD round two merges onto. Otherwise run the repro yourself: it must fail on the combined tree for the reason given. Not reproduced: report what was tried and stop; no fix, no claimed clash. A combined behaviour the user already stated is the decision. Otherwise put the options: A wins, B wins, or a combination (named precisely, such as a per-context rule), each with what it breaks and which tests would change. Recommend one only if it meets both stated intents.
5. Round two, one send to the integrator:

```json tool=terminal.sendCommandOwned
{"terminalId":"<integrator terminal id>","command":"Decision from the user: <decision verbatim>. Implement it on this branch only; a merge you aborted: merge again and resolve it this way. Keep your repro as the test that pins it. Change an existing test only where this decision changes what it asserts, and name each; never delete or loosen one to get green. Run A's and B's own test files and the full suite. Commit; no source branch, push or PR. Report changes, tests changed and why, commands, results.","notify":true,"handback":true}
```

On notice or resume:
1. A newly found behaviour choice goes to the user.
2. Verify yourself: `git status` clean, no merge in progress; A and B both ancestors of HEAD (`git merge-base --is-ancestor`); the pinning test exists and asserts the decided behaviour (read it); `git diff <I0> HEAD` on test files changes only what the decision justified, deletes none; that test, A's and B's tests and the full suite re-run (`project.runCheck`, `runnerId` from `project.detectRunners`, `cwd` the path); A and B still at their frozen SHAs, their worktrees' status as recorded; the remote ref still R0.

Stop and recovery: one corrective round per round, then the user. Never relaunch the integrator while its terminal lives, never re-ask a decision unless new evidence changes it. A user who does not decide: stop with the repro committed and the options written up.

Report back: A and B with SHAs; the clash as expected against actual; each side's intent and source; options and decision; integrator terminal id; integration worktree id, path, branch, I0, HEAD; the pinning test, tests changed and why, suites re-run; R0 before and after; any decision or operation left open.
