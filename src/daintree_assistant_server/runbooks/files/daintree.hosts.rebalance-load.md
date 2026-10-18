---
id: daintree.hosts.rebalance-load
title: Rebalance a running swarm off an overloaded or unavailable Remote Host
summary: 'From this Shell, find which Remote Host in a running swarm is overloaded, unavailable or out of agent allowance from dated per-host readings, start fewer agents there and release the pieces it has not started, move only that unstarted work to hosts with room, and compare readings before and after.'
whenToUse: 'Use when a swarm ALREADY RUNNING across the person''s Remote Hosts is LOPSIDED: one machine is swapping, hot, pegged or running too many agents while others sit idle, a host dropped out mid-run, or its agent CLI ran out of allowance, and the person wants the pressure spread: "one host is swapping while the others are idle, rebalance the swarm", "that host hit its usage limit, take the pressure off without losing work", "relieve the swarm''s resource contention with per-host evidence". The deliverable is per-host load and allowance readings with their times, a proposal shown before anything moves unless the person said to just do it, the pressured host running fewer agents and releasing pieces it has not started, only that unstarted work placed on hosts with room and nothing already running sent twice, and a second reading showing what changed and what stays throttled. Do not use for first placement of new pieces (daintree.hosts.place-work), moving one started unit to another machine (daintree.hosts.reassign-host-work), only reading usage left per host (daintree.agents.check-usage-limits), stopping or calling off a run, or correcting its brief (daintree.hosts.steer-swarm), following a run to its end with nothing moved (daintree.hosts.supervise-swarm), or load on this machine alone (daintree.runtime.relieve-resource-contention).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Only work not started moves; a started unit stays, throttled, or goes to daintree.hosts.reassign-host-work on the person's word. Allowance is per host and account, never summed: one account on two hosts is one allowance.

1. Readings: `host.list` `connection`, `load` (`cpuPercent`, `memoryPressure`, `thermal`) by `sampledAt`, `agentsObserved` (the whole machine, the person's own agents too). Null is unmeasured, never idle; an old `sampledAt` is no reading. Codex allowance per host, through `host.call`:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.readQuota","args":{}}
```

   Per window `usedPercent` and `resetsAt`, with `fetchedAt`; `unavailable` is unknown. No tool reads Claude Code's: an agent screen saying it hit its limit (`terminal.getStatus` `includeOutput` there) is the evidence. At Tool set Full, top processes: `project.runCommand` `ps -Ao pid,pcpu,pmem,etime,comm -r | head -15`, read only, attributed where you can.
2. This run's units per host, from your plan: launched (terminal id; `terminal.getStatus` there: working, waiting, idle), held by a host's assistant you delegated a batch to (its last reply), or not launched.
3. Throttle each pressured host (high readings, out of allowance or `disconnected`): no new launches there. Your agents working there: one queued `terminal.sendCommandOwned` each, no interrupt: "Keep your work. From now on: tests with at most 2 workers; ask before a full build." A host's assistant holding this run's batch: one send by its `terminalId` and `generation`, `handback:true`, `notify:true`: "Run <r>: launch at most <n> more of its agents; hold the units not started and reply which they are; keep all work; kill, delete or restart nothing." End the turn on that send.
4. Movable: not launched, or held and released by that assistant's reply; nothing launched on a `disconnected` host moves (it may still run there). Target each by daintree.hosts.place-work's checks, never a pressured host. Show unit, from, to, reason and reading times, the rest apart; move on the person's yes unless they said just do it.
5. Move: each freed unit launched once on its target, briefed as daintree.hosts.delegate-to-host does, `notify:true`, `handback:true`; end the turn. Then the pressured host's second readings.

On notice or resume: a held or released unit is the assistant's claim: before launching it elsewhere, `terminal.list` through `host.call` there must show no agent started on it. Second readings beside the first, with times: no fall, say so and name the next offender.

Stop and recovery: one throttle per agent, one send per assistant, one launch per freed unit. A lost brief: ask the person. No settings copied, capacity bought or account switched. Throttles stay until lifted.

Report back: per host both readings with times, Codex allowance with `fetchedAt`; per unit moved (from, to, terminal id), stayed, held or released; what stays throttled or waits on the person.
