---
id: daintree.ci.diagnose-and-fix
title: Diagnose and fix a failing check
summary: Take a red test, build or PR check from symptom to verified green — evidence first, fix second, independent verification last.
whenToUse: Use when something is FAILING and the user wants it working — "the tests are failing", "why is CI red", "the build broke", "this check won't pass", "fix the failing test", "is this flaky?" — including the fix-and-verify loop that follows. Do not use to judge the quality of a change that already passes (use daintree.review.review-change), to confirm an agent did what it claimed (use daintree.orchestration.verify-agent-work), to read a PR's review state or comment on its issue without a failure to fix (use daintree.workflow.follow-up-on-issue), to ready a branch for review (use daintree.workflow.prep-branch-for-review), or when the "failure" is Daintree itself misbehaving rather than the project (use daintree.diagnostics).
opener: "Failure diagnosis — the exact error matters more than the visible symptom"
risk: project
requiredTools:
  - context.snapshot
  - worktree.getCurrent
  - git.getProjectPulse
  - forge.getPR
  - forge.getChecks
  - daintree.call
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.await.async
  - terminal.extract
  - terminal.summarize
  - artifact.read
---
Use when: a test, build or PR check is failing and the user wants it fixed.

The discipline is evidence before hypothesis. A failing check has an exact command and an exact error somewhere; a fix written from the check's NAME is a guess, and a guess that makes the symptom disappear without addressing the cause is worse than the original failure.

Never call this done because an agent said it fixed it. "Tests pass now" from the agent that wrote the fix is the single least reliable sentence in this workflow.

Procedure:

1. READ THE STATE. `git.getProjectPulse` for branch and uncommitted changes; `worktree.getCurrent` for the `worktreeId`. For a PR: `forge.getPR({"prNumber":<int>})` gives `reviewDecision` but NOT CI. CI comes from direct Assistant `forge.getChecks({"prNumber":<int>})` — an ordinary typed roll-up read like the rest of this step, not a confirmation-gated escape hatch. Gate on `conclusive`, NOT on `state`: `conclusive:false` means the state could not be determined — the forge returned nothing, or reported ZERO required checks, which is also what it reports when it could not read the required list. That is never evidence a merge is safe, *even when `state` is `"success"`*. Counts cover REQUIRED checks only. Readings are provider-cached (~1m), so an immediate re-read may hand back the same value and one `success` is not a settled verdict. When you need the failing check NAMES, use the different raw Daintree action with the same name: `daintree.call({"name":"forge.getChecks","arguments":{"worktreeId":"<id>","prNumber":<int>}})` returns `{checks:[{name,status,conclusion?,required?,detailsUrl?}]|null}`. It still carries no job-log body.

2. GET THE ACTUAL FAILURE. A check name is not evidence. You need the failing command and its output. If a terminal already ran it, `terminal.extract` the exact error and the command that produced it. The raw per-check action can name the failed check and give `detailsUrl`, but it does not return the log body. If nothing has run it here, have an agent run it or fetch the provider log with provider tooling (step 3) — if the failure only exists in an unread log, say so plainly rather than inventing its content.

3. DIAGNOSE when the cause is not obvious from the error. Spawn an explore agent — instructed not to edit, so it should not "fix" something while looking (a prompt constraint, not a sandbox: if files change, that is a failure to report): `agentTask.spawnForEdits({"mode":"explore","worktreeId":"<id>","title":"diagnose: <check>","taskPrompt":"Run <exact command>. Report the exact failure, the root cause, and the minimal change that would fix it. Cite file:line. Change nothing."})`.
   Pick the agent deliberately: `agentId` is OPTIONAL and defaults to one particular agent, so omitting it silently chooses for the user. Resolve the best fit from the available-agent registry in your startup context and pass it — a name the user SPOKE is only a hint, since transcription drops letters; an unknown id is rejected with the real roster and a suggestion, so re-spawn with the right id rather than the typo.
   Skip this when the error already names the cause — a missing import does not need an investigation.

4. FIX with an edit agent, bound to the check. `agentTask.spawnForEdits({"mode":"edit","worktreeId":"<id>","title":"fix: <check>","taskPrompt":"<the diagnosis and the exact failing command>","acceptanceCriteria":"<command> passes, and the broader suite is not broken by the change"})`.
   Tie the criteria to the COMMAND, not the symptom. "Make the test pass" invites deleting the assertion; "the command passes and the behaviour it asserts is still enforced" does not.
   Require the agent to run BOTH the narrow failing command and the relevant broader suite. A fix that greens one test and reds four is not a fix, and you will not know unless it ran them.

5. SUPERVISE with ONE mode. Every `terminal.awaitAll` in a turn draws on one ENFORCED cumulative 120-second budget, so wait in-turn only for a fix expected to settle in a minute or two; a full-suite run goes to `terminal.await.async` and you end the turn. On `budgetExhausted`, do NOT re-await. A state read is never proof of finishing: after an await, read each settled agent's last lines before believing it.

6. VERIFY INDEPENDENTLY, and verify the RIGHT THING. Not optional, and not delegable to the author — an agent asked to confirm its own work confirms it. Spawn a FRESH agent to run the exact command in the same worktree and report what it sees.
   Provider CI is NOT a substitute here unless the fix was actually pushed AND you can tie the check result to that exact revision. A green check on the previously-pushed commit says nothing about an uncommitted local fix, and reporting it as success is the most damaging outcome in this runbook: a confident green for code nobody ran.

7. FLAKY IS A DIFFERENT DIAGNOSIS. If the same command passes and fails without the code changing, do not "fix" it — say it is flaky, give the evidence (which runs, which outcomes), and ask whether they want it stabilised or quarantined. Re-running until green and declaring victory hides a real defect.

Notes:
- Never report green from a single cached `success`. Say what you checked and when.
- If the fix needs a decision — change the test or change the behaviour — stop and ask. Picking one silently commits the user to a position they did not take.
- Say what is still red. A partial fix reported as complete costs more than no fix, because the user stops looking.
- Two failing checks with one cause is one fix; two failing checks with two causes is two rounds. Do not bundle unrelated failures into one agent brief.

Report back: what was failing and the exact evidence, the root cause, what changed, the command you verified with and its result, anything still failing, and the terminal ids you spawned.
