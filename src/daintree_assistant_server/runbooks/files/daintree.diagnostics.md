---
id: daintree.diagnostics
title: Diagnose a broken or misbehaving Daintree
summary: Diagnose Daintree itself from its MCP status, offered tools, workspace snapshot, recent error log and notification history, then give the concrete recovery step.
whenToUse: Use when the question is about DAINTREE'S OWN HEALTH — something is misbehaving ("nothing is responding", "the MCP is disconnected", "I did X and nothing happened", "why can't I…", "why is this tool missing"), the user asks to show recent Daintree errors or notifications, wants diagnostics for a bug report, or wants sign-in confirmed before relying on it. Nothing has to be broken for the sign-in case because `daintree.status` reports only the Daintree MCP link, not provider-key validity. A plain "is Daintree connected?" with nothing wrong is daintree.foundation. Do not use for a plain how-to, setting or keybinding question; for ordinary project failures; for a specific broken agent/spawn (use daintree.agents.recover-agent); for paused-workflow reconstruction (use daintree.orchestration.status-and-resume); or for red project CI (use daintree.ci.diagnose-and-fix).
risk: system
requiredTools:
  - daintree.status
  - daintree.listTools
  - tool.search
  - context.snapshot
  - daintree.call
---
Use when: something in Daintree is not working and the user wants to know why.

This runbook is for LIVE STATE, not for documentation. A plain "how do I…" question arrives with the matching pages already retrieved and needs no runbook at all — if you are reading this on such a turn, just answer from the documentation block. What you are here for is the other case: the user expected something to happen and it did not.

Guessing is the failure mode this runbook exists to prevent. Daintree's surface changes, and a diagnosis invented from a plausible-sounding memory is worse than "let me check", because the user acts on it.

Procedure:

1. READ THE STATE, don't theorise.
   - `daintree.status` reports the MCP link: endpoint, connected, transport, tool count, and the error text when it is down. It works WHILE disconnected, which is exactly when you need it.
   - `context.snapshot` for workspace state — worktrees, terminals, agents — when the complaint is about something that should be there and isn't.
   - A DISCONNECTED link explains almost every "nothing works" report at once: the control-plane tools (terminal, agent, worktree, git, forge) all stop, so say that first and give the reconnect step rather than diagnosing each missing capability separately. Note what still WORKS, because it changes what you can do next: `daintree.status` itself is designed to answer while disconnected, and documentation retrieval does not go through that link — so a how-to question can still be answered while the app is unreachable.
   - When the complaint is a runtime/inter-process error, call `daintree.call({"name":"errors.recent","arguments":{"limit":20,"includesDismissed":false}})`. It returns newest-first active entries under `errors`, including message/details/source/timestamp, context ids, retryability, recovery hint and occurrence count. `includesDismissed:true` is only for forensics; an empty default list can also mean every recorded error was dismissed.
   - For completion/waiting/informational events and quiet inbox entries, call `daintree.call({"name":"notifications.recent","arguments":{"limit":20}})`. Optional `type` is `success`, `error`, `info` or `warning`; `unreadOnly:true` means not seen as a toast, not necessarily not read anywhere. It returns `notifications` with title/message/timestamp and optional worktree/panel/event context.
   - These two reads are separate stores. Read both for "what went wrong" because an error can be recorded without a notification and a failed operation can notify without an error-log row. They are Daintree workbench reads, but the lack of typed CLI wrappers means `daintree.call` still incurs the CLI's system-risk confirmation. Never call them while MCP is disconnected and present the resulting transport error as the original cause.

2. A MISSING TOOL is a real diagnosis, not a dead end. `daintree.listTools` marks each tool `callable`. A tool that exists but is `callable: false` was not offered THIS turn — say so and name what it does, rather than calling it and reporting a confusing failure. `tool.search({"query":"rename terminal"})` finds a capability by a couple of plain keywords when you know the job but not the name. If the capability genuinely does not exist, saying that IS the answer.

3. COMPARE against what SHOULD happen, then remediate. When the turn also carries a "Daintree documentation" block, that is the other half of the diagnosis: the useful output is the gap — "the docs say X happens when you Y; your link is disconnected, so it cannot". Cite any page you lean on by the full `URL:` line printed with it. If no documentation arrived, diagnose from the state alone. This is the one place that OVERRIDES the general permission to answer from memory with an unverified flag: a remembered "what should happen" is what turns a diagnosis into a wrong instruction the user then acts on, so say the docs did not cover it and report what you actually observed instead — and do not reconstruct the expected behaviour from project source either. Finish with the exact next step — a command, a setting, a reconnect — not a list of things to try.

4. CORRELATE BEFORE PRESCRIBING. Match error and notification rows by timestamp and worktree/panel/terminal context, allowing for missing ids. A repeated `occurrenceCount` is one deduplicated error, not that many independent root causes. A recovery hint is evidence from Daintree, not proof that the suggested retry will work. Prefer the newest root-cause row over downstream notification noise, and never expose credential-like command output, endpoint query strings or secrets from `details`.

5. DIAGNOSTICS TO PASTE. When the user wants something to attach to a bug report, give them the safe set: what `daintree.status` reported, the workspace shape from `context.snapshot`, bounded recent error/notification rows relevant to the incident, and the exact safe error text. Never include the API key, in any form — not the value, not a prefix, not its length. If they ask you to echo the key to prove it is set, refuse and explain that no diagnostic here needs it.

Key questions are their own case, and `daintree.status` does NOT answer them: it reports the Daintree MCP link only — endpoint, connected, transport, tool count — which is a different connection from the model provider entirely. A connected MCP with a dead key looks perfectly healthy there. Point the user at the sign-in check the documentation describes rather than inferring one from a failed turn. Three outcomes that must not be conflated: a REJECTED key needs a new one, a key with no CREDIT needs topping up and is otherwise fine, and an UNREACHABLE provider is neither — never tell a user their key is invalid when we simply could not ask.

Notes:
- Answer the question asked. A user whose terminals stopped responding does not need an explanation of the worktree model.
- Never invent a settings key, flag, keybinding, or tool name. A near-miss name is worse than an admission, because the user will try it.

Report back: what you observed, what it should have been, and the one concrete step that fixes it.
