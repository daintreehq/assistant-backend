---
id: daintree.orchestration.execute-durable-workflow
title: Execute a durable workflow graph
summary: Plan and advance dependency-aware work across turns with a caller-owned dependency plan, exact resource links, evidence and resume-time reconciliation.
whenToUse: Use for a substantial multi-step goal with dependent phases that must survive turns, restarts or asynchronous waits — migrations, delegate-then-verify work, several ordered changes, or "track this plan and resume it later". Use when the user explicitly asks for a durable plan, dependencies, progress tracking, reconciliation or a workflow graph. Do not use for a one-step action, independent parallel fan-out (use daintree.orchestration.fan-out-work), a status-only "where were we" sweep (use daintree.orchestration.status-and-resume), simple agent supervision (use daintree.orchestration.supervise-fleet), or a single future notification (use daintree.orchestration.watch-outcome).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
optionalTools:
  - terminal.getOutput
  - terminal.readLastMessageOwned
---
Use when: a goal has dependent phases that must survive turns, compaction or restarts: a migration, delegate-then-verify work, several ordered changes, "track this plan and resume it later".

Availability: Hybrid. The Daintree app has no workflow graph or plan service. The plan is a file you own, and every node runs through the normal tools of its own runbook: app actions, your shell, `git`, `gh`. A saved plan executes nothing and wakes nobody. The next activation comes from a Daintree notice on a delegated node, a host mechanism you have verified will resume you, or the user.

Procedure:
1. Reuse before creating. Look in `.daintree/runbook-runs/` in the coordinator checkout for an open plan with the same goal, and continue it (On notice or resume) rather than planning a duplicate.
2. Plan once, when the goal, target and scope are concrete. Extend the foundation's checkpoint with a node list, about twenty nodes at most; split larger work into phases. Per node: id; what it does and under which runbook; `dependsOn`; the authority it uses (which writes it may make); the evidence that proves it; status (`pending`, `dispatching`, `running`, `done`, `failed`, `blocked`, `cancelled`, `uncertain`); linked resources (worktree id and path, terminal id, `requestedId`, branch, commit, PR, issue); and the evidence recorded. Show a plan you derived to the user before its first mutation.
3. A node is ready only when every dependency is `done` with evidence. A dependency that is `running`, `uncertain` or merely idle does not count. Ready nodes run together only when they share no files or artifacts (the disjointness gate of daintree.orchestration.fan-out-work).
4. Execute one ready node through its runbook. Before a mutation, mark it `dispatching` with its intent and any id you choose yourself (a launch's `requestedId`, a branch name) and save. After the call, save every returned id at once, verify, record the evidence, then mark it `done`. Resolve arguments from live state at dispatch; never replay them from an old plan.
5. A node that waits on an agent is launched or prompted with `notify` per its runbook and marked `running`. Then end your turn.
6. Replan only when the goal or scope changes: mark the old plan superseded and keep its completed evidence. Never replan to hide a failed node. A failed node is retried within its limit and blocks only its dependents.
7. Cancel honestly. Marking the plan or a node `cancelled` stops progression and leaves a tombstone. It stops nothing the plan started: agents keep running, and worktrees, branches and PRs stay. Stopping those is a separate request (daintree.orchestration.workspace-cleanup).

On notice or resume:
1. Follow the foundation's resume procedure, then map the terminal to its node. A terminal no node links is not this plan's.
2. A node found `dispatching` is uncertain: the call may have happened after the last save. Look for its effect before anything else: the panel by its `requestedId`, the branch or commit in git, the PR with `gh pr list --head`, the file on disk. Adopt what you find; redo only what is proven absent.

```json tool=terminal.list
{"terminalId":"<requestedId recorded for the node>"}
```

3. Verify the node's evidence and record it, then recompute the ready nodes and dispatch within the original authority.
4. Resumed with no wake mechanism (an API-key client, a relaunched pane, or the user simply returning): nothing advanced while you were away. Reconcile from live state, then continue, or tell the user where the plan is saved and that the next step needs their go-ahead.

Stop and recovery: each node has a retry limit (default one). A node that exhausts it is `failed` and its dependents `blocked`. Never re-execute a `done` node, and never replay a write whose result is uncertain; read its system of record first. A corrupt plan is a recovery problem, not permission to start over.

Report back: the plan's path, done out of total, running and blocked nodes, live resources with exact ids, the evidence behind the current state, and what will bring the next step (a notice on a named terminal, or the user).
