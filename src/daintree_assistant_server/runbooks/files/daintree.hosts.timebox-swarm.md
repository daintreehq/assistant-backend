---
id: daintree.hosts.timebox-swarm
title: Give the Remote Hosts a fixed time on a task, then collect how far each got
summary: 'From this Shell''s hub, give each chosen Remote Host a bounded attempt with an absolute deadline that host enforces on its own agent, end the turn, and collect per host its partial result, host-side times and its own stop evidence: finished early, stopped by the deadline, stopped late, or unconfirmed.'
whenToUse: 'Use on the person''s own machine, the one that SENDS work to their Remote Hosts, when they give that work a FIXED AMOUNT OF TIME starting now, after which every host stops and reports how far it got: "let my other machines each have fifteen minutes at the image resize bug, then stop them and show me what they managed", "give every host forty minutes on the release notes and show me where each one got", a bounded attempt sent to one or several other machines. The deliverable is one row per host at the bound: finished early, stopped by the deadline, stopped late, blocked or unconfirmed, with that host''s own times, how far it got and next steps; each host enforces its own deadline, nothing on this machine does. Do not use for a time box on an agent on this machine, or on a host whose own assistant was handed such a deadline (daintree.orchestration.timebox-attempt), sending work with no time bound (daintree.hosts.swarm-fan-out; one host, daintree.hosts.delegate-to-host), stopping or changing a run already sent (daintree.hosts.steer-swarm), following one with no time bound to its end (daintree.hosts.supervise-swarm), or a later start or a wake when work finishes (daintree.orchestration.watch-outcome).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
1. Tell the person first: nothing here keeps time; each host's assistant holds its own box; its reply is the only stop evidence; a host cut off may neither stop nor answer; you report as hosts answer or when asked. Asked for a stop guaranteed on every host: it cannot be had; send once they accept that.
2. Hosts named, else `host.list`; one `host.listAssistants` round: each host's project and lane, else "not reached". In your shell: remote, base SHA (`git ls-remote`), run id, per host a new branch `<run>/<unit>`; then `date -u` and the UTC deadline with its date.
3. One `host.sendMessage` per lane, back to back, no `waitMs`, keeping the deadline and each send time; end the turn. Text:
   "Run <run> unit <unit>: follow daintree.orchestration.timebox-attempt here: <task, acceptance>; new worktree on <branch> from <SHA> of <remote>; CLI <named, else yours>. The box ends <deadline> by this host's `date -u`, setup included, never restarted. Under two minutes left: launch nothing; say so. Unable to hold a wait to it: <best effort | ask back first>. Push only if <said>; no new work after the box. Reply with its report, branch, HEAD, what enforced the stop, `date -u` on reading."

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<brief>"}
```

   A native wakeup (Claude Code's `ScheduleWakeup`): set it for the deadline plus ten minutes; Codex has none.

On notice or resume: `host.getTask` each named task; past the deadline, every open one too.
- `input-required`, cannot hold the box: the person picks best effort (same deadline) or drops it.
- `completed`: its row in host time (`delivery.acceptedAt` minus your send time is clock offset plus transit). Stopped by the deadline only if seen on the agent's screen by then; unpushed work stays the lane's claim.
- Ten minutes past it, still `submitted`/`working` or host disconnected: unconfirmed, never stopped; one stop via daintree.hosts.steer-swarm.

Stop and recovery: per lane one send, at most one stop; never extend. More time, on the person's word: a new message continuing that pane and worktree to a new deadline.

Report back: a row per host: host times, clock delta; finished early, stopped by the deadline, stopped late, blocked, unconfirmed or not reached, and how known; partial work and half-edited files, apart from what you verified; next steps.
