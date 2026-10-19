---
id: daintree.diagnostics
title: Diagnose a broken or misbehaving Daintree
summary: Diagnose Daintree itself from its MCP status, offered tools, workspace snapshot, recent error log and notification history, then give the concrete recovery step.
whenToUse: Use when the question is about DAINTREE'S OWN HEALTH — something is misbehaving ("nothing is responding", "the MCP is disconnected", "I did X and nothing happened", "why can't I…", "why is this tool missing"), the user asks to show recent Daintree errors or notifications, wants diagnostics for a bug report, or wants sign-in confirmed before relying on it. Nothing has to be broken for the sign-in case, because a working Daintree connection says nothing about the agent CLI's own provider sign-in. A plain "is Daintree connected?" with nothing wrong is daintree.foundation. Do not use for a plain how-to, setting or keybinding question; for ordinary project failures; for a specific broken agent/spawn (use daintree.agents.recover-agent); for paused-workflow reconstruction (use daintree.orchestration.status-and-resume); for red project CI (use daintree.ci.diagnose-and-fix); for a project plugin the user is writing or debugging (use daintree.plugins.develop-project-plugin); for a project port another process holds (use daintree.runtime.free-busy-port); or for reaching or controlling another machine (use daintree.hosts.foundation), or for work sent to another machine that never came back (use daintree.hosts.recover-and-clean-up).
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
`errors.recent`, `notifications.recent` and `plugin.diagnostics` ask first in a core pane; the sidebar at core and API-key clients lack them. Never guess: no setting, flag, keybinding or tool name from memory.

Four boundaries fail independently; find the failed one before prescribing:
- Runbook search: only a fresh call shows it answers now; a retryable error is a failed search (retry once), not "no runbook". Its `/readyz` says nothing about the local app.
- The local Daintree app MCP.
- This caller's surface and permissions.
- The agent CLI's own provider sign-in and limits (Daintree signs no agent in).

On a host: step 1 is your own session and CLI: keep it local. Call the other tools on the machine where it failed, and run an agent's CLI checks there (step 5), per the hosts foundation.

1. Call `mcp.surface` once (this session's own; call it again after a relaunch). Answers: keep `tier`, `appVersion`, `hash`; the link is up. Tools absent or a transport error: that one failure is the finding. Do not probe or retry other Daintree actions: a disconnected app cannot diagnose itself. Use client evidence: your client's MCP status (Claude Code `/mcp` or `claude mcp list`; `codex mcp list`), whether `DAINTREE_PANE_ID` is set, whether the app runs. Claude Code panes connect only when the project's Daintree MCP setting is on (default off), fixed at launch; read a Codex pane's connection from its offered tools. Recovery, smallest first: reconnect via the client, turn the setting on, relaunch from Daintree (warn: the new pane owns nothing the old one created). An API-key client needs Daintree's MCP server enabled and its key; never print the key.
2. A missing or refused action is usually the caller. Absent from `mcp.surface`: not offered to this session. `actions.getSchema` gives `NOT_FOUND` off-surface, else a `policy` (`callable`, `unavailableReason`, `requiresConfirmation`, `authorizedBy:"approval"`, `minimumTier`). `USER_REJECTED`, `TIER_NOT_PERMITTED`, `RESOURCE_NOT_OWNED` are not outages and no alias, credential or raw input fixes them. An empty `actions.search` is scoped to this caller, not proof Daintree lacks it.
3. Link up, something didn't happen: `actions.getContext`, then `errors.recent` (default 20, max 50; `includesDismissed:true` only for forensics) and `notifications.recent`, separate stores. Correlate by timestamp and context ids; a high `occurrenceCount` is one error; a recovery hint is a suggestion. Prefer the newest root-cause row. A plugin: `plugin.diagnostics` with its manifest `pluginId`.
4. A notice that never arrived: it types only at an empty prompt (a draft holds it back), one line at a time, 32 pending per pane at most, and only after the target leaves `working`. The pane's notify chip shows pending state; its Stop drops them all.
5. Sign-in or limits: `agent.listAvailable` `availability` once `availabilityComplete`, then confirm with the CLI (`/status`; `codex login status`). A 401 or rate-limit in an agent's output belongs here. Allowance left is daintree.agents.check-usage-limits. Never type into another agent's live prompt to check.
6. Compare with the action's own description and schema, docs you actually read, or observed behaviour. End with the one next step and whether you saw it work.

Secrets: never output a credential in any form (value, prefix, length or hash): the Daintree key, a pane's MCP credential, provider tokens, credential-shaped text in errors, URLs, env dumps or output. Don't read one to prove it is set. Runbook searches leave the machine: describe the task, never paste logs.

Bug report: the failed boundary and evidence; `appVersion`, `tier`, surface `hash`; host and CLI versions; bounded, redacted error and notification rows with timestamps and context ids; checks unavailable. Logs to share: send the user to Settings → Troubleshooting → Download diagnostics, where they choose sections, redactions and find-and-replace rules before saving. Its redaction is best-effort: have them read the ZIP before sending it. Never copy raw log files out yourself unless asked, and then bounded and redacted.

Report back: the failing boundary, evidence and how observed, checks unavailable and why, and the one grounded next step, saying whether recovery was verified.
