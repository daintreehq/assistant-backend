---
id: daintree.hosts.start-swarm
title: Set up a swarm with an assistant ready on each remote host
summary: Before any work goes out, get one enrolled assistant per Remote Host in the right project, reusing the one already enrolled for this Shell and starting one only where the person allows it, and report host, project, slot, agent and generation, or the exact setting that needs the person.
whenToUse: 'Use when the user wants a swarm SET UP across their other machines before handing out work: Claude Code or Codex ready on every Remote Host, other Mac or server (or the ones named) in this project or repository, bootstrapping each host''s assistant lane by reusing the one already enrolled and starting the missing ones, or asks which machines can take work right now. The deliverable is a ready table (host, project, slot, agent, generation) plus what needs the person, with no work sent. Do not use for a worktree''s cloud devbox or remote resource (daintree.worktree.manage-resource), per-worktree databases, ports or setup scripts on this machine (daintree.worktree.provision-local-environment), launching parallel agents on this machine (daintree.orchestration.fan-out-work), sweeping agents already running here (daintree.orchestration.supervise-fleet), or the work itself sent to every host once they are ready (daintree.hosts.swarm-fan-out).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.startAssistant
---
Readies one lane per host and sends no work. Start only what the person asked to have ready; a "who can take work?" question gets the table and an offer.

1. `host.list`; keep the hosts the person named, else all. Not `connected`: report, start nothing.
2. `host.listAssistants` for each, in one parallel round. Not `ok`, or no project names: unknown, not absent; start nothing there. Find the project in each host's own listing; another host's or this Shell's name, path or projectId proves nothing. A match the person named is usable; one found only from this project's name needs their yes before any start. None (see `projectsOmitted`) or several: ask.
3. Per host:
   - a lane with a `generation`, `available:true` and the CLI asked for (if any): ready, no call;
   - a lane with a `generation` but `available:false`, or on another CLI than asked: report it, never start beside it unasked;
   - otherwise one `host.startAssistant`, `agentId` as asked, else the project's assistants' CLI, else omitted. `startNew` only for more than one per project.

```json tool=host.startAssistant surface=hub-full
{"hostId":"<host id>","projectId":"<that host's projectId>","agentId":"codex"}
```

4. `ok`: keep `slot`, `agentId`, `generation`, `started` (`available:false`: enrolled, not yet taking messages). Else quote `detail`; nothing more on that host unless:
   - `opted_out` "Starting assistants is turned off": the person turns on "Let the assistant start assistants on this host" (Settings, Hosts, that host).
   - "No window here shows this host": the person opens the host in a window here.
   - "different agent CLI" with none asked: re-list, then step 3 with theirs.
   - "already runs 3 assistants": the limit; never end one to make room.
   - "didn't reach its prompt", "while this ran", "couldn't be enrolled", `disconnected`, `ambiguous`, no reply or a tool error: uncertain. Re-list (once reachable): a new lane with a `generation` is yours; an assistant without one in a slot free before is this start's, unenrolled (maybe on a question in its window); nothing new may still be starting: list again in a minute. A repeat can open another: only on the person's word.

Report back, per host: project (its own name and path), slot, agent, generation, reused or started, ready or the blocker and the exact setting; omitted projects; unavailable or other assistants seen. Then stop; sending work is another runbook's.
