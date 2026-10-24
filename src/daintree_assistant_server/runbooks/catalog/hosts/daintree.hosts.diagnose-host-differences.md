---
id: daintree.hosts.diagnose-host-differences
title: Find why one commit behaves differently on two hosts
summary: 'Find why the same code passes on one Remote Host and fails on another: check each host''s checkout, reproduce the failure there without changes and take a non-secret environment fingerprint, then rank the differences by evidence, fixing nothing.'
whenToUse: 'Use when the SAME CODE BEHAVES DIFFERENTLY ON TWO OR MORE Remote Hosts this Shell reaches and the person wants to know WHY before anything changes: tests green on one machine and red on another, a build that breaks on only one host, a command that hangs or times out on just one of them, "same branch, works on the Linux box, fails on the Mac host", the host assistants asked to look into why. The deliverable is a diagnosis: each host''s checkout compared with the commit (a stale or dirty checkout is a revision mismatch, found first), the failure reproduced there without edits, a comparable non-secret environment fingerprint from each host, and the differences ranked by the evidence that ties each to the failure, a correction proposed but not applied. Do not use before any difference has been seen (daintree.hosts.run-validation-matrix), a failing check to fix on this machine (daintree.ci.diagnose-and-fix), one question to one host (daintree.hosts.ask-host-assistant), a host that cannot be reached or controlled (daintree.hosts.survey-hosts), or this machine slowed by load (daintree.runtime.relieve-resource-contention).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Fix nothing: no edits, cache deletion, settings changes, pushes, installs outside the worktree, or unnamed shared services. Every host step is a `host.call` to that host and project; the remote and the forge are read from your shell.

1. Pin, from your shell: the remote URL, the full SHA (`git ls-remote`), the exact command, input data digests, a time cap, what each host showed, and `git rev-parse <SHA>:<lockfile>` from your clone.
2. `host.list`: on each named host the project holding that repository. A host not connected or controllable: report it; no substitute host.
3. Where it was seen, per host, read only: `worktree.list` for the worktree the person used; in it `git remote get-url origin`, HEAD, branch, `git status --porcelain`, `git hash-object <lockfile>`, the build output's recorded SHA (else unknown).
4. The run at the SHA, the same on every host: that worktree if clean at the SHA, else a fresh one (`worktree.createWithRecipe` `newBranch` `diag/<short sha>` from `origin/<branch>`, `fromRemote:true`, then `worktree.waitUntilReady`; HEAD not the SHA: say so, run nothing). Record exit code, counts, failing tests, first error, start time, duration; at the cap it is incomplete: a hang, with its last output.
5. Fingerprint, the same fields from every host, unknown as unknown: OS and release, architecture; runtime, package manager, tool and plugin versions the command uses; installed versions of the dependencies the failure involves; locale, timezone; services reached (host, port, database). No secrets: no env dumps; strip credentials from URLs and errors.

Steps 3 to 5: `project.runCommand` with `cwd` the worktree path, `timeoutMs` the cap (at most 4 at once per host):

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"<exact command>","cwd":"<worktree path>","timeoutMs":900000}}
```

6. Compare, in order:
   - Off the SHA, another `origin`, changed source, another lockfile hash or a stale build: a revision mismatch, reported first. It explains the original result only if the runs at the SHA agree.
   - Runs at the SHA; of incomplete ones, only hangs. Still different: every fingerprint difference, ranked by the evidence tying it to the failure (the error text, a confirmation run); untested ones stay hypotheses.
7. Confirmation, within the person's authority: on the failing host, the same command in the same worktree with one variable changed (`TZ=UTC`, a runtime it already has), nothing persisted.

Stop and recovery: one run per host per round; another SHA or an unasked re-run is a new request. Worktrees made here stay, named in the report.

Report back: SHA, command; per host: checkout, run, fingerprint; differences ranked with their evidence; a correction proposed, unapplied; what was left on each host.
