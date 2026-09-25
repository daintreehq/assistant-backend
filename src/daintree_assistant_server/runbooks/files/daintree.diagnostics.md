---
id: daintree.diagnostics
title: Diagnose a broken or misbehaving Daintree
summary: Diagnose Daintree itself from its MCP status, offered tools, workspace snapshot, recent error log and notification history, then give the concrete recovery step.
whenToUse: Use when the question is about DAINTREE'S OWN HEALTH — something is misbehaving ("nothing is responding", "the MCP is disconnected", "I did X and nothing happened", "why can't I…", "why is this tool missing"), the user asks to show recent Daintree errors or notifications, wants diagnostics for a bug report, or wants sign-in confirmed before relying on it. Nothing has to be broken for the sign-in case, because a working Daintree connection says nothing about the agent CLI's own provider sign-in. A plain "is Daintree connected?" with nothing wrong is daintree.foundation. Do not use for a plain how-to, setting or keybinding question; for ordinary project failures; for a specific broken agent/spawn (use daintree.agents.recover-agent); for paused-workflow reconstruction (use daintree.orchestration.status-and-resume); or for red project CI (use daintree.ci.diagnose-and-fix).
risk: system
requiredTools:
  - mcp.surface
  - actions.getContext
optionalTools:
  - actions.search
  - actions.getSchema
  - agent.listAvailable
  - errors.recent
  - notifications.recent
  - plugin.diagnostics
---
Use when: something in Daintree is not working as expected, the user wants recent Daintree errors or notifications or diagnostics for a bug report, or wants an agent's sign-in confirmed.

Availability: Core for the connection and surface checks (`mcp.surface`, `actions.getContext`, `actions.getSchema`). The error log (`errors.recent`), the notification inbox (`notifications.recent`) and `plugin.diagnostics` are on an agent pane's surface but ask the user first at core; the sidebar assistant at core and API-key clients are not offered them. The rest is host evidence from your own tools: your host's MCP status, your environment, and the agent CLI's own status commands.

Guessing is the failure this runbook prevents: a plausible diagnosis from memory is worse than "let me check", because the user acts on it. Never invent a setting, flag, keybinding or tool name.

Four boundaries, each able to fail alone. Find the failed one before prescribing anything; one working proves nothing about another:
- Runbook search (this service). If you are reading this, it answered. A search that returns a retryable error is a failed search, not "no runbook": retry it once. Its public health check (`https://assistant.daintree.org/readyz`) says nothing about the local app.
- The local Daintree app MCP: Daintree actions respond, or fail with a connection or transport error.
- Your caller's surface and permissions: the connection works, but this session is not offered an action, must ask the user for it, or does not own the target.
- The agent CLI's provider account (Claude Code's or Codex's own sign-in and limits). Daintree does not sign agents in.

Procedure:
1. Check the app connection once. Call `mcp.surface`; if it answers, keep `tier`, `appVersion` and `hash`: the link is up and the problem is elsewhere. If the Daintree tools are absent, or that call fails with a connection or transport error, that one failure is the finding. Do not call other Daintree actions to confirm it and do not keep retrying: a disconnected app cannot diagnose itself, and its transport error is not the original cause. Use host evidence instead:
   - your host's MCP status (Claude Code: `/mcp` or `claude mcp list`; Codex: `codex mcp list`);
   - whether you run in a Daintree pane at all (`DAINTREE_PANE_ID` is set in one), and whether the Daintree app is running;
   - not every pane gets a connection: Daintree connects Claude Code panes when the project's Daintree MCP setting is on (it starts off), a Codex pane may have none (Codex reaches Daintree as the sidebar assistant or an API-key client), and a running pane keeps the setting it launched with.
   Recovery, smallest step first: reconnect through the host (Claude Code's `/mcp`); turn the project's Daintree MCP setting on; relaunch the agent from Daintree. Warn before a relaunch: the new pane gets a new credential and no longer owns the terminals and worktrees the old one created. An API-key client needs Daintree's MCP server enabled and its key from Daintree's settings; never print that key.
2. A missing or refused capability is usually the caller, not an outage.
   - An action absent from `mcp.surface` is not offered to this session. `actions.getSchema` returns `NOT_FOUND` for an id outside this session's surface, and otherwise a `policy`: `callable`, `unavailableReason` (`DISABLED`), `requiresConfirmation`, `authorizedBy:"approval"` (the user is asked first) and `minimumTier`.
   - `USER_REJECTED` means the user declined an approval. `TIER_NOT_PERMITTED` means the action is beyond this caller's reach. `RESOURCE_NOT_OWNED` means this session does not own the target, for any reason including that it never existed. None of these is an outage, and none is fixed by an alias, another credential or raw terminal input.
   - Callers differ: the sidebar assistant has unscoped terminal tools, and an API-key client has no notices, no `worktree.waitUntilReady` and no `terminal.moveToWorktree`.
   - If the capability exists nowhere (`actions.search` finds capabilities by plain keywords), saying so is the answer.
3. Something expected did not happen, with the link up. Read `actions.getContext` for what the user has open. For runtime and inter-process failures, read `errors.recent` (default 20, maximum 50; `includesDismissed:true` only for forensics, since an empty default list can mean everything was dismissed). For completion, waiting and quiet messages, read `notifications.recent`. They are separate stores: read both for "what went wrong". Correlate rows by timestamp and by worktree, panel or terminal context; a high `occurrenceCount` is one deduplicated error, and a recovery hint is Daintree's suggestion, not a proven fix. Prefer the newest root-cause row over downstream noise. For a plugin, `plugin.diagnostics` with its manifest `pluginId` gives its load error, state and log tail; an unknown id fails with the list of real ids.
4. A Daintree notice that never arrived. A notice is typed only while the waiting pane sits at an empty prompt, so a half-typed draft holds it back; one line is outstanding at a time; a pane holds at most 32 pending; and it fires only after the target leaves `working`, so an agent that never started working keeps it pending. The pane's notify chip shows pending and delivery state, and its Stop drops every pending notice.
5. Provider sign-in or limits. `agent.listAvailable` reports each agent CLI's `availability` (`unauthenticated`, `blocked`, `missing` and so on) once `availabilityComplete` is true; confirm with the CLI itself, either as a command the user runs or a read-only command in your own shell (Claude Code: `/status`; Codex: `codex login status`; check its `--help` if that differs). A 401 or a rate-limit message in an agent's output belongs here, not to Daintree. How much allowance is left is daintree.agents.check-usage-limits. Never type into another agent's live prompt to check.
6. Compare with what should happen, from evidence only: the action's own description and schema (`actions.getSchema`), Daintree documentation you actually read, or observed behaviour. If nothing covers it, say so and report what you observed. Finish with the one next step (a reconnect, a setting to turn on, a relaunch, an approval to give), not a list of things to try, and say whether you saw it work.

Secrets: never output a credential in any form, not the value, a prefix, its length or a hash. That covers the Daintree API key, a pane's MCP credential in its generated config, provider keys and tokens, and credential-shaped text in error `details`, endpoints, query strings, environment dumps and terminal output. Do not read a credential to prove it is set; no diagnostic here needs it. A runbook search is sent off the machine, so describe the task in it, never paste logs or credentials.

For a bug report, give: the failed boundary and its evidence; `appVersion`, `tier` and the surface `hash`; the host and agent CLI versions; bounded, redacted error and notification rows for the incident with their timestamps and context ids; the exact safe error text; and the checks that were unavailable.

Report back: the failing boundary, the evidence and how it was observed, checks that were unavailable and why, and the one grounded next step, saying whether recovery was verified.
