---
id: daintree.worktree.manage-resource
title: Manage a worktree remote resource
summary: Inspect and interactively provision, pause or resume a worktree's configured remote resource, verifying with fresh status and refusing unsafe unattended or unpreviewed lifecycle actions.
whenToUse: Use when the user asks about a worktree's cloud devbox or remote resource status, wants to provision, pause or resume it, asks to save resource cost, or asks for a future pause/resume. Also use for teardown or connect requests so the assistant can route those unsupported-safe paths honestly. Do not use for creating or deleting an ordinary git worktree (use daintree.worktree.create-plain-worktree or daintree.orchestration.workspace-cleanup), Daintree's own CPU/memory resource profile, or starting a local dev server.
risk: system
requiredTools:
  - context.snapshot
  - worktree.list
  - worktree.getCurrent
  - user.askMultipleChoice
  - timer.schedule
  - worktree.resource.status
  - daintree.call
---
Use when: the user wants status or lifecycle control for a worktree's project-configured remote resource.

The actual MCP actions are `worktree.resource.status`, `worktree.resource.provision`, `worktree.resource.pause`, `worktree.resource.resume`, and `worktree.resource.teardown`. The STATUS read has a typed Assistant wrapper, `worktree.resource.status` — call it directly; sending it through `daintree.call` returns USE_TYPED_WRAPPER and does nothing. The four LIFECYCLE actions have no wrapper and still go through system-risk, always-confirmed and ungrantable `daintree.call`, which is the right gate for them: each one executes a project-configured command. All accept optional `{worktreeId}`, but this runbook ALWAYS supplies the exact id.

Procedure:

1. RESOLVE ONE EXACT WORKTREE from the injected roster, `worktree.getCurrent`, or `worktree.list`. A worktree id is the exact id returned by Daintree, not a branch name. If several match, use `user.askMultipleChoice`. Never rely on focused/active fallback for infrastructure actions.

2. READ FRESH STATUS first:
   `worktree.resource.status({"worktreeId":"<exact id>"})`.
   Inspect `structuredContent`: `{configured:boolean,status:null|{lastStatus?,lastOutput?,error?,lastCheckedAt?,endpoint?,meta?,provider?,resumedAt?,pausedAt?}}`. `configured:false,status:null` means no status command is configured. The action runs a real command and fails instead of returning stale cached state. Treat `unknown` as unknown, never healthy. Do not echo raw `lastOutput`, `meta`, or credential-shaped endpoints unless needed and demonstrably safe.

3. REQUIRE AN EXPLICIT INTERACTIVE REQUEST for provision, pause or resume. These execute arbitrary project-configured commands; provision/resume may incur cost, and pause is not guaranteed to be the inverse of resume. Call exactly one:
   - `daintree.call({"name":"worktree.resource.provision","arguments":{"worktreeId":"<id>"},"requestKey":"<stable key for this provision>"})`
   - `daintree.call({"name":"worktree.resource.pause","arguments":{"worktreeId":"<id>"}})`
   - `daintree.call({"name":"worktree.resource.resume","arguments":{"worktreeId":"<id>"}})`
   A stable request key protects an ambiguous PROVISION retry for 120 seconds; do not reuse it with changed arguments. Pause and resume are intentionally not deduplicated.

4. HANDLE AMBIGUOUS FAILURES WITHOUT REPLAY. A transport timeout does not prove the project command did nothing. Re-run fresh status before considering an identical provision retry. Lifecycle actions return only an acknowledgement, not a resulting resource object. For pause/resume the acknowledgement means the selected configured command returned successfully; provision may instead no-op when cached state is already ready/running/healthy/up, or route a paused/stopped resource through the resume command. None of these acknowledgements proves the external resource reached the desired state.

5. VERIFY WITH STATUS after a successful lifecycle call when a status command exists. Report fresh `lastStatus`, error, and `lastCheckedAt`. Pause/resume update their timestamps but do not themselves rewrite `lastStatus`; only the follow-up status can verify the external postcondition. If no status command exists, say verification is unavailable rather than reporting ready/paused from the acknowledgement.

6. DO NOT TEARDOWN THROUGH THIS ASSISTANT. `worktree.resource.teardown` is real and MCP-allowlisted, but its project-defined commands can destroy unrecoverable infrastructure. The assistant-visible project settings intentionally withhold those commands, so this path cannot preview the actual destructive content. Direct the user to the Daintree worktree card's **Teardown Resource** dialog, which shows the exact commands before confirmation. Do not substitute `pause` unless the user chooses it.

7. DO NOT PROMISE SCHEDULED LIFECYCLE ACTIONS. `daintree.call` cannot be authorized by an automation grant, so a timer/watcher/wake cannot provision, pause or resume later. Never schedule `call_safe_tool` targeting `daintree.call` and never offer `grant.create` for it. With the user's agreement, `timer.schedule` may create an `enqueue` REMINDER only; it runs nothing and does not wake the assistant. State that distinction before creating it and report the returned timer id.

8. ROUTE CONNECT REQUESTS TO THE UI. `worktree.resource.connect` exists only as a renderer action and is not exposed to the Assistant MCP. Tell the user to use the worktree card's connect control; do not invent an SSH command from status metadata.

9. HANDLE THE NONFOCUSED-TARGET EDGE CASE. The provision, pause, resume and teardown availability checks currently examine the focused/active worktree even when `arguments.worktreeId` names another one; status itself does not have this gate. If a valid explicit mutation target is refused as not configured, do not silently switch worktrees through another raw call. Ask the user to select/focus the target worktree or use its worktree-card action.

Report back: exact worktree id/branch, requested action, pre-action status, acknowledgement, post-action status or why verification was unavailable, and any reminder timer id. For teardown/connect, name the Daintree UI path required instead of claiming the MCP action ran.
