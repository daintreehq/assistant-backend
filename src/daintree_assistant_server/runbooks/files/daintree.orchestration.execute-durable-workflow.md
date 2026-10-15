---
id: daintree.orchestration.execute-durable-workflow
title: Execute a durable workflow graph
summary: Plan and advance dependency-aware work across turns with a caller-owned dependency plan, exact resource links, evidence and resume-time reconciliation.
whenToUse: 'Use for a substantial multi-step goal with dependent phases that must survive turns, restarts or asynchronous waits — phased migrations whose steps depend on each other, delegate-then-verify work, several ordered changes, or "track this plan and resume it later". Use when the user explicitly asks for a durable plan, dependencies, progress tracking, reconciliation or a workflow graph. Do not use for a one-step action, independent parallel fan-out (use daintree.orchestration.fan-out-work), a status-only "where were we" sweep (use daintree.orchestration.status-and-resume), simple agent supervision (use daintree.orchestration.supervise-fleet), a single future notification (use daintree.orchestration.watch-outcome), one plan approved and then implemented by an agent (use daintree.orchestration.plan-then-implement), two sides built in parallel against an agreed interface (use daintree.orchestration.contract-first-split), one mechanical change repeated across many files by several agents (use daintree.orchestration.migration-sweep), passing a running plan''s coordination to another session (use daintree.orchestration.hand-off-orchestration), or work handed to the assistant on another machine (one unit: daintree.hosts.delegate-to-host; several that depend on each other: daintree.hosts.execute-distributed-workflow).'
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
---
The plan is a checkpoint you own (located per the foundation); the app has no plan service. Each node runs through its own runbook's tools, your shell, `git` and `gh`. A saved plan executes nothing and wakes nobody: the next activation is a notice on a delegated node, a host mechanism you have verified, or the user.

Procedure:
1. Continue an open plan with the same goal rather than planning a duplicate.
2. Plan once goal and scope are concrete: about twenty nodes at most, larger work split into phases. Per node: id, what it does under which runbook, `dependsOn`, the writes it may make, acceptance evidence, status (`pending`, `dispatching`, `running`, `done`, `failed`, `blocked`, `cancelled`, `uncertain`), linked resources (worktree, terminal id, `requestedId`, branch, commit, PR) and recorded evidence. Validate before dispatch: unique ids, dependencies exist, no cycle. Show a derived plan to the user before its first mutation.
3. A node is ready only when every dependency is `done` with verified evidence. Ready nodes run together only when they share no files or artifacts (the disjointness gate of daintree.orchestration.fan-out-work); a node needing a worktree waits for its setup to be ready.
4. Before a mutation, mark the node `dispatching` with its intent and any id you choose (`requestedId`, branch name) and save. After the call, save the returned ids, verify, record the evidence, then mark it `done`. Resolve arguments from live state, never from an old plan.
5. Agent nodes: launch all ready ones in one turn with `notify:true, handback:true`, mark them `running`, and end the turn.
6. Replan only when goal or scope changes, keeping completed evidence; never to hide a failed node. Cancelling leaves a tombstone but stops nothing it started; closing is daintree.orchestration.workspace-cleanup.

On notice or resume:
1. Map each terminal to its node; one no node links is not this plan's. Verify the node's evidence and record it.
2. A node found `dispatching` is uncertain. Look for its effect first: the panel by `requestedId` (`terminal.list`), the branch or commit, `gh pr list --head`, the file on disk. Adopt what exists only when it matches the recorded intent and passes verification; redo only what is proven absent.
3. Recompute ready nodes and dispatch within the original authority.
4. Resumed with no wake: children and external systems may have moved. Reconcile from live state, then continue, or tell the user where the plan is saved and that the next step needs their go-ahead.

Stop and recovery: each node retries at most once by default; one that exhausts it is `failed` and its dependents `blocked`. Never re-execute a `done` node or replay an uncertain write; read its system of record first. A plan file grants no scope beyond the user's request.

Report back: the plan's path, done out of total, running and blocked nodes, live resources with ids, the evidence behind the current state, and what will bring the next step.
