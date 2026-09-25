---
id: daintree.agents.check-usage-limits
title: Check how much usage an agent has left
summary: Read an agent CLI's own account-usage view — remaining session, five-hour and weekly allowance, premium requests or credits, and when each resets — with the command that only reads, and report what is left and where each figure came from.
whenToUse: 'Use when the user wants to know how much of an AI agent CLI''s plan allowance is LEFT — usage limits, rate limits, quota, premium requests, credits, how close they are to a weekly or five-hour cap, when an agent''s usage resets, or how to check usage in a given agent CLI — for Claude Code, Codex, Grok, Antigravity, Gemini, Copilot, Kimi or any other configured agent, one agent or several, and for a follow-up asking the same about another agent. The deliverable is the account figures read off the agent''s own usage screen, never an answer the agent composes, so the work is running a read-only command inside the agent rather than giving it a task. Do not use to put a question about code or the project to an agent (daintree.agents.ask-and-relay), to start agents on work in parallel (daintree.orchestration.fan-out-work), to be told later when something finishes (daintree.orchestration.watch-outcome), or for Daintree''s own sign-in or health (daintree.diagnostics).'
risk: terminal
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
  - terminal.sendCommandOwned
optionalTools:
  - slashCommands.list
  - worktree.list
  - terminal.list
  - terminal.waitUntilIdle
  - terminal.closeOwned
  - terminal.sendCommand
  - terminal.close
---
Use when: the user asks how much usage, quota, rate limit, premium requests or credits an agent CLI has left, or when it resets.

The figures exist only in each agent CLI's own account view. Asked in prose, a model guesses or searches the web, and web answers about these commands have been wrong for several agents. So the method is always the same: run the agent's read-only usage command at its own prompt, read the screen exactly, report.

Availability: Core, with client-specific screen handling. Needs `agent.launch`, `terminal.sendCommandOwned` and terminal reads; `slashCommands.list` raises an approval at core tier. A usage panel is not a turn: it produces no handback and may produce no working-to-idle change, so read the screen with bounded reads instead of waiting for a notice.

Known read-only views. "Checked" entries were confirmed against the CLI's own command table in September 2026 builds. Commands move between releases, so every one is revalidated in step 2 before use.

- Claude Code (checked): `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (formerly `/extra-usage`), which changes billing. Shows % USED per window (current session, current week for all models, current week per model) with reset times. An API-key login shows session cost only, not plan limits.
- Codex (checked): `/status`. Never `/usage`, a menu that can REDEEM a banked usage-limit reset. Shows % LEFT for the five-hour and weekly limits, reset times and credits.
- Grok (checked): `/usage` (alias `/cost`) and its Usage limit tab; older builds use `/session-info`. Never `/usage manage`, which is billing. Shows a weekly limit bar whose percentage the screen does not label as used or left.
- Antigravity (checked): `/usage` (alias `/quota`). Avoid `/credits` inside the TUI, which offers a purchase link. Shows "Remaining" % per model family for the five-hour and weekly windows, with reset times. From a shell, `agy -p "/usage"` or `agy -p "/credits"` print the same figures as plain text: run it in your own shell when that is permitted, or suggest it to the user.
- Gemini CLI (documented, not checked): `/stats model`, requests against the daily quota for a Google login.
- Copilot CLI (documented, not checked): `/usage`, premium requests used against the monthly allowance.
- Kimi Code (present in the CLI, not run): `/usage`, membership quota.

No known in-CLI allowance view: Cursor (its `/usage` is activity stats), Aider, OpenCode, Goose, Crush, Amp, Kiro, Mistral Vibe, Open Interpreter, Qwen Code. They show session tokens or cost at most, which is not an allowance; say so. Web usage pages Daintree itself links: Claude https://claude.ai/settings/usage, Codex https://chatgpt.com/codex/settings/usage, Grok https://console.x.ai. For any other agent give no URL; point the user at their provider's account page.

Procedure:
1. Resolve which agents with `agent.listAvailable`. A named agent must map to a real id; "all my agents" means the configured ones. List which you checked and which you skipped and why (not launchable, `availability` not ready, no in-CLI view). `availabilityComplete:false` means some are still unknown, not missing. Ask only when the target is genuinely ambiguous.
2. Revalidate each agent's view before using it: `slashCommands.list` with that `agentId` shows what this build offers. Take a command only when its description says it shows or views usage, quota, limits or status, and reject any whose description also mentions manage, redeem, reset, buy, purchase, upgrade, top up or billing: a view that doubles as a spend menu is how a banked reset gets used by accident. A known view this build no longer lists is not trusted. The listing is names and descriptions, leads rather than proof, and an empty one is incomplete coverage. If nothing qualifies, report that you found no in-CLI allowance view. Never guess a command, and never substitute a web search for reading the agent.
3. The command is typed at the agent's own idle prompt, in a terminal the user named for this that you own or were handed, or one you launch for this check: never an agent terminal you merely found idle, and never your own pane: typing into your own prompt is not a way to read your own usage. Launch with the exact `agentId` (a wrong one launches a different agent) and no `prompt`, so the agent starts at its input without spending a turn:

```json tool=agent.launch
{"agentId":"<id of the agent being checked>","name":"Codex: usage check","worktreeId":"<your current worktree id>"}
```

   Never put the command in `prompt`: it can be submitted while the agent is still on a startup screen.
4. Wait for an empty input prompt: `terminal.getStatus` with `includeOutput`, with at most one `terminal.waitUntilIdle` between reads while the agent starts. The screen must show an empty input line: no question, dialog or half-typed draft. A trust, login, update, permissions or data-sharing prompt goes to the user; do not answer it to get past.
5. Send exactly the read-only command, one command per send, with no `handback` (it appends Daintree's instruction text to the command) and no `notify` (a panel may never produce the stop it waits for):

```json tool=terminal.sendCommandOwned
{"terminalId":"<terminal id>","command":"<verified read-only command>"}
```

   The send has no verbatim flag and needs none: it submits the text as given with one Enter. A queued receipt is not proof; confirm on screen that the usage view opened. If the agent treated the command as a chat message, stop; do not resend it in another form. If the screen offers to buy, redeem, reset or change billing, select nothing and leave it to the user. The send cannot press keys, so when the figures sit behind a tab or need scrolling, tell the user which key to press. `RESOURCE_NOT_OWNED` on a terminal the user named: give them the command to type.
6. Read the panel exactly with `terminal.getOutput` and a bounded `maxLines` (around 80): never a summary, and not the agent's transcript, which does not hold the panel. A panel still loading ("Loading", "Scanning") gets one more read a few seconds later; then report what is there.
7. Leave the panel as it is. Offer to close the terminals you launched; close only those (`terminal.closeOwned`), and only when asked or when the request included it.

Report back, per agent:
- Remaining allowance per window (session or five-hour, weekly, per model family), as LEFT. Convert a USED figure (left = 100 − used) and say that you converted it. When the screen does not label its percentage as used or left, report the number as shown and say the direction is unknown; do not convert it or pick a reading. Never present a session token count as an allowance.
- Reset time exactly as shown, with its timezone; say when the screen gave UTC.
- Credits or premium requests where shown.
- The source of every figure, "read from <agent>'s <command> screen", or "not checked: <reason>" for agents skipped or without an in-CLI view. Never report a figure you did not read in this check.
- Every terminal you launched, by id and agent, including one that launched the wrong agent or was abandoned, and anything left on screen for the user (an unanswered prompt, an open panel).
