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
  - terminal.getOutput
  - terminal.readLastMessageOwned
  - agentSessionHistory.list
  - agentSessionHistory.resume
  - notifications.recent
---
Use when: the user wants the cross-turn or cross-session picture (where were we, what is still open, what happened while I was away), wants a closed agent session back, or a saved run checkpoint needs reconciling with reality.

Availability: Core for live reads. `agentSessionHistory.list`, `agentSessionHistory.resume` and `notifications.recent` raise an approval in a core pane. Branch and PR state come from your host's `git` and `gh` (Hybrid). The app keeps no workflow ledger, launch history or audit log. The record is the run checkpoints under `.daintree/runbook-runs/`, live Daintree state, Daintree's closed-session journal, and git and the forge. This is a read-and-reconcile sweep: continuing a piece of work happens under the runbook that started it.

Procedure:
1. Find the record. Read the run checkpoints in the coordinator checkout (usually the project root) that are not closed. If there are none, say so: rebuild the picture from live evidence only, label earlier decisions unknown, and never replay a write from memory. Your own compacted or resumed context is not a record you can read back; the checkpoint and live reads are.
2. Read live state in one pass: `terminal.list`; `worktree.list` for branches and its `issueNumber`, `prNumber` and `setupState` hints; and one `terminal.getStatus` over the checkpoint's terminal ids plus any listed agents, with `includeOutput`. For branch and PR state use `git -C <worktree path>` and `gh pr list --repo <owner/repo> --head <branch>`. Keep the sweep broad: offer a focused CI or review read (daintree.workflow.follow-up-on-issue) rather than pulling one per unit.
3. Reconcile each checkpoint unit on evidence, never on its recorded status or its age:
   - Terminal present and working: live. Whether anything will wake you is a separate question. Notices belong to the pane that armed them and end with it: after that pane relaunched or Daintree restarted, none is pending; after a compaction in the same pane, the ones your checkpoint records still are.
   - Terminal present and stopped: a candidate result, verified against the recorded acceptance before it becomes done.
   - Terminal absent from a successful listing: gone. How it ended comes from the branch, commits and PR, or stays unknown. A failed or partial listing is unknown, not gone.
   - A public write recorded as intended but not confirmed: uncertain until you read it back from the forge or repository.
   Correct the checkpoint only where evidence supports the change: `done` needs verified acceptance, `failed` a confirmed failure, `cancelled` the user's word. Otherwise leave the status and note the gap. Record confirmed PR numbers and branches onto their units. Write atomically, as the only writer.
4. Ownership after an interruption. A relaunched pane, a new session and an app restart own nothing the old connection created. `terminal.list` with `owned:true` shows what this connection holds. Status reads and notices still work on any agent terminal in the project. Sending or interrupting needs the user to hand the terminal over in Daintree; closing and worktree deletion are never handed over and stay with the user. Report which resources are no longer yours instead of trying them.
5. Closed agent sessions. List them per worktree or project, newest first, paging with `offset` while `hasMore`:

```json tool=agentSessionHistory.list
{"worktreeId":"<exact worktree id>","limit":20,"offset":0}
```

   The journal names sessions but holds no transcript text, and old entries are pruned, so absence does not prove a session never existed. To bring one back, only when the user asks:

```json tool=agentSessionHistory.resume
{"worktreeId":"<worktree the session was recorded in>","sessionId":"<exact sessionId from the listing>"}
```

   It relaunches that agent on its own saved conversation, in the worktree it was recorded in (another worktree is refused); a second call brings the same pane forward rather than starting a second agent. It restores the agent's conversation as its CLI saved it, and nothing of yours: not your checkpoint, your notices or your ownership, since this connection did not launch that pane. Say exactly that. It opens off-screen unless its worktree is active.
6. "What happened while I was away": `notifications.recent` lists the completion, waiting and error messages Daintree raised for the user. Combine it with the reconciled units. No record of past MCP calls exists to read.

Report back: one ranked picture with real ids: (a) needs the user now (questions, approvals, errors, uncertain writes); (b) live, and whether anything will wake you; (c) stopped and awaiting verification; (d) done on evidence, gone, or unknown; (e) resumable sessions, newest first. Then what you corrected in which checkpoint and on what evidence, which resources this session no longer owns, and what continuing needs: the recorded runbook's resume steps and the user's go-ahead.
