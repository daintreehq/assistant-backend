---
id: daintree.hosts.swarm-fan-out
title: Send the same work to every remote host
summary: 'From this Shell''s hub, give the assistant on every chosen Remote Host the same brief at once, each on its own run branch, end the turn, and bring back one table with a row per host, each result verified by artifact and any host still open named as open.'
whenToUse: 'Use when the person wants the SAME WORK done on EVERY Remote Host this Shell reaches, or on the several they name, one copy per machine: "run this on all my machines", "have each machine try this", "have every host build the parser on its own branch and show me what each got", a fix, feature or experiment replicated across their other Macs or servers at once. The deliverable is one table with a row per host (project, agent CLI, branch, commit SHA, checks, pane), each result verified on the shared remote or by a narrow follow-up question, and any host still working or not reached shown as such, never folded into one overall success. Do not use for one unit on one host or a hand-over between two (daintree.hosts.delegate-to-host), readying an assistant on each host with no work sent (daintree.hosts.start-swarm), agent usage left on each host (daintree.hosts.check-usage-limits), one question to one host''s assistant (daintree.hosts.ask-host-assistant), what became of work already sent (daintree.hosts.recover-and-clean-up), independent units for agents on this machine (daintree.orchestration.fan-out-work), or a host''s own assistant receiving the hub''s message (daintree.hosts.handle-hub-message) or carrying out its unit (daintree.hosts.do-delegated-work).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
One brief to every chosen host at once, each on its own branch; one table back. Different pieces per host, a split suite or an environment matrix is not this: say so.

1. Hosts the person named, else all in `host.list`; each keeps a row. `host.listAssistants` for all, one round: each host's own project for the work's repository, and its lane's `slot` and `generation`. Not `connected`, not `ok`, or no `available` lane: that host's row, no retry; set a lane up (daintree.hosts.start-swarm) only if the request covers it. Two projects fit: ask.
2. In your shell pin the remote URL, the base SHA (`git ls-remote`), a fresh run id, and per host a unit id naming host and project, its branch `<run>/<unit>` absent from the remote.
3. One `host.sendMessage` per host, to its lane's `slot`, back to back, no `waitMs`; then end the turn. The text differs only in unit, branch and CLI:
   - Objective: the person's words, checkable acceptance; to implement, follow daintree.hosts.do-delegated-work.
   - Inputs: run and unit ids, remote, base SHA, branch; CLI: the person's for that host, else that lane's `agentId`.
   - Scope: this project if its remote matches; checks by command, or none; push or PR only if the person said.
   - Report: `completed` with repository path, branch, SHA, PR URL, each check's command and result, pane id, no diffs; `input-required` on a mismatch, a CLI not signed in, or doubt.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<brief>"}
```

   Pace to 30 sends a minute (10 per host), starts and questions included; `rate_limited` was not sent: send it after `retryAfterMs`.

On notice or resume: fill each named host's row; end the turn while any is `submitted` or `working`.
- `completed`, not pushed: one narrow question per host in its `contextId` (`git log -1 --stat <branch>`, each check's exit code); its answer settles the row.
- Still open once the rest are in: open, never failed.

Stop and recovery: one send per host. A resend after a lost reply is the person's call: a new message with the same run and unit ids, so that host resumes rather than redoes.

Report back: one table, a row per chosen host: project, CLI, branch, SHA claimed vs verified and how, checks, pane id. Side by side, no winner. "All done" only when every row is verified.
