---
id: daintree.orchestration.contract-first-split
title: Build both sides of a feature in parallel against an agreed contract
summary: Agree and commit the interface first (types or schema, example payloads, error cases), branch a worktree per side from that commit, have one agent build each side against the frozen contract at the same time, then prove both together.
whenToUse: 'Use when one feature crosses a boundary — API and client, backend and frontend, library and its consumer, producer and consumer of a message — and separate agents are to build the two sides AT THE SAME TIME against an interface agreed first: "one agent on the API, one on the UI, in parallel", "define the schema first, then build both halves", "backend and frontend agents simultaneously without drifting on field names". The deliverable is a committed contract, a conforming branch per side, and a check of both sides together. Do not use for units that share nothing (use daintree.orchestration.fan-out-work), for steps that must run one after another (use daintree.orchestration.execute-durable-workflow), for one implementer following an approved plan (use daintree.orchestration.plan-then-implement), or for merging branches that are already finished (use daintree.git.integrate-agent-branches).'
risk: git
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.getStatus
  - terminal.interruptOwned
  - terminal.sendCommandMany
  - terminal.sendCommandOwned
---
On a host: shell steps via its command runner, or delegate.

Procedure:
1. Split. Name the two sides, the agent for each (the user's, else configured ones), and the contract: types, schema or IDL at agreed paths, with example payloads and error cases. The sides must touch disjoint files apart from the contract; if they share other files this is not a parallel split: say so and serialize.
2. Contract branch. `worktree.createWithRecipe` with a new branch `contract/<feature>` from the user's base, `collisionPolicy:"error"` (a lost reply is found in `worktree.list`). Every worktree here gets `worktree.waitUntilReady` with its own `worktreeId`, repeated while `pending` or `running`; `failed`, `timed-out`, `needs-approval` or `unknown` blocks it: report `stage` and `error`. Launch a contract author there (the user's pick, else one side's agent) with `waitForReply:true, handback:true`: "Write only <contract paths>: <contract>, with example payloads and error cases; no implementation. Commit it locally as 'contract: <feature>'; push nothing. Reply with the files and the SHA." Check the commit touches only the contract paths. Show the contract to the user and build on it only after their yes, unless they left the interface to you; changes go back to the author. Record the SHA; keep the author's panel for amendments.
3. Side worktrees, both branched from the contract branch (a full SHA also works as `baseBranch`):

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<feature>-api","baseBranch":"contract/<feature>","collisionPolicy":"error"}}
```

   When both are ready, launch one agent in each in the same turn, each with its own `worktreeId` and `notify:true, handback:true`: "Implement the <side> of <feature> against the contract at <paths>, commit <sha>. The contract is read-only: do not edit <paths>; if it is wrong or missing something, stop and report exactly what must change. Build test fixtures from its example payloads, never from shapes of your own. Change only <side> files. When your tests pass, commit everything locally and leave the tree clean; push nothing. Reply with changed files and test results." End your turn.

On notice or resume:
1. A contract-change request (at most two amendments unless the user sets another bound). If the change alters what the other side is building and that side is working, stop its turn with `terminal.interruptOwned` (typed text would wait behind the turn), confirm from `terminal.getStatus` output that it stopped, and tell it to wait. Get the user's yes, or decide if they left the interface to you; the contract author amends and commits on the contract branch; check that commit touches only contract paths. Then one `terminal.sendCommandMany` with `sends:[{terminalId,command},…]` and `notify:true, handback:true`, each: "The contract changed at <new sha>: <what changed>. Commit your work in progress locally, run `git merge <new sha>`, adapt, and continue." Read each result separately and record the contract SHA each side was sent; a finish against an older contract is not done.
2. A side that edited the contract instead of stopping: reject the edit and send it back once ("restore <paths> from <contract sha> and report what must change").
3. A side finished: its report is a claim. In its worktree `git status --porcelain` must be empty (dirty: not finished); `git diff --name-only <contract sha> HEAD` must list only that side's files and no contract path (a file outside its lane is rejected even if it would merge cleanly); its tests must pass when you run them. Record its HEAD. Wait for both.
4. Both verified: a new worktree on branch `integrate/<feature>` from `contract/<feature>`, and one agent there with `notify:true, handback:true`: "Merge <api HEAD sha>, then <other HEAD sha>, one at a time with `git merge --no-edit`. A conflict outside <contract paths> means a side left its lane: `git merge --abort` and report it; do not resolve it. Then run both suites and a test across the boundary (the real client against the real endpoint where the project can run it). Push nothing. Reply with `git log --oneline -3`, the commands and results." On its notice check both SHAs are in `git log` and run the boundary test yourself. Merging into the user's branch is not part of this.

Stop and recovery: one launch per role and one corrective send per side. Each contract amendment is a new commit, never a rewrite. Never replay the contract commit or a merge: read `git log` first.

Report back: the contract paths and each contract SHA; per side its worktree, branch, terminal, verified HEAD, contract version and the results you checked; change requests and how they were settled; the integration worktree, branch and HEAD, and the boundary test command and result; the worktrees left in place.
