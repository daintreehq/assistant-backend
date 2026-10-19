---
id: daintree.git.bisect-regression
title: Bisect a regression to the commit that caused it
summary: Pin a regression, failing test, broken build or slowdown to its first bad commit with git bisect in a dedicated worktree, with proven anchors, correct skip handling and a parent check, fixing nothing.
whenToUse: 'Use when something USED TO WORK and the user wants to know WHEN or WHICH COMMIT broke it: "when did this break?", "bisect it", "it worked in v2.3, find what changed", "which commit made the build start failing", "find the PR that made startup slow", "git bisect between the last release and main". The deliverable is the first bad commit (and its PR) with evidence: the test failing on it and passing on its parent, every step''s verdict, any skipped commits. Nothing is fixed. Do not use to fix the failure or explain why it fails now (use daintree.ci.diagnose-and-fix); when there is no reproduction yet to bisect with (use daintree.debug.reproduce-bug first); to measure a speed difference between two known revisions (use daintree.runtime.measure-performance); to find past pull requests by their text (use daintree.workflow.find-issue-work); or for a live production incident (use daintree.debug.investigate-outage).'
risk: git
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.list
---
Bisect moves HEAD, so it runs only in a worktree made for it, never the user's checkout or an agent's. A visible agent writes the script and runs the bisect; you fix its inputs and confirm the culprit. Only when the user asks you to bisect yourself, follow the brief.

On a host: shell steps via its command runner, or delegate; own shell: `gh -R <repo>`.

Procedure:
1. The test: one behaviour the user accepts as the good/bad line: their repro, a failing test, the build, or a threshold ("median of 5 runs above 2.0 s is bad"). None exists: reproduce first (daintree.debug.reproduce-bug). A flaky symptom is voted inside the script (bad only if it fails M of N).
2. Anchors: bad (usually the branch head) and good (a tag, a release, or `git rev-list -1 --before=<date> <branch>`). Resolve both with `git rev-parse` (testing them is the agent's step), then `git merge-base --is-ancestor <good> <bad>`; not an ancestor: the range is misleading, so ask for another good revision.
3. Worktree: `worktree.createWithRecipe` with `newBranch` `bisect/<slug>` off the bad branch, `collisionPolicy:"error"`, then, before launching, `worktree.waitUntilReady` until `ready` (`timedOut`: call again; `failed`, `timed-out` or `needs-approval`: stop and report). Reconcile an ambiguous create with `worktree.list`; never create twice.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"bisect/login-redirect","baseBranch":"<bad branch>","collisionPolicy":"error"},"focusPolicy":"preserve"}
```

4. Budget: a step cap (about log2 of the commit count plus five) or a time limit the user accepts.
5. Launch one agent: the one the user named (built-in `agentId`), else `claude`, and say which. Pass `name:"Bisect: <slug>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries the worktree path, both SHAs, the test, the budget, and these lines as written:
   - "Write the classifier script OUTSIDE the worktree (bisect checks old commits out over it). Exit 0 good, 1 the regression, 125 cannot test this commit (unrelated install or build break, a needed file missing). Map a crash to 1 only when the crash is the symptom; codes above 127 abort the bisect. An unexpected harness failure: stop and report it."
   - "Reinstall dependencies only when the lockfile changed since the last step (no lockfile: install nothing). A test file old commits lack is copied in and removed by an exit trap; `git status` must be clean before each step."
   - "Prove the anchors first (`git checkout --detach <sha>`, then back to `bisect/<slug>`): the script exits 1 on <bad> and 0 on <good>. Either disagrees: stop and report both runs."
   - "Then in the worktree: `git bisect start <bad> <good>` and `git bisect run sh <absolute script path>`. At the budget, stop and report the remaining range."
   - "Confirm the culprit: 1 on it, 0 on `<culprit>^` (each parent of a merge). Save `git bisect log` to a file outside the tree, then `git bisect reset`. Edit no tracked file, fix nothing, commit or push nothing."
   - "Reply with the script path and exit mapping, the anchor runs, the bisect log path, the first bad commit or unresolved range, and skipped commits."
6. End your turn.

On notice or resume:
- Still working: leave it. A question inside the brief: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; changing the test or the anchors is the user's call.
- The culprit is the agent's claim, never your evidence: report nothing until you have, yourself:
  1. read the saved bisect log ("only skipped commits left" is a RANGE: report it as one);
  2. in the worktree only (`cd <worktree path>`; never your or the user's checkout), run the agent's script on the culprit (expect 1) and its parent (expect 0) via `git checkout --detach <sha>`, then `git checkout bisect/<slug>`; `git status` clean;
  3. read the culprit's diff and name the hunk that changes the tested value, saying what it computes differently;
  4. looked for its PR (`gh pr list -R <owner/repo> --search <sha> --state merged`); none, or no forge, is a result.
- An interrupted bisect resumes with the same `git bisect run`, never a fresh start.

Stop and recovery: one corrective round. A culprit failing your confirmation goes to the user with both runs; re-bisecting is their call. Reconcile with `terminal.list` and `worktree.list` before any second launch or create. Leave the worktree; removing it is daintree.orchestration.workspace-cleanup, on request.

Report back: the first bad commit (sha, subject, author, PR or "no PR found") with your confirmation runs, or the unresolved range; both anchors with their proof runs; the script and its exit mapping; the bisect log and skipped commits; the likely hunk; the agent's terminal id; the worktree left behind; nothing fixed.
