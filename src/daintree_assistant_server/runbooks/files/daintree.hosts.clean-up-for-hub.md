---
id: daintree.hosts.clean-up-for-hub
title: Clean up a run on this host for the hub
summary: On a Remote Host's assistant enrolled as a lane, remove exactly the panes and worktrees this lane made for the run the hub names, never forced and never on the shared remote, and answer the hub with each item removed or kept and why, anything needing a decision asked back as one list.
whenToUse: 'Use on a Remote Host''s Daintree assistant enrolled as a lane when the hub, the assistant on the person''s main machine, asks it to remove what it made on this machine for one of the hub''s runs: "the hub wants the panes from the race closed", "remove the worktrees you made for the parser job and tell the hub what you kept", "the hub asked me to tidy up my part of its experiment". The deliverable is that run''s panes closed and its clean, pushed or abandoned worktrees removed here, nothing else touched, and one reply to the hub: completed with each item removed or kept and why, or one input-required list of what would be lost. Do not use on the hub itself (daintree.hosts.recover-and-clean-up), for the person tearing down panes and worktrees on their own machine (daintree.orchestration.workspace-cleanup), or for a read-only verdict on which worktrees could go (daintree.worktree.audit-worktrees).'
risk: system
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - terminal.closeMany
  - worktree.deleteOwned
  - worktree.delete
  - assistant.reply
optionalTools:
  - assistant.getInbox
---
Follow daintree.hosts.handle-hub-message first (read, fit, reply). Then remove only what this lane made for the run the hub names.

1. The set: items named for that run by the hub's text or your own record (this conversation, hand-offs in `assistant.getInbox` `view:"sent"`), matched exactly by id, name or branch in `terminal.list` (also `location:"background"`) and `worktree.list`. Nothing merely similar or stale; never the main worktree. Not listed: already gone. No record (a restart): ask the hub for the items your replies named.
2. Panes: one `terminal.getStatus` over them. An agent `working`, or `waiting` with no `lastHandback`, stays for the list. Close the rest in one `terminal.closeMany`, but any not in `terminal.list` `owned:true` puts the whole call behind one confirmation: those go in a second call.

```json tool=terminal.closeMany surface=lane-full
{"terminalIds":["<pane id>","<pane id>"]}
```

3. A worktree goes only if no pane is open in it, `git -C <path> status --porcelain` is empty, `git -C <path> ls-remote origin <branch>` shows its `rev-parse HEAD` (or the text calls it abandoned), and its teardown reaches nothing off this machine (no `resource`, `resources` or remote `teardown` in `.daintree/config.json`, worktree then project). `worktree.deleteOwned` if this session's `worktree.createWithRecipe` made it, else `worktree.delete` with only `worktreeId`. Never `force`; a branch only if the text asks (`git branch -d`, main checkout, after). Proof: gone from `worktree.list`; read a lost result there, never resend.
4. Unwatched, a confirmation ends in `CONFIRMATION_TIMEOUT` after about 28 s (no window: `CONFIRMATION_REQUIRED`) unless this host's "Daintree confirmations" is Never ask. The item stands, never routed around (`git worktree remove`); nobody is watching, so no further gated call this round.
5. Reply: one `input-required` listing what you removed and, per item, exactly what would go (unpushed commits, a dirty tree, an agent mid-task, a teardown off this machine, a branch on the shared remote, which a host never deletes: the person decides through the hub). Nothing to decide: `completed`, each item removed or kept, and why.

On the hub's answer: act within it and these rules; a timed-out item is tried again only if it says someone now watches this window or confirmations are off.

Report back: per item, removed or kept and why, with ids.
