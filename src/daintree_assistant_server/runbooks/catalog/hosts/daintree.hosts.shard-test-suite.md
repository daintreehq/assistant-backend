---
id: daintree.hosts.shard-test-suite
title: Split one test suite into shards across the remote hosts
summary: 'Split one test suite into disjoint shards that several Remote Hosts each run once, and combine the shard results into one report checked against the suite''s own test list, with missing, empty, duplicate and retried shards accounted for.'
whenToUse: 'Use when the person wants ONE test suite run faster by SPLITTING it between their Remote Hosts, each host taking a disjoint part of its tests: "our suite takes forty minutes, spread it over my other machines", "run the tests in four shards on my machines and tell me nothing was missed", one manifest of test ids divided between hosts. The deliverable is one merged test report: the suite''s test list taken once, each shard (an index of a total, or a list of tests) run once on one host, and the union checked against that list, with a missing, empty, duplicate or retried shard, a timeout and skipped tests named, and a pass claimed only when every test ran exactly once. Do not use for the same work or change on every host (daintree.hosts.swarm-fan-out), different independent pieces placed one per host (daintree.hosts.place-work), the checks run on this machine (daintree.ci.run-local-checks), or the same checks repeated under each environment for a matrix (daintree.hosts.run-validation-matrix).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Procedure:
1. Pin in your shell the remote, the full SHA (`git ls-remote`), a run id and the suite (the person's scope or manifest, within the repository's rules; unclear: ask). Hosts: connected in `host.list` with `control` and the repository's project; one shard each.
2. Per host through `host.call`: `worktree.createWithRecipe` (`newBranch` `<run>/<host>`, `collisionPolicy:"error"`, `focusPolicy:"preserve"`), `worktree.waitUntilReady` until `ready`. A throwaway: never pushed.
3. Discovery in each `worktreePath`, no test run: pin it (`git fetch origin && git checkout --detach <SHA>`), `git rev-parse HEAD`; the runner's version; its shard option per its own help; its test list (collect or list mode) sorted, with count and sha256, the ids if they fit, by `project.runCommand` there.
4. A differing version, count or hash is a different suite: report it, never shard. Scheme: the ids (or files) cut into n disjoint lists, each fitting one command line or a file, else the runner's own `<i>/<n>` option; neither: report that, never invent a flag.
5. Shards, one per host: one command, config, environment, timeout and workers for all, each writing a machine-readable report (JUnit or the runner's JSON) in its worktree. Keep shard, host, `projectId`, selection and `callId`: `project.runCommand` per host, issued together as parallel calls, `timeoutMs` sized to the shard, the command printing `git rev-parse HEAD` before and after the suite; then one more reading the report: executed ids (count and sha256, the ids if they fit), pass, fail and skip counts, failing and skipped tests by name.

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"<suite command> <shard selection> <report option>","cwd":"<worktreePath>","timeoutMs":1800000}}
```

Accounting: one attempt counts per shard, the retry if any; another run of the same selection is a named duplicate. Coverage: counts sum to the discovered count; a list shard ran exactly its list; option shards' ids unite to the discovered list, none twice (too many to read back: unverified, say so). A shard off the SHA before or after, missing, timed out (`timedOut`), aborted or truncated (`outputTruncated` with no report), empty, or exiting non-zero with no report; a test run twice or never: incomplete, named, never a pass.

Stop and recovery: one run per shard. Retry a shard only once its attempt has finished, on the same SHA and selection: an `expired` run may still be going there (its call's `exitCode` ends it; a report file alone does not). Side-effecting tests need isolated data and an owner the person names. A pass on retry is reported so, with the earlier failure. Worktrees stay until the person asks; then `worktree.deleteOwned` there.

Report back: run id, SHA, runner, scheme, discovered count and hash; per shard host, attempts, SHA, command, selection, counts, report path, proof; holes, duplicates, failures and skips by name. "Passed" only when every listed test ran exactly once and none failed; named skips qualify it.
