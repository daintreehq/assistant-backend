---
id: daintree.hosts.diagnose-host-differences
title: Find why one commit behaves differently on two hosts
summary: 'Find why the same code passes on one Remote Host and fails on another: have each host''s assistant check its checkout, reproduce the failure without changes and report a non-secret environment fingerprint, then rank the differences by evidence, fixing nothing.'
whenToUse: 'Use when the SAME CODE BEHAVES DIFFERENTLY ON TWO OR MORE Remote Hosts this Shell reaches and the person wants to know WHY before anything changes: tests green on one machine and red on another, a build that breaks on only one host, a command that hangs or times out on just one of them, "same branch, works on the Linux box, fails on the Mac host", the host assistants asked to look into why. The deliverable is a diagnosis: each host''s checkout compared with the commit (a stale or dirty checkout is a revision mismatch, found first), the failure reproduced there without edits, a comparable non-secret environment fingerprint from each host, and the differences ranked by the evidence that ties each to the failure, a correction proposed but not applied. Do not use before any difference has been seen (daintree.hosts.run-validation-matrix), a failing check to fix on this machine (daintree.ci.diagnose-and-fix), one question to one host (daintree.hosts.ask-host-assistant), a host testing a commit it was sent (daintree.hosts.verify-for-hub), an assistant that cannot be messaged (daintree.hosts.diagnose-messaging), or this machine slowed by load (daintree.runtime.relieve-resource-contention).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
1. Pin the remote URL, the full SHA (`git ls-remote`), the exact command, input data digests, a time cap, and what each host showed.
2. `host.list`, `host.listAssistants`: on each named host, the project holding that repository and an `available` lane. None: daintree.hosts.diagnose-messaging; no substitute host.
3. One `host.sendMessage` per host, the same brief, no `waitMs`; end the turn. One reply, the same fields from every host, unknown as unknown:
   - Objective: explain a difference, fix nothing: no edits, cache deletion, settings changes, pushes, installs outside the worktree, or unnamed shared services.
   - Where it was seen, read only: `origin`, HEAD, branch, changed and untracked source paths, the lockfile's `git hash-object`, whether build output predates HEAD.
   - The run, per daintree.hosts.verify-for-hub (worktree at the SHA): exit code, counts, failing tests, first error, start time, duration; at the cap, a hang with its last output; no runner: incomplete.
   - Fingerprint: OS and release, architecture; runtime, package manager, tool and plugin versions the command uses; installed versions of dependencies the failure involves; locale, timezone; services reached (host, port, database). No secrets: strip credentials from URLs and errors; no env dumps.

On notice or resume: each reply is that host's claim. Check: the SHA on the remote; `git rev-parse <SHA>:<lockfile>` where your clone has it, against each host's hash. Then, in order:
- Off the SHA, another `origin`, changed source, another lockfile or a stale build: a revision mismatch, reported first. It explains the original result only if the runs at the SHA agree.
- Compare only complete runs at the SHA. Still different: list every fingerprint difference, ranked by the evidence tying it to the failure (the error text, a confirmation run); untested ones stay hypotheses.
- Missing evidence: one narrow question in that exchange's `contextId`.

Confirmation, within the person's authority: one message to the failing host changing one variable for one run (`TZ=UTC`, a runtime it already has), nothing persisted.

Stop and recovery: one send per host per round; another SHA or an unasked re-run is a new request. A host still `working` stays pending.

Report back: SHA, command; per host, attributed: checkout, run, fingerprint; what you verified; differences ranked with their evidence; a correction proposed, unapplied.
