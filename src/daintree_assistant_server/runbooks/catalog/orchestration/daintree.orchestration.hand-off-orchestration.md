---
id: daintree.orchestration.hand-off-orchestration
title: Hand orchestration over to a successor coordinator
summary: Pass coordination of a whole multi-agent operation to a fresh coordinator session — freeze new work, write the handoff (children, dependencies, pending questions, decisions, evidence, next actions, never-replay list), launch the successor to reconcile it against live state, and give the user the pane hand-over steps.
whenToUse: 'Use when the COORDINATOR itself is being replaced while a multi-agent operation is still in flight — "your context is nearly full, hand coordination of these six agents to a fresh session", "I''m switching to Codex as the coordinator, give it everything it needs", "write the handover so another session can take over this migration run", "pass the orchestration to a new Claude". The deliverable is a successor coordinator running from a handoff document (each child by terminal id, title, worktree and branch; dependencies; pending questions; approved decisions; verification evidence; next actions; what must never be replayed), told to reconcile it with live Daintree state before acting, the user told which panes to hand over, and this session arming no new work. Do not use to move ONE worker''s task to a different agent (use daintree.agents.hand-off-task), to pick up a run yourself in a new session from its checkpoints (use daintree.orchestration.status-and-resume), to stop everything for later (use daintree.orchestration.park-work), to plan and checkpoint a dependent multi-step goal (use daintree.orchestration.execute-durable-workflow), or for a run on the Remote Hosts changing coordinator (use daintree.hosts.hand-off-swarm).'
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
---
The successor continues, never restarts. Live Daintree state outranks the handoff, which is a snapshot.

Daintree hands over a terminal only once the pane that launched or holds it closes (/exit alone leaves its shell holding it); worktree ownership never transfers, and notices this session armed die with it.

On a host: shell steps run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Freeze: no launches or task sends but the step 4 successor; answer only a child's in-scope question already pending. Tell the user.
2. Live state: `terminal.list` (`owned:true`), `worktree.list`, one `terminal.getStatus` with `includeOutput:{"lines":30}` over the children; per worktree `git -C <path> rev-parse --abbrev-ref HEAD`, `rev-parse --short HEAD`, `status --short`. PRs: `gh pr view <number> --json state,headRefOid,url`, else `gh pr list --head <branch> --state all`.
3. Write the handoff outside the repository, by absolute path: in your checkpoint folder when `$DAINTREE_ASSISTANT_RUN_CHECKPOINT_DIR` is set (survives restarts), else a path the user names or `${TMPDIR:-/tmp}`. In the coordinator worktree only if the user agrees, never from the sidebar, listed in `git rev-parse --git-path info/exclude`. With real ids:
   - Goal; the user's authority and limits as given (what may be pushed, merged, closed); approved decisions and why.
   - Per child: terminal id, title, agent, worktree id and path, branch, HEAD, task and acceptance, state now, what it waits on.
   - Dependencies: what starts when each finishes.
   - Pending questions (whose, verbatim) and decisions awaiting the user.
   - Evidence you verified (command and result), kept apart from children's claims.
   - Next actions, in order.
   - Never replay: launches made, messages delivered, pushes, PRs, comments, merges, deletions.
   - The run checkpoint's path, if any; the successor then writes in its own folder.
4. Successor: the agent the user named, else a fresh session of your kind. Launch once in the coordinator's worktree with this prompt as written, only the path filled (across machines, the handoff's and checkpoint's content instead):

```json tool=agent.launch
{"agentId":"codex","name":"Coordinator (successor)","worktreeId":"<coordinator worktree id>","prompt":"You are taking over as coordinator of a multi-agent run in Daintree. Read <handoff path>. Before any action, reconcile it with live state: terminal.list, terminal.getStatus over every child, git in each worktree. Launch, send and arm nothing yet. Reply in prose with your reconciled picture and every difference from the handoff. On the user's go: re-read terminal.list owned:true and terminal.getStatus; a child not in your owned list was not handed over, so tell the user and send it nothing; verify any child that finished; arm terminal.notifyWhenIdle on each owned working child; continue from the next actions. Never relaunch a listed child or replay a listed write; a stopped child is a result to verify, not to restart.","waitForReply":true,"handback":true,"waitSeconds":600}
```

   A successor the user already opened: give the user that prompt to paste there. The Global Assistant (never launched): the same, adding: every tool through `host.call`, `hostId` `local`, this `projectId`; close or delete nothing, as a hand-over grants neither.
5. Check its reply against step 2: every child, the same next actions, each difference a live change. A wrong picture gets one correction (`terminal.sendCommandOwned`, `waitForReply:true, handback:true`; to the Global Assistant, via the user).
6. The user hands over, in order: close this coordinator's pane (closing nothing else); on each child's panel, by title, context menu, Hand to orchestrator, the successor's pane or Global Assistant, Hand over terminal; then "go" to the successor. Refusals: "already drives it" or "Already handed" to this coordinator: close its pane first; handed elsewhere: take it back; "isn't running": gone (the Global Assistant: reopen it). The successor is listed only while connected, the Global Assistant only while running, for this window's project. Worktrees this session created stay.

On notice or resume: a child's notice before you exit is a live change, not work to advance: append its outcome to the handoff and, once the successor runs, tell it in one `terminal.sendCommandOwned`; dispatch nothing from it.

Stop and recovery: one successor launch; a lost reply is reconciled by its title in `terminal.list`. One that cannot start (`missing-cli`, `launched:false`): give the user the handoff path and the prompt. Between your pane closing and the successor's go, children run unwatched.

Report back: the handoff path; the successor and its terminal id; its reconciled picture and differences; the children by title and id; the user's hand-over steps; that this session does nothing more.
