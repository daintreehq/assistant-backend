---
id: daintree.agents.check-usage-limits
title: Check how much usage an agent has left
summary: Read an agent CLI's own account-usage view — remaining session, five-hour and weekly allowance, premium requests or credits, and when each resets — with the command that only reads, and report what is left and where each figure came from.
whenToUse: 'Use when the user wants to know how much of an AI agent CLI''s plan allowance is LEFT — usage limits, rate limits, quota, premium requests, credits, how close they are to a weekly or five-hour cap, when an agent''s usage resets, or how to check usage in a given agent CLI — for Claude Code, Codex, Grok, Antigravity, Gemini, Copilot, Kimi or any other configured agent, one agent or several, and for a follow-up asking the same about another agent. The deliverable is the account figures read off the agent''s own usage screen, never an answer the agent composes, so the work is running a read-only command inside the agent rather than giving it a task. Do not use for the allowance on other machines a hub reaches through their assistants (daintree.hosts.check-usage-limits), to put a question about code or the project to an agent (daintree.agents.ask-and-relay), to start agents on work in parallel (daintree.orchestration.fan-out-work), to be told later when something finishes (daintree.orchestration.watch-outcome), for Daintree''s own sign-in or health (daintree.diagnostics), to move a task off an agent that has hit its limit (daintree.agents.hand-off-task), to choose which model or reasoning effort an agent launches with, even to save budget (daintree.edits.spawn-visible-agent), or to run any other of the agent''s own slash commands (daintree.agents.run-agent-command).'
risk: terminal
requiredTools:
  - agent.listAvailable
  - agent.launch
  - terminal.getStatus
  - terminal.getOutput
  - terminal.sendCommandOwned
optionalTools:
  - terminal.sendKeysOwned
  - slashCommands.list
  - terminal.waitUntilIdle
  - terminal.closeOwned
---
The figures exist only in each CLI's own account view; asked in prose, a model guesses. Run the read-only usage command at the agent's own prompt and read the screen. A usage panel is not a turn: it gives no handback and may never stop, so read the screen rather than waiting for a reply.

Known read-only views (September 2026 builds; revalidate every time). Send the listed view; a command to avoid never means skipping that CLI.

- Claude Code: send `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (was `/extra-usage`), which changes billing. Shows % USED per window; an API-key login shows session cost only.
- Codex: send `/status`. Never `/usage` there: a menu that can REDEEM a banked limit reset. `/status` shows % LEFT for five-hour and weekly limits, resets and credits.
- Grok: `/usage` (alias `/cost`), Usage limit tab; older builds `/session-info`. Never `/usage manage` (billing). Its weekly bar does not say used or left.
- Antigravity: `/usage` (alias `/quota`). Avoid `/credits` (purchase link). Shows "Remaining" % per model family.
- Unchecked: Gemini CLI `/stats model`, Copilot CLI `/usage`, Kimi Code `/usage`.

No known in-CLI allowance view: Cursor (its `/usage` is activity), Aider, OpenCode, Goose, Crush, Amp, Kiro, Mistral Vibe, Open Interpreter, Qwen Code. Web pages: https://claude.ai/settings/usage, https://chatgpt.com/codex/settings/usage, https://console.x.ai; otherwise the provider's account page, never an invented URL.

1. Resolve the agents with `agent.listAvailable`; say which you check and which you skip and why.
2. Revalidate with `slashCommands.list` for that `agentId`: take a command only if its description says usage, quota, limits or status, and reject any that also mentions manage, redeem, reset, buy, purchase, upgrade, top up or billing. A known view this build no longer lists is not trusted. `slashCommands.list` unavailable or declined: send nothing and give the user the known command. Nothing qualifies: report no in-CLI allowance view. Never guess a command.
3. Type only at an idle prompt in a terminal the user named that you own, or one you launch now with the exact `agentId`, `focusPolicy:"preserve"` and no `prompt`. Never your own pane. A fresh pane is still starting: decide nothing from it until one `terminal.waitUntilIdle`, then `terminal.getStatus` with `includeOutput`, shows its empty input line; a timeout means still starting, so read again.
   - Folder-trust dialog on a pane you just launched: the foundation's trust rule applies, since the user asked for this check here. Answer it with `terminal.sendKeysOwned` (sidebar: `terminal.sendKeys`) `choose:"<the trust label shown>"` (Claude Code: "Yes, I trust this folder"), wait for the input line again, and report that you trusted it. Never stop the check over it.
   - Any other startup prompt (login, approval, update): the user's. Mark that agent not checked; the others go on.
4. Send exactly the verified command, with no `handback`, `notify` or `waitForReply`. Confirm the view opened; if the agent took it as a chat message, stop and never resend it in another form. If the screen offers to buy, redeem, reset or change billing, select nothing. Tell the user which tab or scroll key shows the figures.
5. Read the panel with `terminal.getOutput` (`maxLines` about 80), exactly; still loading, one more read. Leave it open; close only terminals you launched, on request. A sidebar close can wait on a confirmation: on `CONFIRMATION_TIMEOUT` leave it open and say so, never retry.

Report back, per agent: remaining allowance per window as LEFT (convert USED as 100 − used, quoting the printed figure; an unlabelled percentage has unknown direction; session tokens are not an allowance); reset times exactly as shown with timezone; credits or premium requests; the source of every figure ("read from <agent>'s <command> screen") or "not checked: <reason>", never a figure you did not read now; every terminal you launched.
