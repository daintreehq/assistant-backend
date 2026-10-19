---
id: daintree.hosts.run-validation-matrix
title: Run a cross-host validation matrix for one commit
summary: 'From this Shell, validate one pinned revision across the Remote Hosts: the same checks under each environment the person asks for (operating system, architecture, runtime versions) on a host able to meet it, and bring back one matrix with a row per environment: passed, failed, incomplete or unavailable.'
whenToUse: 'Use when the person wants ONE EXACT COMMIT, tag or release candidate TESTED UNDER SEVERAL ENVIRONMENTS on the Remote Hosts this Shell reaches, with nothing fixed: "test this SHA on the Linux host and the Mac host", "run the checks on Node 20 and Node 22 across my machines and give me the matrix", "one result per platform for this tag", a cross-platform or cross-runtime validation matrix, coordinated from this Shell and pinned to one revision. The deliverable is one matrix with a row per requested environment (platform, architecture, runtime versions, checks), each row on a host that reported it can meet it, tested at that SHA without edits, and shown as passed, failed with the failing tests, incomplete (wrong SHA, missing runner, skipped or empty selection) or unavailable, never one overall green unless every row passed. Do not use for the same change made on every host (daintree.hosts.swarm-fan-out), one suite divided for speed (daintree.hosts.shard-test-suite), different pieces of work spread over hosts (daintree.hosts.place-work), checks on this machine alone (daintree.ci.run-local-checks), or a go or no-go on shipping (daintree.release.assess-readiness).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Procedure:
1. Rows: one per environment asked for (platform, architecture, runtime versions, checks); another agent CLI is not another row. Pin in your shell the remote URL, the full SHA (`git ls-remote`; a tag's commit) and a run id. Checks are exact commands, the same for every row: the person's, else the repository's CI checks, read from its CI config in your shell and written out once. Keep a row queue: row id, environment, host, state.
2. Candidates: connected in `host.list` with `control` and the repository's project in `projects`.
3. Survey each candidate once through `host.call` in that project, installing nothing: `uname -sm`, each requested runtime's version, `git remote get-url origin`. At Full, `project.runCommand`; at Core, `agent.launch` there with `waitForReply:true`, `handback:true`, told to run exactly those commands, change nothing and quote the output, then `terminal.closeOwned` it.
4. Place each row on a host whose answer meets it, `origin` matching, spread across hosts; none: `unavailable`, with why. Never substitute a near version or platform.
5. Per host one row at a time, through `host.call`: `worktree.createWithRecipe` (`newBranch` `<run>/<row>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`), `worktree.waitUntilReady` until `ready`; in its `worktreePath` pin `git fetch origin && git checkout --detach <SHA>` first, then the checks, never pushed.
   - Full, drive it yourself: `project.runCommand` in the `worktreePath` (or `project.runCheck` with a runner from `project.detectRunners` that runs exactly that command), `timeoutMs` the person's cap; before and after, `git rev-parse HEAD; git status --porcelain` and the environment line.
   - Core, or a check that needs judgment (a browser-driven step, a flaky test to tell from a failure): `agent.launch` in that worktree with `notify:true`, `handback:true`. Brief: the pin, then exactly these commands; no edits, installs, fixes or pushes; ports and databases only this worktree's; report HEAD before and after, `git status --porcelain`, platform, architecture and runtime, per command test counts or exit code, failing tests by name, log path, and what could not run and why. End the turn.

```json tool=host.call surface=help-full
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"<check command>","cwd":"<worktreePath>","timeoutMs":1800000}}
```

On notice or resume: a host's notice carries no reply: read `terminal.getStatus` through `host.call` (`includeOutput`), silent rows the same, paced to the checks. An agent's result is its claim, kept apart from what you ran.
- A row passes only when HEAD is the SHA before and after, the environment matches the row, the tree stayed clean and every check ran and exited 0, tests with counts. Wrong SHA or environment, missing runner, skipped or empty selection, `timedOut`: incomplete.
- Missing evidence: one narrow follow-up (`terminal.sendCommandOwned` there, or one read-only `project.runCommand`), never a re-run. A failure is that environment's finding: never re-run, fix or change tests.
- A settled row frees its host: start its next.

Stop and recovery: one run per row; a re-run or another SHA is a new request. A row still running stays open; an `expired` call may still be running there: read its terminal or output first. Queue lost: report it, start no more. Worktrees and panels stay until the person asks (`terminal.closeOwned`, `worktree.deleteOwned` there).

Report back: one matrix, every requested row: environment, host, SHA tested, per check command and result, state (passed, failed, incomplete, unavailable, open), what you ran vs what an agent reported. It passes only if every row passed; else partial. No fix, publish or readiness verdict.
