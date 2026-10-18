---
id: daintree.hosts.audit-host-workspaces
title: Audit what a run left on the Remote Hosts, read-only
summary: From this Shell, inventory the worktrees, panes, branches and dev servers a named run left in each Remote Host's projects, and report each item safe, unsafe or unknown to clean up with the evidence, the coverage gaps and the other owners left out, removing nothing.
whenToUse: 'Use when the person wants the worktrees, panes, branches and dev servers that finished swarm work left on their Remote Hosts INSPECTED, read-only, to see which are in use, hold unsaved work or could be let go: "audit the workspaces the swarm left across my hosts", "which remote worktrees have unpushed work or are still in use?", "read-only cross-host inventory by run and owner". The deliverable is a report per host and project: each item the run made classed safe, unsafe or unknown with its dirty files, unpushed commits, merged evidence and listener owner, hosts or projects not observed as explicit gaps, other owners'' items counted and left alone, and nothing removed, closed or stopped. Do not use for performing a named run''s cleanup (daintree.hosts.recover-and-clean-up), for worktrees on this machine (daintree.worktree.audit-worktrees; tearing them down, daintree.orchestration.workspace-cleanup), or for why one host differs from another (daintree.hosts.diagnose-host-differences).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Read-only on every host: nothing is closed, removed, stopped, fetched there or pushed. Every read below is a `host.call` to that host and project unless it says your shell.

1. The run's items per host and project, from the plan and ids you kept: worktree id, branch, terminal id, port, each with its `hostId` and `projectId`. Match only by those; anything that merely resembles one is another owner's: counted, unjudged. No record of an item: unknown, never the run's.
2. Coverage: `host.list`. A host not `connected` or `control:false`, a project missing from its `projects`, or a call that is not `ok` is a gap, never "nothing there".
3. Inventory per audited project: `worktree.list` (id, path, branch, status); `terminal.list` with no filter and with `location:"background"` (background panels still run); one `terminal.getStatus` over the agent terminals: `working` or `waiting` means in use, whatever git says.
4. Git and listeners per run worktree: the reads of daintree.worktree.audit-worktrees step 2, minus any fetch: status (dirty, untracked, conflicts), an operation in progress, commits on no remote, HEAD; then the listeners: `lsof -nP -iTCP -sTCP:LISTEN`, and `lsof -a -p <pid> -d cwd` for each, to find those inside a run worktree or on a named port. At Tool set Full, `project.runCommand` with `cwd` the worktree path (at most 4 at once per host):

```json tool=host.call surface=help-full
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"git status --porcelain=v2 --branch; git rev-list --count HEAD --not --remotes; git rev-parse HEAD","cwd":"<worktree path>","timeoutMs":60000}}
```

   At Core: one read-only agent per project, `agent.launch` in its main worktree with `handback:true`, `notify:true` and the brief "Report only; change nothing: no fetch, close, kill, remove or push. For <worktree paths>: <the reads above>. Reply each item's facts and any read that failed." End the turn.
5. Merged, from your shell on the shared remote: `git ls-remote --heads origin <branch>` (pushed), `gh pr list --state all --head <branch> --json number,state,headRefOid,mergedAt`, and `git merge-base --is-ancestor <host HEAD> <headRefOid>` in your clone (one `git fetch --quiet origin` there is fine; an error is unknown). Git alone misses squash merges. A remote resource the run configured (a cloud devbox) is noted, never probed.
6. Class per item, every flag named: in use (working or waiting agent, open terminal, listener), dirty or untracked, unpushed, operation in progress, retained service or resource. Safe: clean, idle, no terminal or listener, HEAD inside a merged PR's head, every read ok. Unsafe: any flag. Unknown: pushed but unmerged, a failed read, an unobserved project, an owner unproved, any retained resource. A finished agent is not an abandoned worktree.

On notice or resume: a Core agent's facts are claims; check what is cheap (a pushed claim by `git ls-remote`, a terminal by `terminal.getStatus` there); a missing field gets one narrow `terminal.sendCommandOwned` to that agent. Unchecked stays a claim.

Stop and recovery: one read pass per project, one agent per Core project; a lost or empty read is a gap, never retried into a guess. The audit agents stay open: name them; close on request. Cleanup the person authorises is daintree.hosts.recover-and-clean-up, which reads again first.

Report back: per host and project, each item with class, reasons and the decision it needs; coverage gaps; other owners' items counted; what you read yourself apart from what an agent there claimed; agents left open there.
