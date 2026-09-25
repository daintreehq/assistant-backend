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
Use when: the user wants the status of, or lifecycle control over, a worktree's project-configured remote resource, such as a cloud devbox.

Availability: Full-dependent. `worktree.resource.status`, `worktree.resource.provision`, `worktree.resource.pause`, `worktree.resource.resume` and `worktree.resource.teardown` are on an agent pane's surface, but at core each one asks the user first, and teardown also carries Daintree's own confirmation. The sidebar assistant at core and API-key clients are not offered them: say so and point the user to the worktree card. Each call runs the project's configured commands for real, with real cost and effect. Nothing here schedules: a pause "at 6pm" or "when I'm done" cannot be arranged, and an unattended call would stop at the approval anyway, so say so rather than promising it. Reading the configuration is native file work.

Procedure:
1. Resolve ONE exact worktree id from `worktree.list`: the worktree the user named, never a branch name standing in for an id. Always pass `worktreeId`; left out, these actions fall back to the focused or active worktree. If several match, ask.
2. Read fresh status first:

```json tool=worktree.resource.status
{"worktreeId":"<exact worktree id>"}
```

   `configured:false` with `status:null` means no status command is configured: verification is unavailable, which is not proof that no resource exists. A failing command fails the call rather than returning a stale answer. Treat an `error` or an unknown `lastStatus` as not healthy. Report `lastStatus`, `error` and `lastCheckedAt`; repeat `lastOutput`, `meta` or `endpoint` only when needed and plainly safe, because they can carry credentials or tokens.
3. Know what will run before running it. The commands come from the `resource` block, or a named entry under `resources`, of the first valid `config.json` Daintree finds: the user-level project config under `~/.daintree/projects/`, else the worktree's `.daintree/config.json`, else the main checkout's; with none there, a project settings file can supply resource environments. Read the one that applies when you can, and tell the user which commands will run. Repository commands run only once the user has approved them on the worktree card: an error saying they "haven't been approved" is theirs to resolve there, never yours to work around.
4. Provision, pause or resume only on the user's explicit request, one call, exact id:

```json tool=worktree.resource.provision
{"worktreeId":"<exact worktree id>"}
```

   Pause and resume take the same argument. Provision creates real infrastructure and may incur cost; it does nothing when the resource already reports running, and routes a paused one through the resume commands. Resume may incur cost from the moment it returns. Pause is not guaranteed to be undone by resume: read the commands before calling it reversible.
5. Handle ambiguity without replay. A timeout or a lost reply does not mean the command did nothing: read status again before considering an identical call. A lifecycle call returns an acknowledgment, not the resource's new state.
6. Verify with a fresh `worktree.resource.status` after a successful call when a status command exists, and report the new `lastStatus`, `error` and `lastCheckedAt`. Pause and resume record their own timestamps, but only a status read shows the external result. Without a status command, say verification is unavailable instead of reporting the resource ready or paused.
7. A refusal as disabled or "no … command configured" for a worktree you know has that command: the availability check follows the context worktree (the user's focused or active one, or your pane's launch context), not the id you passed. `actions.getSchema` shows it as `callable:false` with `unavailableReason:"DISABLED"`. Do not retarget through another path; point the user to that worktree's card, where the action targets the right worktree.
8. Teardown destroys whatever the project's commands destroy. Prefer the worktree card's Teardown Resource dialog, which shows the commands before it asks. Call `worktree.resource.teardown` only on an explicit request here, after showing the user the exact teardown commands from the configuration that applies and offering pause as the lighter path; if you cannot establish those commands with confidence, leave teardown to the dialog. Daintree's confirmation still appears, and a declined confirmation is final. Deleting the worktree also runs the project's teardown: that is daintree.orchestration.workspace-cleanup.
9. Connecting has no app action: send the user to the worktree card's connect control, and do not assemble an SSH command from status metadata.

Report back: the exact worktree id and branch, the action requested, the commands that ran or would run, the status before, the acknowledgment, the status after or why verification is unavailable; for teardown or connect, the Daintree card path when the action did not run here.
