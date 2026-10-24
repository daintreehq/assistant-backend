---
id: daintree.hosts.delegate-to-host
title: Delegate work to a remote host or its assistant
summary: Hand one unit of work to one Remote Host, an agent you drive there or its assistant, with a brief by reference, end the turn, verify what it reports by reference, and relay a verified result to the next host when one piece depends on another, or only ready a host's assistant.
whenToUse: 'Use when the person wants ONE piece of WORK done on another machine, a Remote Host this Shell reaches, by an agent there or its Daintree assistant, whatever the work is (a bug, a failing test, a feature, docs, a review): "get the build box to fix the failing checkout test and open a PR", "have the assistant on my other Mac implement this issue and tell me the SHA", or a second host starting once the first host''s change lands ("when that machine pushes the API change, have the other one build the client on it"). Also when the person only wants Claude Code or Codex READY as a host''s assistant, or each named host''s, before any work goes out. The deliverable is that host''s commit, branch, PR or review findings, verified by reference before anything advances (for one dependency, the verified result relayed to the second host), or each host''s assistant ready or its blocker. Do not use for work by an agent on THIS machine (daintree.edits.spawn-visible-agent; several units here, daintree.orchestration.fan-out-work; a started task moved to another local agent, daintree.agents.hand-off-task), for a later start or wake-up on this machine (daintree.orchestration.watch-outcome), for a dependent plan on this machine (daintree.orchestration.execute-durable-workflow), for a question to that machine''s assistant whose deliverable is its answer (daintree.hosts.ask-host-assistant), for the same work on several hosts at once (daintree.hosts.swarm-fan-out), for several hosts'' finished work combined into one branch (daintree.hosts.integrate-host-branches), for stopping or changing work already sent (daintree.hosts.steer-swarm), or for a staged plan of several dependent units (daintree.hosts.execute-distributed-workflow).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Drive the host yourself (the hosts foundation); delegate to its assistant only when the person asks for it, or its own judgment or context is the point.

Procedure:
1. `host.list`: the named host and, in its `projects`, the one holding the work's repository. Not `connected`, or `control:false`: report it, stop. Two fit, or none: ask; a doubtful match: daintree.hosts.verify-project-placement first.
2. In your own shell pin the remote URL, the base branch's full SHA (`git ls-remote`) and, for a change, a unit branch not yet on the remote.
3. Brief, by reference:
   - Objective: the person's words (named commands and skills resolve on that host), checkable acceptance criteria.
   - Inputs: remote URL, base branch at SHA, the unit branch or the SHA to review, issue or PR URL.
   - Scope: stop and say so if `origin` is another repository or the base is not at that SHA; the checks to run; push or open a PR only if the person said so.
   - Report: branch, full SHA, PR URL, each check's command and result; a review: findings with `file:line`.
4. Driving, each call through `host.call` there: a change gets `worktree.createWithRecipe` (`newBranch`, the unit branch off the base), then `worktree.waitUntilReady` (only `ready` admits the launch); a review, a worktree on that branch (`worktree.list`, else created). Launch once; keep the `terminalId` with the host:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.launch","args":{"agentId":"claude","name":"Fix: checkout test","worktreeId":"<that host's worktreeId>","prompt":"<brief>","handback":true,"notify":true}}
```

   Delegating: to its assistant as the hosts foundation says, the same brief, saying whether it may use its own agents. End the turn.

Ready only (no work sent): for each host named, else each `connected` one, `assistant.list` through `host.call` in its project (as step 1; one found only by this project's name needs the person's yes), in one parallel round:
- One at its prompt (`agentState:"waiting"`, `waitingReason:"prompt"`), of the CLI asked for if any: ready, no call.
- One working, stopped on a question or error, or of another CLI: report it; never start beside it unasked.
- None: one `assistant.start` (`agentId` as asked, else omitted for that host's default). `ready:false` still runs: report `notReadyReason`, never start another. No `vacantSlots`: the limit; never close one to make room. `ASSISTANT_START_REFUSED` or a refused call: quote `detail` and the setting; nothing more there.
Then stop; sending work is the next request.

On notice or resume: read `terminal.getStatus` there with `includeOutput:{"lines":50}`. A question: answer only within the brief, with `notify` again (a delegated assistant stopped on a prompt in its own pane: only a person there answers); a CLI swap or a push the person did not authorise is theirs. Still working: end the turn again; never re-prompt. Stopped with its report (a delegated assistant's by `terminal.readLastMessageOwned` there): advance only on the acceptance criteria.
- Pushed: in your shell `git ls-remote` shows the reported SHA; a PR's head, base, checks and diff fit them (`gh pr view`, `gh pr diff`).
- Not pushed: on the host, at Full `project.runCommand` in its worktree path (`git log -1 --format='%H %P' <branch>`, `git status --porcelain`) and the criteria's checks by `project.runCheck`; at Core one evidence question to the same agent. What you cannot check stays unverified.
- Relay: when the person's condition holds (pushed, PR open, merged) at a verified SHA, brief the next host from step 1 with that branch and SHA as its base. The branch must reach the remote: without push authority, ask before the first launch.

Stop and recovery: one initial launch or send and one evidence question per unit; a missing notice is a status read, never a relaunch. With repair authorised, a known failing check gets one corrective prompt per unit to the same agent (their bound overrides), then verify again; other rework is a new brief on the person's go; `terminal.interruptOwned` only on request. `RESOURCE_NOT_OWNED` after you restarted: read only; the person or that host's assistant closes it. More than one dependency is not a relay: daintree.hosts.execute-distributed-workflow.

Report back: per unit, host, project and `terminalId`, driven or delegated, branch and SHA claimed vs verified (how), checks you ran vs claimed, the next hop and its condition; readying, per host: project, slot, agent, `terminalId`, `generation`, reused or started, ready or the blocker and its setting.
