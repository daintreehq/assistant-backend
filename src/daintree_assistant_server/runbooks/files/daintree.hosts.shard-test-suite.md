---
id: daintree.hosts.shard-test-suite
title: Split one test suite into shards across the remote hosts
summary: 'Split one test suite into disjoint shards that several Remote Hosts each run once, and combine the shard results into one report checked against the suite''s own test list, with missing, empty, duplicate and retried shards accounted for.'
whenToUse: 'Use on the hub when the person wants ONE test suite run faster by SPLITTING it between their Remote Hosts, each host taking a disjoint part of its tests: "our suite takes forty minutes, spread it over my other machines", "run the tests in four shards on my machines and tell me nothing was missed", one manifest of test ids divided between hosts. The deliverable is one merged test report: the suite''s test list taken once, each shard (an index of a total, or a list of tests) run once on one host, and the union checked against that list, with a missing, empty, duplicate or retried shard, a timeout and skipped tests named, and a pass claimed only when every test ran exactly once. Do not use for the same work or change on every host (daintree.hosts.swarm-fan-out), different independent pieces placed one per host (daintree.hosts.place-work), the checks run on this machine (daintree.ci.run-local-checks), the same checks repeated under each environment for a matrix (daintree.hosts.run-validation-matrix), or on a host lane running its one shard (daintree.hosts.verify-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
1. Pin the remote, full SHA (`git ls-remote`), a run id and the suite (the person's scope or manifest, within the repository's rules; unclear: ask). Lanes (`host.listAssistants`): per host an `available` one in the repository's project, one open shard each.
2. Discovery, one `host.sendMessage` per lane, no `waitMs`: "Follow daintree.hosts.verify-for-hub"; remote, SHA, run id, the shards' environment and limits; check: list the suite's tests with its own runner, none run; report runner version, its shard option per its help, count and sha256 of the sorted ids, the ids (else files) if they fit. End the turn.
3. Every lane answered: a differing version, count or hash is a different suite: report, never shard. Scheme: the ids (or files) cut into n disjoint lists, each fitting one message, else the runner's option `<i>/<n>`; neither: report that, never invent a flag.
4. Per shard one `host.sendMessage`, no `waitMs`, as in step 2 plus shard `<i>/<n>` and its list or option; one command, config, environment, timeout and workers for all; report SHA, command, ids run (else count and sha256), pass, fail and skip counts, failing and skipped tests by name, its report file's path. Keep shard, lane, list, `taskId`s. End the turn.

On notice or resume: discovery replies go to step 3. A shard reply must show its SHA, command and selection; ids or hash missing: one narrow question in its `contextId`. One attempt counts per shard, the retry if any; another copy is a named duplicate. Coverage: counts sum to the discovered count; a list shard ran exactly its list; option shards' ids unite to the discovered list, none twice (too many to return: unverified, say so). A shard missing, unanswered, timed out or empty, a test twice or never: incomplete, named, never a pass.

Stop and recovery: one send per shard. A retry: a new message referencing the original, its text naming run, shard, attempt and original `taskId`, same SHA and selection, once that attempt completed or its lane says it stopped; side-effecting tests need isolated data and an owner the person names. A pass on retry is reported so, with earlier failures.

Report back: run id, SHA, runner, scheme, discovered count, hash; per shard host, `taskId`, attempts, SHA, command, selection, counts, report path, proof; holes, duplicates, failures and skips by name. "Passed" only when every listed test ran exactly once and none failed; named skips qualify it.
