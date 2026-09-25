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
---
Use when: the user asks how much usage, quota, rate limit, premium requests or credits an agent CLI has left, or when it resets.

The figures exist only in each CLI's own account view; asked in prose, a model guesses. So: run the agent's read-only usage command at its own prompt, read the screen exactly, report.

Availability: Core; `slashCommands.list` raises an approval at core tier. A usage panel is not a turn: it gives no handback and may never produce a stop, so read the screen instead of waiting for a notice.

Known read-only views (September 2026 builds; revalidated in step 2 every time):

- Claude Code (checked): `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (formerly `/extra-usage`), which changes billing. Shows % USED per window with reset times; an API-key login shows session cost only.
- Codex (checked): `/status`. Never `/usage`, a menu that can REDEEM a banked limit reset. Shows % LEFT for the five-hour and weekly limits, resets and credits.
- Grok (checked): `/usage` (alias `/cost`), Usage limit tab; older builds `/session-info`. Never `/usage manage` (billing). Its weekly bar does not say used or left.
- Antigravity (checked): `/usage` (alias `/quota`). Avoid `/credits` in the TUI (purchase link). Shows "Remaining" % per model family. `agy -p "/usage"` prints the same in a shell, where that is permitted.
- Gemini CLI (documented, not checked): `/stats model`. Copilot CLI (documented, not checked): `/usage`. Kimi Code (not run): `/usage`.

Not known to offer an in-CLI allowance view as of those builds: Cursor (its `/usage` is activity), Aider, OpenCode, Goose, Crush, Amp, Kiro, Mistral Vibe, Open Interpreter, Qwen Code; session tokens or cost are not an allowance. Web pages: Claude https://claude.ai/settings/usage, Codex https://chatgpt.com/codex/settings/usage, Grok https://console.x.ai; otherwise point at the provider's account page, with no invented URL.

Procedure:
1. Resolve the agents with `agent.listAvailable`; list which you check and which you skip and why. `availabilityComplete:false` means some are unknown, not missing.
2. Revalidate each view with `slashCommands.list` for that `agentId`. Take a command only if its description says it shows usage, quota, limits or status, and reject any that also mentions manage, redeem, reset, buy, purchase, upgrade, top up or billing. A known view this build no longer lists is not trusted. If `slashCommands.list` is unavailable or declined, you have no way to confirm the command: send nothing, and give the user the known command to run themselves. If nothing qualifies, report no in-CLI allowance view found on this build. Never guess a command or substitute a web search.
3. Type only at an idle prompt in a terminal the user named that you own or were handed, or one you launch now with the exact `agentId` and no `prompt` (a prompt can be submitted over a startup screen). Never your own pane.

```json tool=agent.launch
{"agentId":"<id of the agent being checked>","name":"Codex: usage check","worktreeId":"<worktree id>","focusPolicy":"preserve"}
```

4. Read `terminal.getStatus` with `includeOutput`, with at most one `terminal.waitUntilIdle` while it starts, until an empty input line shows. A trust dialog for this launch directory is answered as the foundation says; a login, update, permissions or data-sharing prompt goes to the user.
5. Send exactly the verified command, with no `handback` and no `notify`:

```json tool=terminal.sendCommandOwned
{"terminalId":"<terminal id>","command":"<verified read-only command>"}
```

   Confirm on screen that the view opened. If the agent took it as a chat message, stop; never resend it in another form. If the screen offers to buy, redeem, reset or change billing, select nothing. The send cannot press keys: tell the user which tab or scroll key shows the figures. `RESOURCE_NOT_OWNED`: give the user the command to type.
6. Read the panel with `terminal.getOutput` (`maxLines` around 80), exactly, not a summary. Still loading: one more read, then report what is there.
7. Leave the panel open. Close only terminals you launched, only when asked (`terminal.closeOwned`).

Report back, per agent:
- Remaining allowance per window, as LEFT. Convert a USED figure (left = 100 − used) and say so. A percentage not labelled used or left is reported as shown with the direction unknown. A session token count is not an allowance.
- Reset times exactly as shown, with timezone; credits or premium requests where shown.
- The source of every figure ("read from <agent>'s <command> screen"), or "not checked: <reason>". Never a figure you did not read now.
- Every terminal you launched, by id and agent, and anything left on screen for the user.
