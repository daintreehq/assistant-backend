---
id: daintree.orchestration.test-first-split
title: One agent writes the tests, another makes them pass
summary: Have one agent write failing tests for the acceptance criteria, prove they fail for the right reason and freeze them, then have a different agent implement until they pass without touching them, checked by diff rather than trust.
whenToUse: 'Use when tests are to be written FIRST by one agent and the code by a different agent that must make them pass without touching them — "Codex writes the failing tests, Claude makes them green", "TDD this across two agents", "one agent writes the specs, another implements, no editing the tests", so the implementer cannot grade its own work. The deliverable is a frozen set of new tests proven red for the right reason, an implementation that passes them, and proof the tests and test config are unchanged since the freeze. Do not use for one agent that writes both the code and its tests (use daintree.edits.spawn-visible-agent), for tests that are already failing and should pass (use daintree.ci.diagnose-and-fix), for a reported bug to be reproduced but not fixed (use daintree.debug.reproduce-bug), for a plan approved before implementation (use daintree.orchestration.plan-then-implement), or for a reviewer sending back findings until it approves (use daintree.orchestration.review-until-approved).'
risk: project
requiredTools:
  - agent.launch
  - terminal.getStatus
optionalTools:
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.sendCommandOwned
---
On a host: shell steps run there, per the hosts foundation.

Procedure:
1. Set up. Criteria as checkable statements; new test files for the author (a missing import then breaks only them); the frozen set: those paths plus the test config (`conftest.py`, `pytest.ini`, jest/vitest config, snapshot directories, coverage thresholds). The two roles are different agents: the ones the user named, otherwise two different kinds. One worktree for both: the one named, or a new one on a new branch when asked or when the named checkout may not take commits (`worktree.createWithRecipe`, then `worktree.waitUntilReady` with its `worktreeId`, repeated while `pending` or `running`; otherwise report it and stop). Baseline there: `git rev-parse HEAD` and `git status --porcelain`.
2. Test author, one launch:

```json tool=agent.launch
{"agentId":"<test author>","name":"Tests: <feature>","worktreeId":"<worktree id>","prompt":"Write tests only, under <test paths>, for: <criteria>. Change no production code (not even a stub) or test config. Run them: every criterion needs a test that fails because the behaviour is missing; mark any test that already passes as a regression test. Then stage only the files you wrote, by exact path, check `git diff --cached --name-only` lists nothing else, and commit them as 'test: <feature>'; push nothing. Reply with the test command, the failing output, the files and the commit SHA.","notify":true,"handback":true}
```

   When commits on this branch are outside the request, drop the commit sentence. For a small feature pass `waitForReply:true, waitSeconds:900` instead of `notify` and stay in the turn.
3. Prove red yourself; the author's output is a claim. Confirm it stopped (`terminal.getStatus`), then run the new tests. The right red is an assertion, or a name the criteria create not existing yet. A syntax error, a wrong import path, a broken fixture, one collection error hiding the rest, or a criterion with no failing test is the wrong red. The existing suite must still pass. Scope: `git log --oneline <baseline>..HEAD` shows at most the one test commit, and `git diff --name-only <baseline>` plus new untracked files touch only the test paths (beyond the baseline's own changes). Anything wrong: one corrective `terminal.sendCommandOwned` to the author naming every problem (wait as in step 2), then the user.
4. Freeze. The freeze point is the author's commit SHA. With no commit: record `git hash-object` of every frozen file and the `git status --porcelain -- <frozen set>` listing; a hash detects a change but cannot restore the bytes. Record the test command and how many tests it collected; tell the user.
5. Implementer, only after the author has stopped, in the same worktree, one `agent.launch` with `notify:true, handback:true`: "Make the tests in <test files> pass (frozen at <sha or hashes>). Do not edit, skip, xfail, rename, delete or reconfigure any of: <frozen set>. Do not special-case the test inputs. If a test looks wrong, stop and say which and why instead of changing it. Run the full suite; reply with changed files and results." Leave the author's panel idle for disputes; it never works while the implementer does.

On notice or resume:
1. The author's reply goes to step 3; only the implementer's continues here.
2. Tamper check first: `git diff <freeze sha> -- <frozen set>` and `git status --porcelain -- <frozen set>` both empty (hash mode: every hash matches and no path beyond the recorded listing), and no new skip, xfail or `.only` markers anywhere in `git diff <freeze sha>`. Then run the frozen test command yourself: the same number of tests collected, none skipped, all passing; then the whole suite. Read the implementation for branches keyed to the test inputs.
3. Tampering or special-casing: one corrective send to the implementer ("restore <files> to <sha> and make them pass unchanged"); a second time, or tampering in hash mode, reject the implementation and report it.
4. "This test is wrong" is never settled by the implementer. Put the objection to the user or, when they delegated it, to the test author as a read-only question with `waitForReply:true, handback:true`. A test the author agrees to change (at most one amendment) is changed and committed by the author as a new freeze point. Never reset the tree to re-prove its red: read the amended test yourself and report it as not re-proven. Then the implementer continues against the new freeze.

Stop and recovery: one launch per role, one corrective round per role, one test amendment.

Report back: the outcome and the step it stopped at; worktree, baseline and freeze point (SHA or hashes); the red output you saw; the implementer's diff summary; your run of the frozen tests (count collected, results) and the suite; the tamper check; disputes and how they were settled; both terminal ids.
