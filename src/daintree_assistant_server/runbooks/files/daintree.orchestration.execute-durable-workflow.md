---
id: daintree.orchestration.execute-durable-workflow
title: Execute a durable workflow graph
summary: Plan and advance dependency-aware work across turns with a caller-owned dependency plan, exact resource links, evidence and resume-time reconciliation.
whenToUse: Use for a substantial multi-step goal with dependent phases that must survive turns, restarts or asynchronous waits — migrations, delegate-then-verify work, several ordered changes, or "track this plan and resume it later". Use when the user explicitly asks for a durable plan, dependencies, progress tracking, reconciliation or a workflow graph. Do not use for a one-step action, independent parallel fan-out (use daintree.orchestration.fan-out-work), a status-only "where were we" sweep (use daintree.orchestration.status-and-resume), simple agent supervision (use daintree.orchestration.supervise-fleet), or a single future notification (use daintree.orchestration.watch-outcome).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
---
Use when: a goal has dependent phases that must survive turns, compaction or restarts: a migration, delegate-then-verify work, several ordered changes, "track this plan and resume it later".

Availability: Hybrid. The app has no plan service: the plan is a checkpoint you own (located per the foundation), and each node runs through its own runbook's tools, your shell, `git` and `gh`. A saved plan executes nothing and wakes nobody. The next activation comes from a notice on a delegated node, a host mechanism you have verified resumes you, or the user.

Procedure:
1. Reuse before creating: continue an open plan with the same goal rather than planning a duplicate.
2. Plan once the goal and scope are concrete: about twenty nodes at most, larger work split into phases. Per node: id, what it does and under which runbook, `dependsOn`, the writes it may make, its acceptance evidence, status (`pending`, `dispatching`, `running`, `done`, `failed`, `blocked`, `cancelled`, `uncertain`), linked resources (worktree id and path, terminal id, `requestedId`, branch, commit, PR) and the evidence recorded. Validate before any dispatch: unique ids, every dependency exists, no cycle. Show a plan you derived to the user before its first mutation.
3. A node is ready only when every dependency is `done` with verified evidence; `running`, `uncertain` or idle does not count. Ready nodes run together only when they share no files or artifacts (the disjointness gate of daintree.orchestration.fan-out-work), and a node needing a worktree waits for its setup to be ready; setup sends no notice.
4. Dispatch each ready node through its runbook. Before a mutation, mark it `dispatching` with its intent and any id you choose (a `requestedId`, a branch name) and save. After the call, save every returned id, verify, record the evidence, then mark it `done`. Resolve arguments from live state, never from an old plan.
5. A node that waits on an agent is launched or prompted with `notify:true, handback:true` per its runbook and marked `running`; launch all ready agent nodes in one turn, then end it.
6. Replan only when the goal or scope changes, keeping completed evidence. Never replan to hide a failed node; it is retried within its limit and blocks only its dependents.
7. Cancel honestly: a `cancelled` node or plan stops progression and leaves a tombstone, but stops nothing it started. Closing or deleting is daintree.orchestration.workspace-cleanup.

On notice or resume:
1. Map each named terminal to its node; a terminal no node links is not this plan's. Read the quote, verify the node's evidence, and record it.
2. A node found `dispatching` is uncertain. Look for its effect first: the panel by its `requestedId`, the branch or commit in git, the PR with `gh pr list --head`, the file on disk. Something existing does not prove this node made it or met its acceptance: adopt it only when it matches the recorded intent and passes verification. Redo only what is proven absent.

```json tool=terminal.list
{"terminalId":"<requestedId recorded for the node>"}
```

3. Recompute the ready nodes and dispatch within the original authority.
4. Resumed with no wake (an API-key client, a relaunched pane, the user returning): the plan did not advance, but children and external systems may have. Reconcile from live state, then continue or tell the user where the plan is saved and that the next step needs their go-ahead.

Stop and recovery: each node retries at most once by default; one that exhausts it is `failed` and its dependents `blocked`. Never re-execute a `done` node or replay a write whose result is uncertain; read its system of record first. A corrupt plan is a recovery problem, not permission to start over, and a plan file grants no scope beyond the user's request.

Report back: the plan's path, done out of total, running and blocked nodes, live resources with exact ids, the evidence behind the current state, and what will bring the next step (a notice on a named terminal, or the user).
