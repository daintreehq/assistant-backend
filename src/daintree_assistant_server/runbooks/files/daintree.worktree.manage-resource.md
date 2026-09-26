---
id: daintree.worktree.manage-resource
title: Manage a worktree remote resource
summary: Inspect and interactively provision, pause or resume a worktree's configured remote resource, verifying with fresh status and refusing unsafe unattended or unpreviewed lifecycle actions.
whenToUse: Use when the user asks about a worktree's cloud devbox or remote resource status, wants to provision, pause or resume it, asks to save resource cost, or asks for a future pause/resume. Also use for teardown or connect requests so the assistant can route those unsupported-safe paths honestly. Do not use for creating or deleting an ordinary git worktree (use daintree.worktree.create-plain-worktree or daintree.orchestration.workspace-cleanup), Daintree's own CPU/memory resource profile, or starting a local dev server.
risk: system
requiredTools:
  - worktree.list
  - worktree.resource.status
optionalTools:
  - worktree.resource.provision
  - worktree.resource.pause
  - worktree.resource.resume
  - worktree.resource.teardown
  - actions.getSchema
---
Pane-only at core, each call asks the user first; the sidebar at core and API-key clients lack these tools: point the user to the worktree card. Every call runs the project's configured commands for real, with real cost. Daintree schedules nothing: a pause "at 6pm" needs a verified host mechanism (daintree.orchestration.watch-outcome), and still stops at Daintree's approval.

1. Resolve ONE exact `worktreeId` from `worktree.list` and always pass it (omitted, these fall back to the focused worktree). Several match: ask.
2. Know what runs: the `resource` block (or a named `resources` entry) of the first valid config among `~/.daintree/projects/…`, the worktree's `.daintree/config.json`, the main checkout's. Tell the user the commands. Repository commands run only once approved on the worktree card; "haven't been approved" is theirs to resolve there, never a reason to copy commands into the user-level config.
3. `worktree.resource.status` first (it runs the status command too). `configured:false` means verification is unavailable, not that nothing exists. An `error` or unknown `lastStatus` is not healthy. Report `lastStatus`, `error`, `lastCheckedAt`; repeat `lastOutput`, `meta` or `endpoint` only when plainly safe (they can carry credentials).
4. Provision, pause or resume only on explicit request, one call, exact id. Provision and resume may cost money; pause is not guaranteed reversible until you read the commands.
5. A timeout, lost reply or error may have changed the resource partway: re-read status and report it, without replay, before any identical call. A lifecycle reply is an acknowledgment; verify with a fresh status read, or say verification is unavailable.
6. Refused as disabled or "no … command configured" when you know it exists: availability follows the context worktree, not your id (`actions.getSchema`: `callable:false`, `DISABLED`). Do not retarget; send the user to that worktree's card.
7. Teardown: prefer the card's Teardown Resource dialog. Call it here only on explicit request, after showing the exact teardown commands and offering pause; unsure of the commands, leave it to the dialog. A declined confirmation is final. Deleting the worktree is daintree.orchestration.workspace-cleanup.
8. Connect has no action: send the user to the card's connect control; never build an SSH command from metadata.

Report back: worktree id and branch, the action, the commands run or that would run, status before, acknowledgment, status after or why unverifiable; for teardown or connect, the card path if not done here.
