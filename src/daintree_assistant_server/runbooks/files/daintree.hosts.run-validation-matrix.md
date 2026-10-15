---
id: daintree.hosts.run-validation-matrix
title: Run a cross-host validation matrix for one commit
summary: 'From this Shell''s hub, validate one pinned revision across the Remote Hosts: the same checks under each environment the person asks for (operating system, architecture, runtime versions) on a host able to meet it, and bring back one matrix with a row per environment: passed, failed, incomplete or unavailable.'
whenToUse: 'Use when the person wants ONE EXACT COMMIT, tag or release candidate TESTED UNDER SEVERAL ENVIRONMENTS on the Remote Hosts this Shell reaches, with nothing fixed: "test this SHA on the Linux host and the Mac host", "run the checks on Node 20 and Node 22 across my machines and give me the matrix", "one result per platform for this tag", a cross-platform or cross-runtime validation matrix, coordinated from the hub and pinned to one revision. The deliverable is one matrix with a row per requested environment (platform, architecture, runtime versions, checks), each row on a host that reported it can meet it, tested at that SHA without edits, and shown as passed, failed with the failing tests, incomplete (wrong SHA, missing runner, skipped or empty selection) or unavailable, never one overall green unless every row passed. Do not use for the same change made on every host (daintree.hosts.swarm-fan-out), different pieces of work spread over hosts (daintree.hosts.place-work), checks on this machine alone (daintree.ci.run-local-checks), a go or no-go on shipping (daintree.release.assess-readiness), or one host testing its own row for the hub (daintree.hosts.verify-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
1. Rows: one per environment asked for (platform, architecture, runtime versions, checks); another agent CLI is not another row. Pin the remote URL, the full SHA (`git ls-remote`; a tag's commit) and a run id. Keep pins and a row queue: row id, environment, host, `taskId`, state; surveys and follow-ups apart.
2. `host.list`, then `host.listAssistants`: each host's project for the repository with an `available` lane. None: set one up only if the request covers it (daintree.hosts.start-swarm).
3. Ask every candidate lane once: `uname -sm`, which requested runtime versions it has now (install nothing), its signed-in CLIs, its `origin` URL. End the turn.
4. Once the surveys settle, place each row on a host whose answer meets it, `origin` matching, spread across hosts; none: `unavailable`, with why. Never substitute a near version or platform; Windows cannot host a lane.
5. Per host one row at a time: one `host.sendMessage` per row, then end the turn.
   - Objective: follow daintree.hosts.verify-for-hub. Inputs: run and row ids, remote, full SHA, checks as exact commands (else the repository's CI checks), the environment row, the CLI if named; limits: time cap, no edits, installs, fixes or pushes, ports and databases only its worktree's.
   - Report: `completed` with HEAD before and after, tree clean, per command platform, architecture, runtime, test counts or exit code, failing tests by name, log path; what could not run and why.

On notice or resume: survey answers go to step 4. A row passes only when HEAD is the SHA, the environment matches the row, the tree stayed clean and every check ran, tests with counts. Wrong SHA or environment, missing runner, skipped or empty selection, timeout, or `failed`, `canceled`, `rejected`: incomplete. Missing evidence: one narrow question in its `contextId`, never a re-run. A failure is that environment's finding: never re-run, fix or change tests. A settled row frees its host: reconcile, send its next.

Stop and recovery: one send per row; a re-run or another SHA is a new request. A row still `submitted` or `working` stays open. Queue lost: report it, send no more.

Report back: one matrix, every requested row: environment, host, SHA tested, per check command and result, state (passed, failed, incomplete, unavailable, open), what the lane verified vs what you checked. It passes only if every row passed; else partial. No fix, publish or readiness verdict.
