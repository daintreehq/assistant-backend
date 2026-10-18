---
id: daintree.hosts.prepare-host-maintenance
title: Prepare a Remote Host for an update or restart, then reconcile
summary: 'Before the person updates, restarts or reboots a Remote Host, report what that will stop there and what cannot be seen from here, get the work running on it checkpointed, and after the host returns reconcile its projects, worktrees and agents before sending anything more.'
whenToUse: 'Use BEFORE the person updates, restarts or reboots Daintree on a Remote Host, or the machine it runs on, to find out what that will stop there (its projects, agents and delegated swarm jobs, and what this Shell cannot see), have the work on it checkpointed, and, once the host is back, reconcile it. Examples: "before restarting the remote Daintree server, show what projects and swarm jobs will stop", "get the other Mac ready for a Daintree update without losing what I sent it", "the host is back from the update, can it take work again". The deliverable is an impact report per host with its limits stated, each agent''s acknowledged checkpoint (branch, SHA, pushed or not, uncommitted files, writes in flight), and after the return a reconciliation of its projects, worktrees and agents before anything more is sent. The person does the update or restart. Do not use for an unplanned restart or dropped link already behind us (daintree.hosts.recover-and-clean-up), putting a whole run on hold for later (daintree.hosts.park-swarm), stopping or changing a run (daintree.hosts.steer-swarm), a read of the hosts with no restart ahead (daintree.hosts.survey-hosts), adding a machine as a host (daintree.hosts.connect-host), or Daintree itself misbehaving on this machine (daintree.diagnostics).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
The person updates or restarts the host in Settings, Hosts or at the machine; no tool restarts it. Daintree restarting there ends every terminal and agent on it mid-task; worktrees and commits stay. A reboot is that restart, and Daintree comes back only with start at login. A Shell restart leaves the host working, but notices armed before it reach nobody, and its agents there can be read but not sent to or closed (`RESOURCE_NOT_OWNED`).

1. Snapshot per host: `host.list` (`connection`, `control`, `agentsObserved` with `load.sampledAt`: whole-machine counts, observed). Per project in its `projects`, through `host.call`: `terminal.list` (no filter, and `location:"background"`), one `terminal.getStatus` over its agent terminals, `worktree.list`, `assistant.list`. Yours: `terminal.list` `owned:true`.
2. Impact per project: working and waiting agents (yours, a host's assistant's, the person's), assistants mid-turn, dev servers and plain terminals. Unobserved, stated as risk: a project not in `projects` (it lists 50), control off, a call not `ok`, other Shells' work. The update dialog in Settings, Hosts lists what ends per project, not unpushed work: they read it; "Update when idle" waits.
3. To save work: no new work to that host. Each of this run's agents there gets one queued `terminal.sendCommandOwned` (no interrupt: it lands at the agent's next stop), `handback:true`, `notify:true`:

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.sendCommandOwned","args":{"terminalId":"<terminalId>","command":"Checkpoint before this host restarts. Let any push, PR, comment or release in flight finish (unsure: say blocked), then stop. Reply: worktree, branch, HEAD, pushed or not, uncommitted files, writes in flight.","handback":true,"notify":true}}
```

   Add "commit and push to <run branch>" only if the person allowed it. A host's assistant running part of the run gets the same brief by its `terminalId` and `generation`: stop only the agents it launched for you, close nothing, report each. Agents not yours are reported, never sent to. End the turn.

On notice or resume: a checkpoint is acknowledged only when that agent is idle by `terminal.getStatus` there and its writes are settled; a pushed claim is checked with `git ls-remote` on the shared remote from your shell. Anything less is unacknowledged.

When they say it is back: `host.list` against the snapshot (`connection`, the same `projectId`s); per project `worktree.list` and `terminal.list` through `host.call`: terminals and agents from before are gone. Build: they check it in Settings, Hosts; no tool shows it. Work resumes only as a new launch in its worktree, briefed from its checkpoint after reading what survived there (HEAD, status), never the old prompt resent unasked (daintree.hosts.recover-and-clean-up).

Stop and recovery: one checkpoint brief per agent or assistant, one narrow follow-up for a missing field; a notice that never comes is a `terminal.getStatus` poll there, paced to the work. Nothing is interrupted, closed or relaunched without the person.

Report back: per host and project, what ends and what is unobserved; each agent's checkpoint (branch, SHA, pushed: verified or claimed) or unacknowledged; after the return, what is back, what is gone, what waits.
