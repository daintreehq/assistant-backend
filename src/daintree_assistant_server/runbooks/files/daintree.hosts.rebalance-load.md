---
id: daintree.hosts.rebalance-load
title: Rebalance a running swarm off an overloaded or unavailable Remote Host
summary: 'From this Shell''s hub, find which Remote Host in a running swarm is overloaded, unavailable or out of agent allowance from dated per-host readings, have its own assistant run fewer agents and release pieces it has not started, move only that unstarted work to hosts with room, and compare readings before and after.'
whenToUse: 'Use on the hub when a swarm ALREADY RUNNING across the person''s Remote Hosts is LOPSIDED: one machine is swapping, hot, pegged or running too many agents while others sit idle, a host dropped out mid-run, or its agent CLI ran out of allowance, and the person wants the pressure spread: "one host is swapping while the others are idle, rebalance the swarm", "that host hit its usage limit, take the pressure off without losing work", "relieve the swarm''s resource contention with per-host evidence". The deliverable is per-host load and allowance readings with their times, a proposal shown before anything moves unless the person said to just do it, the pressured host''s own assistant lowering how many agents it runs and releasing pieces it has not started, only that unstarted work placed on hosts with room and nothing already running sent twice, and a second reading showing what changed and what stays throttled. Do not use for first placement of new pieces (daintree.hosts.place-work), moving one started unit to another machine (daintree.hosts.reassign-host-work), only reading usage left per host (daintree.hosts.check-usage-limits), stopping or calling off a run, or correcting its brief (daintree.hosts.steer-swarm), or load on this machine alone (daintree.runtime.relieve-resource-contention).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.listTasks
  - host.getTask
  - host.sendMessage
optionalTools:
  - host.cancelTask
---
Only work not started moves; a started unit stays, throttled, or goes to daintree.hosts.reassign-host-work on the person's word. Allowance is per host and account, never summed.

1. Readings: `host.list` `connection`, `load` (`cpuPercent`, `memoryPressure`, `thermal`) by `sampledAt`, `agentsObserved` (whole machine, the person's own too). Null is unmeasured, never idle; an old `sampledAt` is no reading. `host.listAssistants`; this run's units by `host.listTasks` (paging) and `host.getTask`.
2. Per pressured host (high readings or out of allowance), one `host.sendMessage`, no `waitMs`, to the lane holding its units: "Run <r>, units <hostTaskIds>. A timed reading, top processes attributed, by daintree.runtime.relieve-resource-contention steps 1-2, read only<; usage by daintree.agents.check-usage-limits>. Then: launch at most <n> of this run's agents from now on; hold units not started until I say; by daintree.hosts.steer-for-hub, queued, no interrupt: tests at most 2 workers, ask before a full build. Keep all work; kill, delete or restart nothing. Reply: reading, units started or held, throttles." End the turn.
3. Movable: not sent, held, or `submitted` on a connected host; nothing reaches a `disconnected` one or is withdrawn there. Target each by daintree.hosts.place-work steps 2-5 (step 4 ignoring only their originals), never a pressured host; one account on two hosts is one allowance. Table: unit, from, to, reason, reading times; others apart. Move on the person's yes unless they said just do it.
4. `submitted`: `host.cancelTask`; only `state:"canceled"` frees it. Each pressured lane: "<Never start <units>, released | Resume held units>; an exchange left with nothing to run: reply completed. Then the same timed reading, and each agent's throttle as its screen shows." Then one `host.sendMessage` per freed unit, daintree.hosts.delegate-to-host's brief, no `waitMs`. End the turn.

On notice or resume: the lane's two readings beside newer `host.list` samples; no fall: say so, name the next offender.

Stop and recovery: one throttle and one follow-up per lane, one send per unit; brief lost: ask the person. No settings copied, capacity bought or account switched. Throttles stay until lifted.

Report back: per host both readings with times, allowance per CLI as its lane read it; per unit moved (from, to, `taskId`), stayed, released or withdrawn; what stays throttled or waits on the person.
