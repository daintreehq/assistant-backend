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
The app keeps no workflow ledger or record of past MCP calls. The record is your run checkpoints, live Daintree state, the closed-session journal, and git and the forge. This sweep reads and reconciles; continuing happens under the runbook that started the work.

1. Find the record: open checkpoints and whatever your host verifiably shows of the earlier session. With none, say so, rebuild from live evidence, label earlier decisions unknown, and replay no write from memory.
2. Read live state in one pass: `terminal.list`; `worktree.list` (`issueNumber`, `prNumber`, `setupState`); one `terminal.getStatus` over recorded and listed agents with `includeOutput`; branch and PR from `git -C <path>` and `gh pr list --repo <owner/repo> --head <branch>`. Offer a focused CI read (daintree.workflow.follow-up-on-issue) rather than one per unit.
3. Reconcile each recorded unit on evidence, not its recorded status:
   - Present and working: live. A recorded notice request does not prove it is still pending (it may have fired, been cleared, or died with a relaunched pane or restarted app). Say whether anything will wake you.
   - Present and stopped: a candidate result, checked against the recorded acceptance.
   - Absent from a successful listing: gone; how it ended comes from the branch, commits and PR, or stays unknown. A failed listing is unknown, not gone.
   - A recorded public write with no confirmed result: uncertain until read back.
   Correct the checkpoint only on evidence (`done` needs verified acceptance, `failed` a confirmed failure, `cancelled` the user's word).
4. A relaunched pane, a new session and an app restart own nothing the old connection created; `terminal.list` with `owned:true` shows what this one holds. Reads and notices work on any agent terminal; sending needs a hand-over; closing and worktree deletion are never handed over. Report which resources are no longer yours.
5. Closed sessions: `agentSessionHistory.list` with the exact `worktreeId`, paging with `offset` while `hasMore`. The journal holds no transcript and prunes, so absence proves nothing. Resume only on request with `agentSessionHistory.resume` (`worktreeId`, exact `sessionId`); it restores that agent's conversation, not your checkpoint, notices or ownership. Say so.
6. "What happened while I was away": `notifications.recent`, combined with the reconciled units.

Continuing an authorized plan needs these fresh target and ownership checks, not a new approval.

Report back: one ranked picture with real ids: (a) needs the user now; (b) live, and whether anything will wake you; (c) stopped, awaiting verification; (d) done on evidence, gone, or unknown; (e) resumable sessions. Then checkpoint corrections and their evidence, resources no longer owned, and what continuing needs.
