---
id: daintree.hosts.prepare-host-maintenance
title: Prepare a Remote Host for an update or restart, then reconcile
summary: 'Before the person updates, restarts or reboots a Remote Host, report what that will stop there and what cannot be seen from here, get each enrolled lane to checkpoint its work, and after the host returns reconcile its projects, lanes and exchanges before sending anything more.'
whenToUse: 'Use BEFORE the person updates, restarts or reboots Daintree on a Remote Host, or the machine it runs on, to find out what that will stop there (its projects, agents and delegated swarm jobs, and what the hub cannot see), have the work on it checkpointed, and, once the host is back, reconcile it. Examples: "before restarting the remote Daintree server, show what projects and swarm jobs will stop", "get the other Mac ready for a Daintree update without losing what I sent it", "the host is back from the update, which lanes need enrolling again". The deliverable is an impact report per host with its limits stated, each enrolled lane''s acknowledged checkpoint (branch, SHA, pushed or not, uncommitted files, writes in flight), and after the return a reconciliation of projects, lanes and exchanges before anything more is sent. The person does the update or restart. Do not use for an unplanned restart or dropped link already behind us (daintree.hosts.recover-and-clean-up), putting a whole run on hold for later (daintree.hosts.park-swarm), stopping or changing a run (daintree.hosts.steer-swarm), a read of the hosts with no restart ahead (daintree.hosts.survey-hosts), adding a machine as a host (daintree.hosts.connect-host), or Daintree itself misbehaving on this machine (daintree.diagnostics).'
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
The person updates or restarts the host in Settings, Hosts or at the machine (no SSH or restart API). Daintree restarting there ends every terminal and agent mid-task, every lane enrollment and the pause, and wipes message and reply text; exchanges expire after 24 hours. A reboot is that restart; Daintree returns only with start at login. A Shell restart leaves the host working; only the hub's enrollment and pause end.

1. Snapshot per host: `host.list` (`connection`, `agentsObserved` and its time), `host.listAssistants` (`projectsOmitted`: unassessed), `host.listTasks` for `states` submitted, working, input-required, paging, then `host.getTask` each.
2. Impact: per project its lane, `generation`, open exchanges (this Shell's only). Unobserved, as risk: a project with no lane or `generation` null; terminals, dev servers, other Shells' work; counts are observed. The update dialog in Settings, Hosts lists what ends per project, not unpushed work: they read it; "Update when idle" waits.
3. To save work first: no new work to that host; `host.cancelTask` a `submitted` exchange. One `host.sendMessage` to each enrolled lane, even with its exchanges done, naming the run and its panes: "Checkpoint before this host restarts. First let any push, PR, comment or release in flight finish (unsure: blocked); then stop only the agents you launched (daintree.hosts.steer-for-hub), closing nothing. Per agent: worktree, branch, HEAD, pushed or not, uncommitted files, writes in flight." Add "commit and push to <run branch>" only if allowed. End the turn.

On notice or resume: acknowledged only if every agent is stopped or done and its writes settled; else unacknowledged. When they say it is back: `host.list`, `host.listAssistants` against the snapshot: `projectId`s match; `generation` null, not enrolled for this Shell; changed, a fresh enrollment (foundation step 2). `host.listTasks` from cursor 0, `host.getTask` each: a pre-restart exchange lost its reply text; never resend it for a new generation. Build: they check it in Settings, Hosts; no tool shows it. New work only to an enrolled lane; before briefing a unit afresh, ask its lane what survived (daintree.hosts.recover-and-clean-up).

Report back: per host and project, what ends and what is unobserved; each lane's checkpoint (branch, SHA, pushed) or unacknowledged; after the return, lanes re-enrolled or not, exchanges as recorded, what waits.
