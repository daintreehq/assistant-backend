---
id: daintree.orchestration.status-and-resume
title: Status sweep, ledger hygiene and session resume
summary: Reconstruct what is in flight across turns and sessions from run checkpoints, live Daintree state and closed-session history — then correct the stale checkpoint entries nothing else will.
whenToUse: Use when the user asks where were we, what was I working on, catch me up, what is still open or in flight, what happened while I was away, or to pick up / resume something from an earlier session. Also for the workflow list or saved run checkpoint itself — why it still shows finished work as active, tidying stale entries, recording a PR onto an entry — and for after-the-fact forensics over Daintree's recent notifications. Do not use for a plain what-exists-now read of terminals, worktrees or queues (daintree.foundation), to triage the agents running this minute (daintree.orchestration.supervise-fleet), to be told later when something finishes (daintree.orchestration.watch-outcome), to verify one agent's output (daintree.orchestration.verify-agent-work), to close terminals or delete worktrees (daintree.orchestration.workspace-cleanup), or to start an issue (daintree.workflow.start-work-on-issue).
risk: project
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
optionalTools:
  - agentSessionHistory.list
  - agentSessionHistory.resume
  - notifications.recent
---
Use when: the user wants the cross-turn or cross-session picture (where were we, what is still open, what happened while I was away), wants a closed agent session back, or a saved run checkpoint needs reconciling with reality.

Availability: Core for live reads. `agentSessionHistory.list`, `agentSessionHistory.resume` and `notifications.recent` raise an approval in a core pane. Branch and PR state come from your host's `git` and `gh`. The app keeps no workflow ledger or record of past MCP calls: the record is your run checkpoints (in `$DAINTREE_ASSISTANT_SCRATCH_DIR` or `.daintree/runbook-runs/`), live Daintree state, the closed-session journal, and git and the forge. This is a read-and-reconcile sweep; continuing work happens under the runbook that started it.

Procedure:
1. Find the record: open checkpoints in the coordinator's checkpoint location, and whatever your own host can verifiably show of the earlier session. With none, say so, rebuild from live evidence, label earlier decisions unknown, and replay no write from memory.
2. Read live state in one pass: `terminal.list`; `worktree.list` for branches and its `issueNumber`, `prNumber` and `setupState` hints; one `terminal.getStatus` over the recorded terminal ids plus listed agents, with `includeOutput`. Branch and PR state from `git -C <worktree path>` and `gh pr list --repo <owner/repo> --head <branch>`. Offer a focused CI read (daintree.workflow.follow-up-on-issue) rather than one per unit.
3. Reconcile each recorded unit on evidence, not on its recorded status:
   - Present and working: live. A checkpoint shows a notice was requested, not that it is still pending: it may have fired, been cleared by the user, or died with a relaunched pane or restarted app. Say whether anything will wake you, and if not, that continuing needs a fresh notice or the user.
   - Present and stopped: a candidate result, checked against the recorded acceptance.
   - Absent from a successful listing: gone; how it ended comes from the branch, commits and PR, or stays unknown. A failed or partial listing is unknown, not gone.
   - A recorded public-write intent with no confirmed result: uncertain until read back from the forge or repository.
   Children may have progressed while the coordinator was away, even though nothing of yours advanced. Correct the checkpoint only on evidence (`done` needs verified acceptance, `failed` a confirmed failure, `cancelled` the user's word), atomically and as the only writer.
4. Ownership after an interruption. A relaunched pane, a new session and an app restart own nothing the old connection created; `terminal.list` with `owned:true` shows what this one holds. Status reads and notices work on any agent terminal in the project. Sending needs the user to hand the terminal over in Daintree; closing and worktree deletion are never handed over. Report which resources are no longer yours.
5. Closed agent sessions, newest first, paging with `offset` while `hasMore`:

```json tool=agentSessionHistory.list
{"worktreeId":"<exact worktree id>","limit":20,"offset":0}
```

   The journal holds no transcript text and prunes old entries, so absence proves nothing. Resume one only when the user asks:

```json tool=agentSessionHistory.resume
{"worktreeId":"<worktree the session was recorded in>","sessionId":"<exact sessionId from the listing>"}
```

   It relaunches that agent on its saved conversation in its recorded worktree (a second call brings the same pane forward), restoring nothing of yours: not your checkpoint, notices or ownership. Say so.
6. "What happened while I was away": `notifications.recent` lists the completion, waiting and error messages Daintree raised, to combine with the reconciled units.

Continuing an already authorized plan needs these fresh target and ownership checks, not a new generic approval.

Report back: one ranked picture with real ids: (a) needs the user now (questions, approvals, errors, uncertain writes); (b) live, and whether anything will wake you; (c) stopped, awaiting verification; (d) done on evidence, gone, or unknown; (e) resumable sessions. Then what you corrected in which checkpoint and on what evidence, which resources this session no longer owns, and what continuing needs.
