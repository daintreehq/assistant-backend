---
id: daintree.hosts.audit-host-workspaces
title: Audit what a run left on the Remote Hosts, read-only
summary: From this Shell's hub, have each Remote Host's assistant inventory the worktrees, panes, branches and dev servers a named run left in its own project, and report each item safe, unsafe or unknown to clean up with the evidence, the coverage gaps and the other owners left out, removing nothing.
whenToUse: 'Use on the hub when the person wants the worktrees, panes, branches and dev servers that finished swarm work left on their Remote Hosts INSPECTED, read-only, to see which are in use, hold unsaved work or could be let go: "audit the workspaces the swarm left across my hosts", "which remote worktrees have unpushed work or are still in use?", "read-only cross-host inventory by run and owner". The deliverable is a report per host and project: each item the run made classed safe, unsafe or unknown with its dirty files, unpushed commits, merged evidence and listener owner, hosts or projects not observed as explicit gaps, other owners'' items counted and left alone, and nothing removed, closed or stopped. Do not use for performing a named run''s cleanup (daintree.hosts.recover-and-clean-up), for a host''s own assistant removing what it made for the hub (daintree.hosts.clean-up-for-hub), or its answering the hub''s audit request (daintree.hosts.handle-hub-message), for worktrees on this machine (daintree.worktree.audit-worktrees; tearing them down, daintree.orchestration.workspace-cleanup), or for why one host differs from another (daintree.hosts.diagnose-host-differences).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
---
Read-only here and on every host: nothing is closed, removed, stopped or pushed.

1. The run's items per host and project, from the replies and plan you kept: the worktree, branch, pane and port it named. Match only by those; anything that merely resembles one is not the run's: counted, unjudged. No record (a restart drops reply text): ask that lane which it made; unproven is unknown.
2. `host.list`; `host.listAssistants` per host. A lane with a `generation` and `available` is audited; a `disconnected` host, a project with no lane, `projectsOmitted` above 0, no generation, not `available` or not `ok` is a coverage gap, never "nothing there".
3. One `host.sendMessage` per audited lane, no `waitMs`, for its own project only; end the turn.

```json tool=host.sendMessage surface=hub-full
{"hostId":"<hostId>","projectId":"<projectId>","slot":0,"generation":"<generation>","text":"<audit brief>"}
```

   Brief: "Report only; change nothing. Run <run>. Follow daintree.worktree.audit-worktrees, but no `fetch` (what it would settle is unknown), for these items in this project: <ids, branches, panes, ports>. Per item add: a listener on a named port or with its cwd in it (`lsof -nP -iTCP -sTCP:LISTEN`, then `lsof -a -p <pid> -d cwd`); a configured remote resource, noted and never probed; HEAD on the remote (`ls-remote`). List others, unjudged. Never close, kill, remove or push. Reply each item's facts and any failed read."

On notice or resume (the hosts foundation's reconcile):
- `completed`: check what is cheap: a pushed claim by `git ls-remote` on the shared remote; else one narrow question, same `contextId`. Unchecked stays a claim.
- Per item, apart: in use (working agent, open pane, listener), dirty or untracked, unpushed, merged (a PR head holding HEAD; git alone misses squashes), listener owner, retained service or cloud resource. Safe: clean, idle, HEAD in a merged PR, every read ok. Unsafe: a flag, named. Unknown: pushed but unmerged, failed read, unobserved lane, owner unproved, any retained resource. A closed task is not an abandoned worktree.

Stop and recovery: one audit per lane; an unsent, lost or empty one is a gap. Re-check before any cleanup the person authorises (daintree.hosts.recover-and-clean-up);

Report back: per host and project, each item with class, reasons and the decision it needs; coverage gaps; other owners counted; what you checked against what lanes said.
