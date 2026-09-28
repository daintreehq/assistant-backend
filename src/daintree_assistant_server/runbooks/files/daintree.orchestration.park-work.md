---
id: daintree.orchestration.park-work
title: Park work at a clean, resumable stop
summary: Wrap up for the day or shelve work without tearing anything down — bring running agents to a clean stopping point, record where each stopped, checkpoint uncommitted work only as the user allows, and leave every panel, session and worktree standing with a resume note.
whenToUse: 'Use when the user is walking away and wants in-flight work paused and made safe, not torn down — "wrap up for the day", "get everyone to a stopping point and tell me where each is", "pause the agents but keep their sessions", "park this worktree, I''ll come back next week", "save everything before I shut my laptop", "leave notes so tomorrow''s agent can pick it up". The deliverable is, per agent and worktree, a clean stop, what is uncommitted or checkpointed, and a resume note, with nothing closed. Do not use to close terminals or delete worktrees (use daintree.orchestration.workspace-cleanup), to ready a branch or open a real PR (use daintree.workflow.prep-branch-for-review), to pick parked work back up (use daintree.orchestration.status-and-resume), to change one running agent''s course or stop only its turn (use daintree.agents.steer-running-agent), to move a task to a different agent (use daintree.agents.hand-off-task), to be told when running agents finish (use daintree.orchestration.watch-outcome), or for a sweep of what needs you now (use daintree.orchestration.supervise-fleet).'
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandMany
optionalTools:
  - terminal.interruptOwned
  - terminal.sendCommandOwned
  - terminal.setClientMetadata
---
Parking makes work safe and resumable; it closes nothing. Closing the app or a pane ends its running agents and every pending notice. The agents save their own work; you choose the set, send, check and record.

Procedure:
1. Scope: "everyone" is every agent you launched or were handed; a named worktree is only its agents (`terminal.list` with `worktreeId`). Everything after applies to that set. Agents you neither launched nor were handed are listed for the user, never sent to.
2. Inventory: `worktree.list`, `terminal.list` (`owned:true`), one `terminal.getStatus` with `includeOutput:{"lines":40}` over the set. Per worktree: `git -C <path> status --short` and `git -C <path> log --oneline @{u}..`. No upstream means no tracking branch, not "never pushed": remote state is unknown until `git ls-remote <remote> <branch>` says.
3. Permissions, each separate, from what the user already said: a WIP commit; a push (to which remote and branch); a draft PR. None given: no commits.
4. Send by the user's words, `terminal.sendCommandMany` in batches of at most 16, `notify:true, handback:true`. Record each accepted item and each refused one; never replay a batch.
   - "wrap up", "stopping point": the send is the agent's NEXT prompt, read when its current turn ends; do not promise a sooner stop.
   - "stop now": `terminal.interruptOwned` on each working one and read its screen; send only to one confirmed stopped. Unconfirmed: report it, send nothing that could restart work.
   - "leave it running": leave it, and say it stops if the app or its pane closes.
   The message: "We are parking. Start nothing new. Reply in prose: what is done, what is half-done, which files are uncommitted, and the exact next step." Then either "Change no files and make no commits." or, when a commit is allowed and it is the only agent in its worktree: "Then commit as `wip: <summary>`, staging by path; leave out `.env*`, keys, credentials and build output, and ask me about anything secret-shaped. Never `--no-verify`, `git stash`, or a commit on the main checkout's default branch." Add the push only if allowed: "then push to <remote> <branch>, never force."

```json tool=terminal.sendCommandMany
{"sends":[{"terminalId":"<agent terminal id>","command":"We are parking. Start nothing new. Reply in prose: what is done, what is half-done, which files are uncommitted, and the exact next step. Change no files and make no commits."}],"notify":true,"handback":true}
```

5. End your turn, having told the user how long you will wait (the parking deadline). The resume record (step 6) is still owed after the replies: parking is not done without it.

On notice or resume:
- Match each notice to the agent you sent to; before any report, check its account yourself against `git status` and `git log` in its worktree (the reply is the agent's claim).
- Several agents in one worktree: once all are confirmed stopped, send the commit to one of them (same wording), `terminal.sendCommandOwned`, `notify:true, handback:true`.
- A commit: `git -C <path> show --stat HEAD` holds only the approved files; unrelated staged work is left and reported. A push: `git ls-remote` shows the WIP SHA before you call it saved remotely.
- Still working: one reminder that repeats "Change no files". At the deadline, report it as still working or unconfirmed.
- A dirty worktree with no agent: report it; a checkpoint there is an agent's job, only if the user asks.
- A draft PR the user asked for: daintree.workflow.prep-branch-for-review, kept draft and WIP.
6. Resume record where the user wants it, else in the WIP commit message or a file outside the repository, never an untracked file in the tree: task, worktree, branch, HEAD, uncommitted state, decisions, what was verified, the exact next step. A durable workflow's units are marked parked so a late continuation dispatches nothing. Optional panel tag: `terminal.setClientMetadata` with `terminalId` and the whole merged record as `clientMetadata` (read it first with `terminal.list` `includeClientMetadata:true`; keep existing keys).

Stop and recovery: a re-run looks for an existing WIP commit and push before asking for another. A rejected push leaves the commit local: report it, never force. A declined commit leaves the tree dirty and reported, never stashed. Close nothing; a closed session can be resumed later (daintree.orchestration.status-and-resume).

Report back: per worktree and agent, the state (stopped clean, interrupted, left running, unconfirmed, not yours), uncommitted files, WIP commit SHA and the remote ref you verified, draft PR, where the resume record lives; anything not saved and why; what stops if the app closes.
