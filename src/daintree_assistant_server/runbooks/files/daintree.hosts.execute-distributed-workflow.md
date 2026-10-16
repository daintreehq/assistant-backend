---
id: daintree.hosts.execute-distributed-workflow
title: Run a dependent plan across Remote Hosts
summary: 'From this Shell''s hub, run a multi-stage plan of dependent units across Remote Hosts and repositories to the end: a dependency graph kept in the conversation, each stage released only when everything it depends on is verified, failed and blocked units kept visible, and the plan resumed from the hosts'' task records after a restart.'
whenToUse: 'Use on the hub when the person has a multi-stage PLAN across their Remote Hosts: a dependency graph of three or more units in which several wait on others, often in different repositories: "contract first, then the server and the client against it, then the docs, each on the host that owns that project", "five units over three machines; both apps wait on the schema and the release notes wait on both apps", "run this dependency graph across my machines and keep going until it is done". The deliverable is the whole graph carried through stage by stage: every unit with its host, project and repository; a stage released only once the commits it depends on are verified; failed or blocked units kept visible; one table of every unit''s state and verified SHA. Do not use for one unit, or one hand-over between two hosts (daintree.hosts.delegate-to-host), independent pieces placed on whichever host can take them (daintree.hosts.place-work), the same work on every host (daintree.hosts.swarm-fan-out), finished branches combined into one (daintree.hosts.integrate-host-branches), a dependent plan for agents on this machine (daintree.orchestration.execute-durable-workflow), agreeing or changing the contract its units share (daintree.hosts.coordinate-contract-change), or only an account of where exchanges stand after a restart, or a cleanup (daintree.hosts.recover-and-clean-up).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
  - host.listTasks
---
The plan lives in this conversation and wakes nobody; task records hold no text and last a day. Each unit is a daintree.hosts.delegate-to-host unit, its brief and checks (`get_runbook` if absent).

1. Plan, per unit: id; host, project and lane (`host.listAssistants`) holding its repository (the lane confirms `origin`); that repository's base branch at SHA; `dependsOn`; its inputs from each predecessor (repository, branch, SHA; another project's contract also by path and version); acceptance; allowed writes; branch `<run>/<unit>`; `taskId` `<run>-<unit>`. Ids unique, dependencies known, no cycle; two predecessors in one repository need an integration unit (daintree.hosts.integrate-host-branches). Show the table before sending unless told to go.
2. Ready: every predecessor done, its exact SHA on the remote the dependent's project fetches (`git ls-remote`, PR head); unpushed feeds no host.
3. Per ready unit: mark it `sending` with the time, then `host.sendMessage` under its `taskId`, no `waitMs`. The brief adds: build on exactly these SHAs, no newer tip, no other repository's history; a missing pin or contract mismatch is `input-required`. `rate_limited`: send after `retryAfterMs`. End the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","taskId":"<run>-<unit>","text":"<brief>"}
```

On notice or resume, after the hosts foundation's reconcile:
- `sending`, no result: `host.getTask` by its `taskId`. `not_found`, host connected, within the hour of sending: resend that id (never delivered twice; `conflict`: held); later or `expired`: ask the person.
- `completed`, verified as delegate-to-host does: done, SHA and how noted. `failed`, `rejected` or a failed check: failed, dependents blocked and shown.
- Then steps 2-3, input SHAs re-read.
Conversation lost: send nothing until the person restores the plan; then reconcile by `taskId` and `<run>/*` branches.

Stop and recovery: one send per unit; rework: a new `taskId` referencing the failed one, on the person's go. Changing units or hosts is the person's call; the plan grants no push, merge, deploy. Stop only on request (daintree.hosts.steer-swarm): canceled stays canceled; a late completion releases nothing.

Report back: the table (unit, host, dependencies, state, SHA claimed vs verified and how), blocked and canceled units, what wakes the next step.
