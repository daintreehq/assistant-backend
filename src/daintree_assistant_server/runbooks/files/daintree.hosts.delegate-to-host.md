---
id: daintree.hosts.delegate-to-host
title: Delegate work to a remote host's assistant
summary: Hand one unit of work to the assistant on one Remote Host with a brief by reference, end the turn, verify the branch, commit or PR it reports, and relay a verified result to the next host when one piece depends on another.
whenToUse: 'Use when the person wants ONE piece of WORK done by the Daintree assistant on another machine, a Remote Host this Shell reaches, whatever the work is (a bug, a flaky or failing test, a feature, docs): "get the build box to fix the failing checkout test and open a PR", "have the assistant on my other Mac implement this issue and tell me the SHA", or a second host starting once the first host''s change lands ("when that machine pushes the API change, have the other one build the client on it"). The deliverable is that host''s commit, branch or PR, verified by reference before anything advances, and for that single dependency a deliberate relay of the verified result to the second host. Do not use for work by an agent on THIS machine (daintree.edits.spawn-visible-agent; several units here, daintree.orchestration.fan-out-work; a started task moved to another local agent, daintree.agents.hand-off-task), for a later start or wake-up on this machine (daintree.orchestration.watch-outcome), for a dependent plan on this machine (daintree.orchestration.execute-durable-workflow), or for a question to that machine''s assistant whose deliverable is its answer (daintree.hosts.ask-host-assistant).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
  - host.startAssistant
  - host.cancelTask
---
1. Resolve the named host (`host.list`), then in `host.listAssistants` the project holding the work's repository and its lane. Two fit or none: ask. No lane: start one only if the request covers it.
2. In your own shell pin the remote URL (the person's or your clone's), the base branch's SHA (`git ls-remote`), a run id and, for a change, a run/unit branch not yet on the remote.
3. One `host.sendMessage`, no `waitMs`, briefing:
   - Objective: the person's words, the deliverable named plainly (implement, fix, review) so it searches for its own runbook, and checkable acceptance criteria. Named commands and skills resolve on that host.
   - Inputs: run and unit ids, remote URL, base branch at SHA, the new branch or the SHA to review, issue or PR URL.
   - Scope: this project, if its remote matches; the agent CLI if named; the checks to run; push or open a PR only if the person said so.
   - Report: `completed` with branch, SHA, PR URL, each check's command and result, pane id (a review: findings); `input-required` on another remote or base SHA, a missing CLI or command, or doubt.
   Then end the turn.

On notice or resume:
- `submitted`, `working`: end the turn again; never chase.
- `input-required`: a CLI swap or a push the person did not authorise is their call.
- `completed`: advance only on the acceptance criteria. Pushed: `git ls-remote` shows the reported SHA; a PR's head, base, checks and diff fit them (`gh pr view`, `gh pr diff`). Not pushed: one narrow question in the same `contextId` (`git log -1 --format='%H %P' <branch>`, check exit codes). What you can't check stays unverified.
- Relay: when the person's condition holds (pushed, PR open, merged) at a verified SHA, brief the next host with that branch and SHA; `referenceTaskIds` is only the record:

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<brief>","referenceTaskIds":["<predecessor taskId>"]}
```

  The branch must reach the remote: without push authority, ask before the first send.

Stop and recovery: one send and one evidence question per unit; more work after a failed or wrong unit is a new message referencing it, on the person's go. `host.cancelTask` only on request. More than one dependency is not a relay: say so.

Report back: per unit, branch and SHA claimed vs verified (how), the host's checks, the next hop and its condition.
