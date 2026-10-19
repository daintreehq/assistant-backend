---
id: daintree.review.red-team-change
title: Red-team a change with an attacking agent
summary: Point one or more agents at a finished change to break it, accept only breaks they demonstrate with a runnable test or script, and run each one yourself on the change and on its base so regressions are told apart from old bugs.
whenToUse: 'Use when the user wants a change ATTACKED rather than reviewed — "have Codex try to break Claude''s fix", "red-team this PR", "get an agent to find inputs that crash the new parser", "adversarially test the auth change and prove anything it finds", "try to break it". The deliverable is a list of CONFIRMED breaks, each with a reproduction you ran that fails on the change, marked regression or pre-existing by a run on the base, plus the claims rejected and why; nothing is fixed. Do not use for findings from reading a diff (use daintree.review.review-change), to check an agent''s done-claim against evidence (use daintree.orchestration.verify-agent-work), to audit an existing area of code that is not a change (use daintree.review.audit-codebase), for exploratory testing of the whole running app (use daintree.orchestration.bug-bash), to fix a failing check (use daintree.ci.diagnose-and-fix), or to send confirmed breaks to an agent to fix (use daintree.agents.relay-review-feedback).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Only a break you ran counts. "Potential", "could", a stack of reasoning, or a repro that fails for environment reasons is a rejected claim.

On a host: shell steps via its command runner, or delegate; own shell: `gh -R <repo>`.

Procedure:
1. Freeze the candidate as a SHA. Local work must be committed (a worktree starts from committed code; if it is dirty, say so and ask the author or user to commit). A GitHub PR: `git fetch origin pull/<n>/head`, and `FETCH_HEAD` must equal `gh pr view <n> --repo <owner/name> --json headRefOid`; another forge: its own PR ref. Base: `git merge-base <candidate> origin/<base branch>`. Record both.
2. One worktree per attacker, never the author's, so repro files and runs never collide: `git branch redteam/<slug>-<lens> <candidate sha>` in the main checkout (this creates the branch; never check out or switch branches in the main or the author's checkout), then create its worktree with no recipe, then `worktree.waitUntilReady`.

```json tool=worktree.createWithRecipe
{"source":{"kind":"existingBranch","branchName":"redteam/coupons-input"}}
```

   `timedOut`: call again a few times, then report. `failed`, `needs-approval` (the user approves setup commands; you cannot) or `unknown`: skip that attacker and report `stage` and `error`. Keep each returned worktree id.
3. One attacker per lens the user wants (input handling, concurrency, auth and permissions, error paths, resource limits); one general attacker if none named. Use an agent kind other than the author's where one is available, and never say who wrote the change. Separate `agent.launch` per attacker (never `agent.launchMany`), each with its own `name` and its worktree's id, `notify:true, handback:true`; the prompt never tells it to check out anything. Prompt: "Try to break this change: <intent in one paragraph>, commit <sha>, files <list>. For each break write a NEW failing test or script under redteam/ and give the exact command that runs it and the output you saw. The script must not delete outside this worktree, touch the network or external services, or need credentials. Do not edit any existing file, fix anything, commit or push. 'No break found' is a valid answer. Report each break as: what breaks, command, expected, actual." End the turn.

```json tool=agent.launch
{"agentId":"codex","name":"Red team: <lens>","worktreeId":"<attacker worktree id>","prompt":"<intent, sha, files, rules above>","notify":true,"handback":true}
```

On notice or resume: a notice is one attacker's stop, not its verdict. A question within the brief gets one answer by `terminal.sendCommandOwned` with `notify:true, handback:true`; one that widens scope goes to the user. An attacker that edited existing files broke its brief (`git -C <path> status --porcelain`): report it and confirm only its new files. Then confirm its claims in its own worktree, now that it has stopped (you run its repros; you never write one yourself):
- Read each repro before its first run. One that deletes outside the worktree, writes external state or needs a network, secret or machine you lack: do not run it; rejected, with the reason. Pure reasoning: rejected.
- Run the command on the candidate: it must fail as claimed. Run it twice; a result that flips is flaky, reported as such.
- Then on the base: `git -C <path> switch --detach <base sha>` (the untracked repro files stay), run it, `git -C <path> switch redteam/<slug>-<lens>`. Same failure and symptom on base: pre-existing. Passes on base, or cannot run there because the code under attack is new: a regression. A different failure: base inconclusive. If the switch refuses, record the base as unchecked; never force it.
Then wait for the other attackers; end the turn while any is working.

Stop and recovery: one round per attacker unless the user asks for another. Never relaunch an attacker that is still working, and never re-run a repro that deletes or publishes anything. Fixing is separate: offer daintree.agents.relay-review-feedback with the confirmed repros, and leave the attacker worktrees and branches until the user decides.

Report back: candidate and base SHAs, each attacker's worktree and branch, each confirmed break (repro command, failing output on the candidate, result on the base: regression, pre-existing, inconclusive or unchecked), flaky results, rejected claims with reasons, attacker terminal ids, setup that blocked an attacker, and anything still running.
