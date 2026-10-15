---
id: daintree.hosts.clean-up-for-hub
title: Clean up a run on this host for the hub
summary: On a Remote Host's assistant enrolled as a lane, remove exactly the panes and worktrees this lane made for the run the hub names, never forced and never on the shared remote, and answer the hub with each item removed or kept and why, anything needing a decision asked back as one list.
whenToUse: 'Use on a Remote Host''s Daintree assistant enrolled as a lane when the hub, the assistant on the person''s main machine, asks it to remove what it made on this machine for one of the hub''s runs: "the hub wants the panes from the race closed", "remove the worktrees you made for the parser job and tell the hub what you kept", "the hub asked me to tidy up my part of its experiment". The deliverable is that run''s panes closed and its clean, pushed or abandoned worktrees removed here, nothing else touched, and one reply to the hub: completed with each item removed or kept and why, or one input-required list of what would be lost. Do not use on the hub itself (daintree.hosts.recover-and-clean-up), for the person tearing down panes and worktrees on their own machine (daintree.orchestration.workspace-cleanup), for a read-only verdict on which worktrees could go (daintree.worktree.audit-worktrees), or for stopping, pausing or correcting an agent of that run that is still at work (daintree.hosts.steer-for-hub).'
risk: system
requiredTools:
  - terminal.list
  - terminal.getStatus
  - worktree.list
  - terminal.closeMany
  - worktree.deleteOwned
  - worktree.delete
  - assistant.reply
---
Follow daintree.hosts.handle-hub-message first (read, fit, reply); then remove only what this lane made for the run the hub names.

1. The set: items named for that run by the hub's text or this conversation, only of the kinds it asks for (panes only means panes), matched exactly by id, name or branch in `terminal.list` (also `location:"background"`) and `worktree.list`. Nothing merely similar or stale; never the main worktree. Not listed: already gone. No record (a restart): ask the hub for the items your replies named.
2. Panes: one `terminal.getStatus` (`includeOutput`) over them. An agent `working`, or `waiting` with no `lastHandback` after its `lastTypedInputAt`, stays for the list. Close the rest in one `terminal.closeMany`, but any not in `terminal.list` `owned:true` puts the whole call behind one confirmation: those go in a second call.

```json tool=terminal.closeMany surface=lane-full
{"terminalIds":["<pane id>","<pane id>"]}
```

3. A worktree goes only if no pane is open in it, `git -C <path> status --porcelain` is empty, `git -C <path> ls-remote origin <branch>` shows its `rev-parse HEAD` (or the text calls it abandoned), and its teardown reaches nothing off this machine: no `resource`, `resources`, `resourceEnvironments` or remote `teardown` in the `config.json` or `settings.json` under `~/.daintree/projects/<project root, / as _>/` or `.daintree/` (worktree, project); unsure is remote. `worktree.deleteOwned` if this session's `worktree.createWithRecipe` made it, else `worktree.delete` with only `worktreeId`. Never `force`; a branch only if the text asks (`git branch -d` after). Proof: gone from `worktree.list`, a lost result too; never resend.
4. Unwatched, a confirmation ends in `CONFIRMATION_TIMEOUT` after about 28 s (no window: `CONFIRMATION_REQUIRED`) unless this host skips them ("Daintree confirmations" Never ask, or following a global Skip permission prompts). The item stands (no `git worktree remove`); no further gated call this round.
5. Reply: one `input-required` listing what you removed and, per item, exactly what would go (unpushed commits, a dirty tree, an agent mid-task, a teardown off this machine, a branch on the shared remote, which a host never deletes: the person's, via the hub). Nothing to decide: `completed`, each item removed or kept, and why.

On the hub's answer: act within it and these rules.

Report back: per item, removed or kept, why, ids.
