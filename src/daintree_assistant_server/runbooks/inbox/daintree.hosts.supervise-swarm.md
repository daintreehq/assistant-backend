---
id: daintree.hosts.supervise-swarm
title: 'Supervise a run on the Remote Hosts through its delegated exchange updates'
summary: 'Keep work already out on the Remote Hosts moving across turns on each delegated exchange update: wait quietly while hosts work, answer a host''s question within what the person asked, bring decisions to the person, verify each finished unit by artifact, and check once on a host left quiet after its peers are in, with no polling and nothing sent twice.'
whenToUse: 'Use once this Shell''s work is OUT on the Remote Hosts and the person wants the run followed to its end: "keep an eye on the studios and tell me when they''re done", "keep the swarm going and answer what you can", a remote host asking whether it may do something, one host still working long after the others came back, or the line "Daintree: delegated exchange update (exchanges …)" for work it sent. The deliverable is the run kept moving: every update read, a host''s question answered within the person''s request, a host''s own message answered, decisions brought to the person, each finished unit verified by branch, SHA or PR, a quiet host checked once, and one row per unit at the end. Do not use for sending the work in the first place (daintree.hosts.swarm-fan-out, daintree.hosts.place-work, daintree.hosts.delegate-to-host), changing its course (daintree.hosts.steer-swarm), or agents on this machine (daintree.orchestration.supervise-fleet).'
risk: external
requiredTools:
  - host.getTask
  - host.listTasks
  - host.sendMessage
optionalTools:
  - host.listAssistants
  - assistant.getInbox
  - assistant.reply
hostEligibility: configured
---
Beside the ids the foundation keeps, give each unit a row: state, when it last moved, result, and each follow-up's `taskId` and purpose.

1. Resume from your rows. Lost: `host.listTasks` per host finds the exchanges but not their briefs; match them to this run from what you kept, or ask the person. Never resend work.
2. On an update line, read what it names (foundation). On resume or a native wakeup, also `host.listTasks`, every page, from the cursor of each host with open rows (a wake can be dropped; `reset` is a full relisting), then `host.getTask` what moved. Per unit:
   - `submitted`, `working`: keep waiting; nothing to the person.
   - `input-required`: answer in scope (foundation) once; it stays open until the lane completes your answer. Beyond scope: the person, the host's words quoted.
   - `completed`: verify (foundation); not pushed, one narrow evidence question in its `contextId`.
   - `failed` (`outcomeCode` `expired` too), `rejected`, `canceled`, or its lane missing from a complete `host.listAssistants` (no `projectsOmitted`): report it. A `disconnected` host may still reply; moving the work is daintree.hosts.reassign-host-work, on the person's word.
   Rows awaiting you, a check or the person carry over.
3. A host's own message (its `Daintree: assistant inbox` line, or on resume): `assistant.getInbox`, every page; answer each once, a decision only after the person makes it:

```json tool=assistant.reply surface=hub-full
{"taskId":"<inbox taskId>","state":"completed","text":"<answer>"}
```

4. A quiet host: once the other hosts are in and one has gone several times their time with no update, `host.getTask` its open tasks; still `working`, one narrow `host.sendMessage` in its `contextId` asking where the work stands. Report what you saw ("`working` 20 minutes, no update since it started"), never a guess at why.
5. End the turn while any row is open. A native wakeup in your tools (Claude Code's `ScheduleWakeup`): set one, at the quiet-host threshold. None (Codex): tell the person which host you wait on and what you will check when they or an update return.

Stop and recovery: `rate_limited`, read or send: hold that call until `retryAfterMs`, then once; never spin. One answer per question, one check per quiet host; no new lane, no unit moved.

Report back: a row per unit: host, state, branch and SHA claimed vs verified and how; what waits on the person; timings as observed.
