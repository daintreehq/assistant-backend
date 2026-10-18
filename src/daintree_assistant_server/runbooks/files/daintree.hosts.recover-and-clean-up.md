---
id: daintree.hosts.recover-and-clean-up
title: Recover and clean up a run across Remote Hosts
summary: From this Shell, rebuild where the work sent to each Remote Host stands after a restart, a new session or a dropped link, from what each host shows now rather than waiting for notices, and clean up after a named run by removing what it made there.
whenToUse: 'Use when the user wants to know what happened on their other machines while they were away, where things stand with the work handed to another machine, or why a task sent to a Remote Host never came back, usually after Daintree restarted, the assistant session was replaced or the link to a Remote Host dropped: which delegated pieces finished, which are still running, which will never finish, and whether to send one again. Also when they want what a run left on the Remote Hosts, its panes (usage-check panes included), worktrees and branches, cleaned up on each host. The deliverable is a per-host account of every piece from what that host shows now, and for a cleanup what each host removed and kept, and why. Do not use for agents, terminals or worktrees on this machine: catching up on local work (daintree.orchestration.status-and-resume), tearing down local panes and worktrees (daintree.orchestration.workspace-cleanup), a broken or hung local agent (daintree.agents.recover-agent) or triaging the local fleet (daintree.orchestration.supervise-fleet); nor for Daintree itself misbehaving on this machine (daintree.diagnostics).'
risk: system
requiredTools:
  - host.list
  - host.call
hostEligibility: configured
---
The Shell keeps no record of what was sent: the account is what you kept, checked against what each host shows now. An earlier session's notices never reach a later one. What an earlier session launched is readable but no longer yours to send to or close (the hosts foundation's ownership): the person, or that host's assistant, closes it.

Procedure:
1. References, per unit: host, project, terminal id and title, worktree, branch, brief, last SHA or PR, from this conversation, a checkpoint, or a park or hand-over record the person names. None: say so and ask which run; match only what the person describes. Never infer a brief or acceptance from a pane title or branch name, and never rebuild a task list from the hosts.
2. `host.list`: per host `connection`, `lastSeenAt`, `control`. Not connected: its units are unknown, not lost; give the last-seen time and move on.
3. Per connected host and project, through `host.call`: `terminal.list` (no `owned` filter: `owned:true` lists only this session's), `worktree.list`, then one `terminal.getStatus` over the run's terminals with `includeOutput:{"lines":40}`; `terminal.readLastMessageOwned` only for an agent this session launched. From your shell, per branch: `git ls-remote origin <branch>`, `gh pr list --head <branch> --state all`.
4. One state per unit:
   - Finished: stopped with its handback for that brief, and its branch at a SHA on the remote; verified only as the foundation's proving a finish says.
   - Still working: leave it; following it to its end is daintree.hosts.supervise-swarm.
   - Waiting on a question, approval or error on its screen: to the person, verbatim.
   - Will not finish: exited, gone, or its host restarted, with nothing pushed. What is left is its worktree and any unpushed edits there: say where.
   - Unmatched: a run-looking terminal or worktree no reference names: listed apart, never claimed.
   Resend only on the person's word, as new work (daintree.hosts.delegate-to-host; on another host, daintree.hosts.reassign-host-work), briefed in full by shared reference, once nothing still writes that unit.
5. Cleanup: only what the request names (panes only means panes), for a named run, never a unit still working (stopping it first is daintree.hosts.steer-swarm). Per host and project:
   - Panes this session launched: one `terminal.closeMany` through `host.call`.

```json tool=host.call
{"hostId":"<hostId>","projectId":"<projectId>","tool":"terminal.closeMany","args":{"terminalIds":["<terminalId>","<terminalId>"]}}
```

   - A worktree this session created, once its panes are closed: `worktree.deleteOwned`. Its confirmation comes to the person here; it refuses uncommitted or untracked work and keeps the branch, so commits not on the remote survive there: say which.
   - Branches, PRs and anything on the remote: listed for the person, never removed here.
   - Anything else (an earlier session's, the person's, the host's own) is refused `RESOURCE_NOT_OWNED`: list it for the person to close on that host or, on their word, brief that host's assistant (the hosts foundation's Delegating): exactly those items, close only them, delete a worktree only when clean and its panes are closed, never force or delete a branch, and reply each item removed or kept, and why. End the turn.

On notice or resume: its answer is a claim: re-read `terminal.list` and `worktree.list` there and confirm each item is gone or kept as it says. An item it could not remove stays, with its reason, for the person.

Stop and recovery: one cleanup brief per host; never a second while one is open. A refused or timed-out item stays standing and reported; retry only on the person's word. Nothing here is replayed or resent unasked.

Report back: per host: finished (verified or claimed), still working, waiting on the person, will not finish, unmatched, unreachable since when; for a cleanup, what was removed and kept, and why, and what the person still has to close.
