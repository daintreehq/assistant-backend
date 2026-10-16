---
id: daintree.hosts.coordinate-contract-change
title: Agree and amend one interface contract across Remote Hosts
summary: 'From this Shell''s hub, have the interface between projects on different Remote Hosts written, agreed and frozen at one commit, brief every side with that exact version, carry each change as a new version the affected hosts pause for, acknowledge and adopt, and accept only results checked against the current version.'
whenToUse: 'Use on the hub when Remote Hosts build the sides of ONE INTERFACE in DIFFERENT REPOSITORIES (an API and its client, a schema and the services reading it, a message producer and its consumers) and the CONTRACT between them has to be agreed before they build or CHANGED while they do: "pin down the event schema before the producer on one machine and the consumer on another start", "the app side on the laptop needs another field; change the contract and move every machine onto the new version", "the hosts are building against different versions of the API spec, get them back onto one". The deliverable is the contract''s versions and each side''s adoption: every version a commit by full SHA in the one repository that holds it, agreed before anything builds on it; each side briefed with that version on its own repository''s base; a change made a new version, sent once the affected hosts acknowledged pausing, then adopted side by side; a result built on an older version rejected as stale; every side checked with the same compatibility test. Do not use for both sides built by agents on this machine (daintree.orchestration.contract-first-split), a staged plan of dependent units with no contract to agree or change (daintree.hosts.execute-distributed-workflow), one unit for one host or a single hand-over between two (daintree.hosts.delegate-to-host), stopping or changing sent work with no contract change (daintree.hosts.steer-swarm), finished branches combined into one (daintree.hosts.integrate-host-branches), or on a host''s lane building or testing its side (daintree.hosts.do-delegated-work, daintree.hosts.verify-for-hub).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
  - host.cancelTask
  - assistant.getInbox
---
1. Per side: host, project, lane (`host.listAssistants`), remote, that repository's base SHA (`git ls-remote`) and its contract copy's path; a SHA never crosses repositories. Contract: one home repository and paths (schema, examples, error cases, a compatibility test).
2. Brief the home lane as daintree.hosts.delegate-to-host does (`get_runbook` if absent): its agent writes only those paths on `contract/<run>` and pushes (no push authority: ask first). Check its scope on the remote, show the person, freeze on their yes (the interface is theirs unless left to you): v1, full SHA. Versions are `contract/<run>`'s history.
3. Brief each side likewise, `taskId` `<run>-<side>-v1`: copy the paths unchanged from v1 to its copy's path (the home side builds on v1); fixtures from its examples; only its own paths; a contract change needed: stop, reply `input-required`, never edit the copy; report the contract SHA used. End the turn.

On notice or resume:
- A change, a side's or the person's, decided as in step 2, is vN, never a rewrite. `submitted` sides: `host.cancelTask`; only `canceled` is withdrawn. Others: "Follow daintree.hosts.steer-for-hub, run <run>, unit <side>: pause, keep all work"; acknowledged only when the reply shows each agent stopped or done, else unresolved, as is an unavailable lane. Brief the home lane for vN on the prior version; check ancestry (`git merge-base --is-ancestor`) and scope. Then withdrawn sides get step 3 at vN; acknowledged ones, done or not:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","taskId":"<run>-<side>-v<N>","text":"Follow daintree.hosts.steer-for-hub, run <run>, unit <side>: resume on contract v<N> <SHA>: <change>","referenceTaskIds":["<earlier taskIds>"]}
```

  Adopted once a reply names its agent on vN's SHA ("change sent" is not yet); its result: the original exchange if open, else `assistant.getInbox`.
- A result at its HEAD: each copy's blob there equals the current version's (`git rev-parse <HEAD>:<path>`), and daintree.hosts.verify-for-hub runs the compatibility test at that HEAD. Built on an older version: stale.

Stop and recovery: one brief per side per version; never merge commits across repositories.

Report back: versions (SHA, change, who decided); per side repository, base, verified HEAD and how, version, acknowledgement, test evidence; unresolved sides.
