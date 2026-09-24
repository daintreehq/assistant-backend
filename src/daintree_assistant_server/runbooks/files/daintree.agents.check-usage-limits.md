---
id: daintree.agents.check-usage-limits
title: Check how much usage an agent has left
summary: Read an agent CLI's own account-usage view — remaining session, five-hour and weekly allowance, premium requests or credits, and when each resets — with the command that only reads, and report what is left and where each figure came from.
whenToUse: 'Use when the user wants to know how much of an AI agent CLI''s plan allowance is LEFT — usage limits, rate limits, quota, premium requests, credits, how close they are to a weekly or five-hour cap, when an agent''s usage resets, or how to check usage in a given agent CLI — for Claude Code, Codex, Grok, Antigravity, Gemini, Copilot, Kimi or any other configured agent, one agent or several, and for a follow-up asking the same about another agent. The deliverable is the account figures read off the agent''s own usage screen, never an answer the agent composes, so the work is running a read-only command inside the agent rather than giving it a task. Do not use to put a question about code or the project to an agent (daintree.agents.ask-and-relay), to start agents on work in parallel (daintree.orchestration.fan-out-work), to be told later when something finishes (daintree.orchestration.watch-outcome), or for Daintree''s own sign-in or health (daintree.diagnostics).'
confirmation: "Checking how much usage an agent has left. The aim is to read the figures off the agent's own screen with its read-only command, never one that spends a reset or buys credits."
risk: terminal
requiredTools:
  - context.snapshot
  - tool.search
  - tool.schema
  - daintree.invoke
  - agentTask.spawnForEdits
  - terminal.awaitAll
  - terminal.sendCommand
  - terminal.read
  - terminal.close
---
Use when: the user asks how much usage, quota, rate limit, premium requests or credits an agent CLI has left, or when it resets.

The figures exist only in each agent CLI's own account view. The agent cannot see them as a model — asked in prose it guesses or searches the web — and web answers about these commands have been wrong for several agents. So the method is always the same: run the agent's read-only usage command where the agent's own UI renders it, read the screen, report.

Known read-only views. "Checked" entries were confirmed against the CLI's own command table in September 2026 builds; commands move between releases, so a named command the agent no longer lists is resolved by step 2, not trusted.

- Claude Code (checked): read `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (formerly `/extra-usage`), which changes billing. Shows % USED per window — current session, current week for all models, current week per model — with reset times. An API-key login shows session cost only, not plan limits.
- Codex (checked): read `/status`. Never `/usage`, a menu that can REDEEM a banked usage-limit reset. Shows % LEFT for the five-hour and weekly limits, reset times and credits.
- Grok (checked): read `/usage` (alias `/cost`) and its Usage limit tab; older builds use `/session-info`. Never `/usage manage`, which is billing. Shows a weekly limit bar whose percentage the screen does not label as used or left.
- Antigravity (checked): read `/usage` (alias `/quota`). Avoid `/credits` inside the TUI, which offers a purchase link. Shows "Remaining" % per model family for the five-hour and weekly windows, with reset times. From their own shell the user can run `agy -p "/usage"` or `agy -p "/credits"` for the same figures as plain text — suggest it; you have no shell to run it in.
- Gemini CLI (documented, not checked): `/stats model`, requests against the daily quota for a Google login.
- Copilot CLI (documented, not checked): `/usage`, premium requests used against the monthly allowance.
- Kimi Code (present in the CLI, not run): `/usage`, membership quota.

No known in-CLI allowance view: Cursor (its `/usage` is activity stats), Aider, OpenCode, Goose, Crush, Amp, Kiro, Mistral Vibe, Open Interpreter, Qwen Code. They show session tokens or cost at most, which is not an allowance — say so. Web usage pages Daintree itself links: Claude https://claude.ai/settings/usage, Codex https://chatgpt.com/codex/settings/usage, Grok https://console.x.ai. For any other agent name no URL; point the user at their provider's account page.

Procedure:
1. Resolve which agents. A named agent must map to a configured agent id; "all my agents" means the configured ones — list which you checked and which you skipped and why. Ask only when the target is genuinely ambiguous.
2. Pick each agent's read-only view from the list above. For an agent not in it, or a listed command the agent no longer lists, look it up with the foundation's agent capability lookup: `tool.search` for `agentCapabilities.search`, else `daintree.invoke` → `slashCommands.list` with that agent id. The result is names and descriptions only — leads, not proof, and an empty list is incomplete coverage rather than absence. Take a command only when its description says it shows or views usage, quota, limits or status; reject any whose description also mentions manage, redeem, reset, buy, purchase, upgrade, top up or billing, because a view that doubles as a spend menu is how a banked reset gets used by accident. If nothing qualifies, report that you found no in-CLI allowance view. Never guess a command, and never substitute a web search for reading the agent.
3. The command must be typed at the agent's own idle input prompt, in a terminal the user named for this or one you create for this check — never an agent terminal you merely found idle. Before sending, read it fresh: an empty input prompt, no question, dialog or half-typed draft. To create one, spawn with `agentTask.spawnForEdits`, `mode: "explore"`, the exact configured `agentId` of the agent being checked — never omit it, since an omitted id launches the default agent instead — a short title such as "Codex: usage check", and a trivial taskPrompt such as "Reply with the single word READY and do nothing else." Tell the user this costs one tiny turn on that agent's own allowance. NEVER put the slash command in the taskPrompt: the prompt is composed with constraints and arrives as chat text, so the agent answers it as a question instead of running it.
4. Wait for the agent to be idle at its prompt (`terminal.awaitAll`). If the screen shows a trust, login, update, permissions or data-sharing prompt, stop and hand it to the user; do not answer it to get past.
5. Send exactly the read-only command with `terminal.sendCommand` and `verbatim: true`, one command per send. Without `verbatim` the send can arrive with text appended and the agent answers it as a prompt instead of running it. If the tool rejects `verbatim`, this build cannot send a bare command: stop and tell the user the command to type themselves. A queued receipt is not proof: confirm on screen that the agent opened its usage view. If it shows the agent treating the command as a chat message, stop — do not resend in another form. Never send bare keystrokes — Tab, arrows, Escape, an empty line — through `terminal.sendCommand`; a Tab meant to switch a help tab autocompleted a different command into the input box. When the figures sit behind a tab or need scrolling, tell the user which key to press instead.
6. Read the panel exactly with `terminal.read`, never a summary — numbers and reset times must be exact. A slash command produces no handback, so do not wait for one. A panel still loading ("Loading", "Scanning") gets one more read after a short wait; then report what is there.
7. Leave the panel as it is. Offer to close the terminals you opened; close only those, and only when asked or when the request included it.

Report back, per agent:
- Remaining allowance per window (session or five-hour, weekly, per-model family), as LEFT. Convert a USED figure (left = 100 − used) and say that you converted it. When the screen does not label its percentage as used or left, report the number as shown and say the direction is unknown — do not convert it or pick a reading. Never present a session token count as an allowance.
- Reset time exactly as shown, with its timezone; say when the screen gave UTC.
- Credits or premium requests where shown.
- The source for every figure — "read from <agent>'s <command> screen" — or "not checked — <reason>" for agents skipped or without an in-CLI view. Never report a figure you did not read this turn.
- Every terminal you created this turn, by id and agent — including one that launched the wrong agent or was abandoned — and anything left on screen for the user (an unanswered prompt, an open panel).
