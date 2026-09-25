---
id: daintree.ci.diagnose-and-fix
title: Diagnose and fix a failing check
summary: Take a red test, build or PR check from symptom to verified green — evidence first, fix second, independent verification last.
whenToUse: Use when something is FAILING and the user wants it working — "the tests are failing", "why is CI red", "the build broke", "this check won't pass", "fix the failing test", "is this flaky?" — including the fix-and-verify loop that follows. Do not use to judge the quality of a change that already passes (use daintree.review.review-change), to confirm an agent did what it claimed (use daintree.orchestration.verify-agent-work), to read a PR's review state or comment on its issue without a failure to fix (use daintree.workflow.follow-up-on-issue), to ready a branch for review (use daintree.workflow.prep-branch-for-review), or when the "failure" is Daintree itself misbehaving rather than the project (use daintree.diagnostics).
risk: project
requiredTools:
  - worktree.list
optionalTools:
  - forge.getCIStatus
  - forge.getPR
  - project.detectRunners
  - project.runCheck
  - agent.listAvailable
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - terminal.sendCommandOwned
---
Use when: a test, build or PR check is failing and the user wants it working.

Availability: Hybrid. You read logs, reproduce, edit and re-run checks yourself with your own shell and file tools, in the verified checkout, when the user asked for a fix. From the app: `worktree.list` for the target, the PR roll-up (`forge.getCIStatus`, `forge.getPR`) and detected project commands (`project.detectRunners`, `project.runCheck`). In a core pane each of those except `worktree.list` raises an approval to the user, and an API-key client has none of them. The app gives no per-check list, no job log and no head commit SHA: get those from the host's permitted forge CLI (for GitHub: `gh pr checks`, `gh run view --log-failed`, `gh pr view --json headRefOid`). Without one, say which evidence you could not read. A visible Daintree agent is optional: use one when the user asked for it or wants the fix run separately.

Evidence comes before hypothesis. A fix written from a check's name is a guess, and a guess that makes the symptom vanish without fixing the cause is worse than the failure. "Tests pass now" from whoever wrote the fix is a claim, not evidence.

Procedure:
1. Fix the target. Resolve the worktree id and path with `worktree.list` (it also shows a linked PR), then in that path read the branch, `git rev-parse HEAD` and whether the tree is dirty. Record this starting revision. Run every command there, never in your own checkout if it differs.
2. For a red PR check, read the roll-up with `worktreeId` and the PR number, then bind it to a commit. `state` is the answer (`success`, `failure`, `pending`, `neutral`, `unknown`), and `ciStatus:null` is no reading. Counts cover required checks only, and a zero total also appears when the required list could not be read, so it never means nothing gates the merge. Readings are cached for about a minute. The roll-up does not say which commit it describes: read the PR's head SHA with the forge CLI and compare it with the revision in question. A result for another commit, a green one included, says nothing about this code.
3. Get the actual failure: the failing command and its output, from the failed job's log, the terminal that ran it, or your own reproduction. A check name is not evidence. If the failure exists only in a log you could not read, say so and never invent its content. A check that timed out or was cancelled failed to finish; it is not a code failure until its log shows one, and re-running it is the user's decision.
4. Reproduce with the narrowest command that shows the failure, in the target path. Your shell is fine. `project.runCheck` is the app alternative: take `runnerId` from `project.detectRunners` and check what it runs first (detection lists publish, deploy and server scripts too), take `projectId` from `actions.getContext`, always pass `cwd`, and never point it at a long-lived server.

```json tool=project.runCheck
{"projectId":"<project id>","runnerId":"<runner id from project.detectRunners>","cwd":"<target worktree path>"}
```

   Read `passed`, not the call's success. `timedOut` or `aborted` is incomplete execution, not a defect, and `outputTruncated` means the output is partial. Report the returned `command`: it is what actually ran.
5. Diagnose to a root cause with `file:line` evidence; skip a deep investigation when the error already names the cause. If the same command passes and fails on unchanged code, it is flaky: do not "fix" it. Report the runs and their outcomes and ask whether to stabilise or quarantine it. One intermittent failure is not permission to quarantine anything.
6. Fix narrowly, bound to the command and the behaviour it asserts, not to the symptom. Never delete, skip or weaken a test, loosen an assertion, lower a coverage threshold or exclude code from coverage to get green unless the user asked for exactly that. If the real question is whether the test or the behaviour is wrong, stop and ask: picking one commits the user to a position. One cause is one fix; unrelated failures get separate fixes.
7. Verify yourself: re-run the narrow failing command and the relevant broader suite in the same path, and record the revision they ran against. That proves the local change. Provider CI proves it only after the fix is pushed (only when the user authorized committing and pushing) and a completed run is read for that exact head SHA. Until then, say CI has not run on the fix. Never report green from a single cached reading.

Delegating: record the checkpoint, then launch one agent in the target worktree. Its prompt carries the exact failing command and error, your diagnosis if any, the scope ("fix the cause; do not delete, skip or weaken tests or coverage"), acceptance ("<command> passes, the broader suite still passes, the asserted behaviour is still enforced"), reporting (files changed, commands run with results) and boundaries (no commit or push unless the user asked).

```json tool=agent.launch
{"agentId":"<id from agent.listAvailable>","name":"fix: <check name>","worktreeId":"<target worktree id>","prompt":"<failing command, error, diagnosis, scope, acceptance, reporting, boundaries>","handback":true,"notify":true}
```

Then end the turn. The notice means the agent stopped, not that the check passes.

On notice or resume:
1. Follow the foundation's resume procedure. Read the report with `terminal.readLastMessageOwned` for a Claude Code pane, otherwise `terminal.getOutput`. Answer a routine question within scope once with `terminal.sendCommandOwned`, asking for `notify` again; a question about changing the test versus the behaviour goes to the user.
2. Treat "fixed, tests pass" as a claim. Read the diff in the agent's worktree path and re-run the narrow and broader commands there yourself (step 7).
3. A diff that deletes, skips or weakens tests or cuts coverage without the user's say-so is not a fix. Report it with the files involved and do not accept it.

Stop and recovery: one corrective round per failure unless the user asks for more; a second failed round goes back to the user with the evidence. An ambiguous launch is reconciled with `terminal.list`, never relaunched. Do not re-run a CI job, push, or quarantine a test on your own initiative.

Report back: what was failing and the evidence (command, error, where the log came from), the root cause, what changed, the commands you verified with and their results against which revision, what CI shows and for which commit (or that it has not run on the fix), anything still red, and any agent's terminal id.
