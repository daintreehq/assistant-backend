---
id: daintree.hosts.execute-distributed-workflow
title: Run a dependent plan across Remote Hosts
summary: 'From this Shell, run a multi-stage plan of dependent units across Remote Hosts and repositories to the end: a dependency graph kept in the conversation, each stage released only when everything it depends on is verified, failed and blocked units kept visible, and the plan resumed from its checkpoint and the hosts'' live state after a restart.'
whenToUse: 'Use from this Shell when the person has a multi-stage PLAN across their Remote Hosts: a dependency graph of three or more units in which several wait on others, often in different repositories: "contract first, then the server and the client against it, then the docs, each on the host that owns that project", "five units over three machines; both apps wait on the schema and the release notes wait on both apps", "run this dependency graph across my machines and keep going until it is done". The deliverable is the whole graph carried through stage by stage: every unit with its host, project and repository; a stage released only once the commits it depends on are verified; failed or blocked units kept visible; one table of every unit''s state and verified SHA. Do not use for one unit, or one hand-over between two hosts (daintree.hosts.delegate-to-host), independent pieces placed on whichever host can take them (daintree.hosts.place-work), the same work on every host (daintree.hosts.swarm-fan-out), finished branches combined into one (daintree.hosts.integrate-host-branches), one repeated mechanical change across repositories (daintree.hosts.migrate-across-repositories), a dependent plan for agents on this machine (daintree.orchestration.execute-durable-workflow), agreeing or changing a contract between the sides, with no whole plan to carry out (daintree.hosts.coordinate-contract-change), or only an account of where the hosts'' work stands after a restart, or a cleanup (daintree.hosts.recover-and-clean-up).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
The plan is yours: kept in this conversation and, for a run spanning sessions, in a checkpoint as the foundation says, with each unit's state and its host, project, worktree and terminal ids. It wakes nobody: only a notice or the person does. Each unit is one agent you drive on its host through `host.call`; the shared remote you read in your own shell.

Procedure:
1. Plan, per unit: id; host and project holding its repository (`host.list`); that repository's base branch at full SHA; `dependsOn`; its inputs from each predecessor (repository, branch, SHA; another project's contract also by path and version); acceptance (checks at the exact SHA plus the unit's criteria; a review unit only if the person or repository asks or a criterion needs inspection); allowed writes; branch `<run>/<unit>`; plus the person's models, retry bound and stop conditions, carried into every brief. Ids unique, dependencies known, no cycle; two predecessors in one repository need an integration unit (daintree.hosts.integrate-host-branches). Units push their branches: without the person's push authority, ask. Show the table before launching unless told to go.
2. Ready: every predecessor done, its exact SHA on the remote the dependent fetches (`git ls-remote`, PR head); unpushed feeds no host.
3. Per ready unit, mark it `launching` with the time, then on its host (holding the base SHA first: `git fetch origin <branch>` then `git cat-file -e <SHA>^{commit}` there, by `project.runCommand`) a worktree on `<run>/<unit>` from that SHA (`worktree.createWithRecipe`, `collisionPolicy:"error"`, then `worktree.waitUntilReady` with its `worktreeId`) and one agent there; record the returned ids and mark it `running`:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"<run>: <unit>","worktreeId":"<worktreeId>","prompt":"<brief>","notify":true,"handback":true}}
```

   The brief: confirm `origin` is <remote>, else stop; fetch and build on exactly these SHAs, no newer tip, no other repository's history; a missing pin or a contract mismatch: stop and report; acceptance and the checks to run; commit, push `<run>/<unit>`, reply with the SHA and each check's command and result. End the turn.

On notice or resume:
- `launching` with no terminal id (an outcome you never saw): `terminal.list` there for its name, `worktree.list` for its branch; adopt what exists, launch only what is proven absent.
- Finished is a claim: its SHA on `<run>/<unit>` (`git ls-remote`); `git diff --stat <base>...<SHA>` in a scratch clone, within its allowed writes; its acceptance checks at that SHA on its host (`project.runCommand` there, or `project.runCheck` at Full, `cwd` its worktree). Done, with the SHA and how it was checked. A failed check, an error, or a refusal there: failed, its dependents blocked and shown.
- A question within scope: answer once with `terminal.sendCommandOwned` there, `notify:true, handback:true`; otherwise ask the person.
- Then steps 2-3, input SHAs re-read. A notice that never came: reconcile as here and go on; the plan already approved needs no new go.
- Plan lost, conversation and checkpoint: launch nothing until the person restores it; then reconcile each unit by its `<run>/<unit>` branch and, on its host, its terminal by name.

Stop and recovery: one agent per unit. A failed acceptance check inside the unit's scope gets one corrective prompt per unit to the same agent (`notify`, `handback`), or as the person bounded it, then verify again; exhausted, it stays failed and its dependents blocked while independent units go on. Other rework, on the person's go, is one send to the same agent or a new one in its worktree. After you restart, sends to the old agents are refused (`RESOURCE_NOT_OWNED`): read them, and launch anew only once the old one is idle. Changing units or hosts is the person's call; the plan grants no push, merge or deploy beyond theirs. Stop only on request (daintree.hosts.steer-swarm): a stopped unit stays stopped, and a late finish releases nothing.

Report back: the table (unit, host, dependencies, state, SHA claimed vs verified and how), blocked and stopped units, what wakes the next step.
