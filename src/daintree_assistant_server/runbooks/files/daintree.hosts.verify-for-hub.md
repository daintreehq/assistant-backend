---
id: daintree.hosts.verify-for-hub
title: Test a commit for the hub
summary: On a host lane, put the exact commit the hub named through the checks it asked for without changing it — an isolated worktree at that SHA, a visible verifier agent, the run checked against the checkout — and return each command's counts and what could not run.
whenToUse: 'Use when the hub asks this lane to TEST or VERIFY a commit or branch here and report the results, with no fixes: "the hub wants its commit put through the test suite on this machine", "hub message: verify this SHA with lint and the unit tests", "independently validate the delegated build on this host", one shard of a suite, or one environment row of the hub''s matrix. The deliverable is a test run bound to that exact SHA: an isolated worktree, a visible verifier agent running only the requested checks without edits, HEAD and working-tree state before and after, each command''s platform and pass, fail and skip counts, and what could not run marked incomplete. Do not use for the message or reply itself (daintree.hosts.handle-hub-message), the person''s own checks here (daintree.ci.run-local-checks), a local agent''s result (daintree.orchestration.verify-agent-work), a code review (daintree.review.review-change; one the hub asked for, daintree.hosts.review-for-hub), or the hub''s own side (daintree.hosts.delegate-to-host; a matrix across hosts, daintree.hosts.run-validation-matrix).'
risk: terminal
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
  - assistant.reply
optionalTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.listAvailable
---
The message, its fit and the one reply are daintree.hosts.handle-hub-message's; the run goes to a visible verifier.

1. From the brief keep the remote, full SHA, checks (named commands, else the repository's CI checks), any shard (id/total) or test manifest, environment row and limits. A short SHA, or a remote other than `git remote get-url origin` here: ask back. A branch alone: pin it once (`git ls-remote`) and say so.
2. Reconcile by exchange: its `verify/<exchange id>` worktree (`worktree.list`) or verifier (`terminal.list`) is continued. Else, if `git cat-file -e <SHA>^{commit}` fails here, a visible agent fetches it (`waitForReply:true`). Create the worktree at the SHA; `worktree.waitUntilReady` on its id until `ready` (again while pending or running; else blocker):

```json tool=worktree.createWithRecipe surface=lane-full
{"source":{"kind":"newBranch","branchName":"verify/<exchange id>","baseBranch":"<full SHA>","collisionPolicy":"error"},"focusPolicy":"preserve"}
```

3. Launch one verifier there, the brief's CLI if named, `notify:true, handback:true`. Brief: SHA, checks, selection, environment, limits; the repository's own setup; HEAD and `git status --porcelain` before and after; full output saved outside the checkout; never edit, commit, push, install other tools or fix; per command its command, runtime and platform, test counts or exit code, the selection actually run, log path, what could not run and why. End the turn.

On notice or resume: the report is a claim. Check yourself: HEAD is the SHA, `git diff --quiet HEAD` passes (untracked files listed as generated, apart from source), each log names the SHA and matches its counts or exit code. A missing runner, unmet environment, empty selection, timeout, or a test command exiting 0 with no runner summary is incomplete, never a pass. A branch moved (`git ls-remote`) is a finding, never a re-run. Reply `completed`: SHA, branch head now, per check command, runtime, platform, counts, failing tests by name, log path; what could not run. Sensitive or oversized detail stays here by path; every failure is still counted.

Stop and recovery: one worktree and one verifier per exchange; a re-run, another revision or a fix is a new request. A source edit voids the run: report it. Leave the worktree and panes.

Report back: the reply as sent, with the worktree and pane ids.
