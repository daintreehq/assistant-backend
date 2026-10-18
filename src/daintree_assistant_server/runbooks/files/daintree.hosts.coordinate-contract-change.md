---
id: daintree.hosts.coordinate-contract-change
title: Agree and amend one interface contract across Remote Hosts
summary: 'From this Shell, have the interface between projects on different Remote Hosts written, agreed and frozen at one commit, brief every side with that exact version, carry each change as a new version the affected hosts pause for, acknowledge and adopt, and accept only results checked against the current version.'
whenToUse: 'Use from this Shell when Remote Hosts build the sides of ONE INTERFACE in DIFFERENT REPOSITORIES (an API and its client, a schema and the services reading it, a message producer and its consumers) and the CONTRACT between them has to be agreed before they build or CHANGED while they do: "pin down the event schema before the producer on one machine and the consumer on another start", "the app side on the laptop needs another field; change the contract and move every machine onto the new version", "the hosts are building against different versions of the API spec, get them back onto one". The deliverable is the contract''s versions and each side''s adoption: every version a commit by full SHA in the one repository that holds it, agreed before anything builds on it; each side briefed with that version on its own repository''s base; a change made a new version, sent once the affected hosts acknowledged pausing, then adopted side by side; a result built on an older version rejected as stale; every side checked with the same compatibility test. Do not use for both sides built by agents on this machine (daintree.orchestration.contract-first-split), a staged plan of dependent units with no contract to agree or change (daintree.hosts.execute-distributed-workflow), one unit for one host or a single hand-over between two (daintree.hosts.delegate-to-host), stopping or changing sent work with no contract change (daintree.hosts.steer-swarm), one repeated mechanical change across repositories with no contract to agree (daintree.hosts.migrate-across-repositories), or finished branches combined into one (daintree.hosts.integrate-host-branches).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Each side, and the contract's author, is one agent you drive on its host, every call through `host.call` there. What is pushed you check in your own shell: a scratch clone per remote (`git clone --filter=blob:none --no-checkout <remote>`), `git fetch --prune` before each check. Keep per side: host, project, worktree and terminal ids, the version it was sent.

Procedure:
1. Per side: host and project (`host.list`), its repository's remote, base branch at full SHA (`git ls-remote`), its contract copy's path; a SHA never crosses repositories. Contract: one home repository and paths (schema, examples, error cases, a compatibility test). Sides push branches: without the person's push authority, ask before the first launch.
2. Contract: on the home host (holding the base SHA: Full, `project.runCommand` `git fetch origin` there first; Core, an agent fetches first) a worktree on new branch `contract/<run>` from that SHA (`worktree.createWithRecipe`, `collisionPolicy:"error"`, then `worktree.waitUntilReady` with its `worktreeId`), one author agent there: write only those paths, commit, push `contract/<run>`, reply with the SHA. Check its scope on the remote (`git diff --name-only <base>..<SHA>`), show the person, freeze on their yes (the interface is theirs unless left to you): v1, full SHA. Versions are `contract/<run>`'s history; keep the author's terminal for amendments.
3. Each side likewise: its worktree on `<run>/<side>` from its own base, one agent:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"<run>: <side>","worktreeId":"<worktreeId>","prompt":"<brief>","notify":true,"handback":true}}
```

   The brief: the home remote and v1's SHA; copy <home paths> from it unchanged to <copy path> (the home side builds on v1 itself); fixtures from its examples; change only <side paths>; a contract change needed: stop and report exactly what must change, never edit the copy; commit, push `<run>/<side>`, reply with HEAD and the contract SHA used. End the turn.

On notice or resume:
- A change, a side's or the person's, decided as in step 2, is vN, never a rewrite. Pause each affected side still working: `terminal.interruptOwned` there (typed text waits behind its turn); acknowledged only when `terminal.getStatus` there (`includeOutput`) shows it stopped or done, else unresolved, as is a host that cannot be reached. The author amends on the prior version; check ancestry (`git merge-base --is-ancestor <prior SHA> <vN SHA>`) and scope. Sides not yet launched get step 3 at vN; each acknowledged one, done or not, one `terminal.sendCommandOwned` there with `notify:true, handback:true`: "Contract v<N> at <SHA>: <change>. Commit your work in progress, bring the copy to v<N> unchanged, adapt, continue." Adopted once its reply says it builds on vN; "sent" is not yet.
- A result: its HEAD on the remote (`git ls-remote`); each copy's blob there equals the current version's (`git rev-parse <HEAD>:<copy path>` against `<vN SHA>:<home path>`); the compatibility test at that HEAD on its host: `project.runCheck` or `project.runCommand` there, `cwd` its worktree, at Tool set Full; at Core its agent runs it and the exit code is a claim. Built on an older version: stale, sent back once with the current one.
- A question within scope: answer once by a send, `notify:true, handback:true`; one touching the interface goes to the person.

Stop and recovery: one agent per side, one send per side per version; before any launch, `terminal.list` there: never a second agent while one lives. A send refused `RESOURCE_NOT_OWNED` (you restarted): read it, and a new agent in that worktree only once the old one is idle. A refused or unreachable side stays unresolved, never moved to another host unasked. Never merge commits across repositories.

Report back: versions (SHA, change, who decided); per side its host, repository, base, verified HEAD and how, version, acknowledgement, test evidence; unresolved sides.
