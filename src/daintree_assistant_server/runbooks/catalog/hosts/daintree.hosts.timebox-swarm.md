---
id: daintree.hosts.timebox-swarm
title: Give the Remote Hosts a fixed time on a task, then collect how far each got
summary: 'From this Shell, give each chosen Remote Host a bounded attempt with an absolute deadline that host enforces on its own agent, end the turn, and collect per host its partial result, host-side times and its own stop evidence: finished early, stopped by the deadline, stopped late, or unconfirmed.'
whenToUse: 'Use on the person''s own machine, the one that SENDS work to their Remote Hosts, when they give that work a FIXED AMOUNT OF TIME starting now, after which every host stops and reports how far it got: "let my other machines each have fifteen minutes at the image resize bug, then stop them and show me what they managed", "give every host forty minutes on the release notes and show me where each one got", a bounded attempt sent to one or several other machines. The deliverable is one row per host at the bound: finished early, stopped by the deadline, stopped late, blocked or unconfirmed, with that host''s own times, how far it got and next steps; each host enforces its own deadline, nothing on this machine does. Do not use for a time box on an agent on this machine, or on a host whose own assistant was handed such a deadline (daintree.orchestration.timebox-attempt), sending work with no time bound (daintree.hosts.swarm-fan-out; one host, daintree.hosts.delegate-to-host), stopping or changing a run already sent (daintree.hosts.steer-swarm), following one with no time bound to its end (daintree.hosts.supervise-swarm), or a later start or a wake when work finishes (daintree.orchestration.watch-outcome).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Nothing on this machine keeps time, and a notice may never come. Each host's assistant holds its own box in its own turn there, launching the agent, holding the wait and interrupting it at the deadline (daintree.orchestration.timebox-attempt); what that host's screens show at the deadline is the stop evidence. A host cut off may neither stop nor answer.

Procedure:
1. Tell the person that first. Asked for a stop guaranteed on every host: it cannot be had; send once they accept.
2. Hosts named, else `host.list`. In your shell: the remote, base SHA (`git ls-remote`), run id, per host a new branch `<run>/<unit>`; then `date -u` and the UTC deadline with its date. Per host project, an assistant through `host.call` (hosts foundation, Delegating); none to be had: "not reached".
3. One brief per host assistant, back to back, keeping each send time; end the turn. Brief: "Run <run> unit <unit>: follow daintree.orchestration.timebox-attempt here: <task, acceptance>; a new worktree on <branch> from <SHA> of <remote>; CLI <named, else yours>. The box ends <deadline> by this host's `date -u`, setup included, never restarted: hold your wait to it, stop the agent at it, then ask for its report. Under two minutes left: launch nothing; say so. Unable to hold a wait to it: <best effort | ask back first>. Push only if <said>; no new work after the box. Reply with its report, the agent's terminal id, branch, HEAD, what enforced the stop, and `date -u` at the stop and on reading."

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandOwned","args":{"terminalId":"<assistant terminalId>","generation":3,"command":"<brief>","handback":true,"notify":true}}
```

   A native wakeup in your tools: set one for the deadline plus ten minutes.

On notice or resume: before the deadline, a notice is a question back or an early finish. Cannot hold the box: the person picks best effort (same deadline) or drops it. Past the deadline, every host, notified or not: one `terminal.getStatus` through `host.call` on its assistant and, once named, its agent, `includeOutput:{"lines":30}`; the assistant's answer by `terminal.readLastMessageOwned`.
- Answered: its row in host time (its `date -u` against your send time is clock offset plus transit). Stopped by the deadline only if the agent's screen showed it stopped by then (`lastTransitionAt`), else stopped late. A pushed SHA: checked on the remote in your shell; unpushed work stays that host's claim.
- Ten minutes past it, the assistant or agent still `working`, or the host `disconnected`: unconfirmed, never stopped; one stop through daintree.hosts.steer-swarm.

Stop and recovery: per host one brief and at most one stop; never extend. More time, on the person's word: a new brief continuing that pane and worktree to a new deadline.

Report back: a row per host: host times, clock delta; finished early, stopped by the deadline, stopped late, blocked, unconfirmed or not reached, and how known; partial work and half-edited files, apart from what you verified; next steps.
