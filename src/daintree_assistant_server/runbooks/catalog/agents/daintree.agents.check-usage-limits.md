---
id: daintree.agents.check-usage-limits
title: Check how much usage an agent has left
summary: Read an agent CLI's own account-usage view, here or on each Remote Host — remaining session, five-hour and weekly allowance, premium requests or credits, and when each resets — with the command that only reads, and report what is left, machine by machine, and where each figure came from.
whenToUse: 'Use when the user wants to know how much of an AI agent CLI''s plan allowance is LEFT — usage limits, rate limits, quota, premium requests, credits, how close they are to a weekly or five-hour cap, when an agent''s usage resets, or how to check usage in a given agent CLI — for Claude Code, Codex, Grok, Antigravity, Gemini, Copilot, Kimi or any other configured agent, one agent or several, and for a follow-up asking the same about another agent; on this machine or on the Remote Hosts this Shell controls ("usage left on each of my machines", which machine has the most headroom), reported machine by machine, never added together. The deliverable is the account figures read off the agent''s own usage screen (on a host, Codex''s quota as Daintree reads it there), never an answer the agent composes, so the work is running a read-only command inside the agent rather than giving it a task. Do not use to put a question about code or the project to an agent (daintree.agents.ask-and-relay), to start agents on work in parallel (daintree.orchestration.fan-out-work), to be told later when something finishes (daintree.orchestration.watch-outcome), for Daintree''s own sign-in or health (daintree.diagnostics), to move a task off an agent that has hit its limit (daintree.agents.hand-off-task), to say which issues or tasks are still left to do (daintree.workflow.find-issue-work), to choose which agent, model or reasoning effort a task launches with, even to save budget (daintree.agents.choose-agent-model), or to run any other of the agent''s own slash commands (daintree.agents.run-agent-command).'
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
  - terminal.closeMany
  - host.list
  - host.call
---
The figures exist only in each CLI's own account view; asked in prose, a model guesses. Run the read-only usage command at the agent's own prompt and read the screen. A usage panel is not a turn: no handback, maybe never stopping, so read the screen. A working Claude Code pane's own warning ("You've used 80% of your weekly limit · resets …") is a reading as of when you saw it, quoted with that time; no warning proves nothing. A standing watch ("stop at 90%"): read again before more work goes to that account once the last reading is 20 minutes old.

Known read-only views (September 2026 builds; revalidate each time); a command to avoid never means skipping that CLI.

- Claude Code: send `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (was `/extra-usage`), which changes billing. Shows % USED per window; an API-key login shows session cost only.
- Codex: send `/status`. Never `/usage` there: a menu that can REDEEM a banked limit reset. `/status` shows % LEFT for five-hour and weekly limits, resets and credits.
- Grok: `/usage` (alias `/cost`), Usage limit tab; older builds `/session-info`. Never `/usage manage` (billing). Its weekly bar does not say used or left.
- Antigravity: `/usage` (alias `/quota`). Avoid `/credits` (purchase link). Shows "Remaining" % per model family.
- Unchecked: Gemini CLI `/stats model`, Copilot CLI `/usage`, Kimi Code `/usage`.

No known in-CLI allowance view: Cursor (its `/usage` is activity), Aider, OpenCode, Goose, Crush, Amp, Kiro, Mistral Vibe, Open Interpreter, Qwen Code. Web pages: https://claude.ai/settings/usage, https://chatgpt.com/codex/settings/usage, https://console.x.ai; otherwise the provider's account page, never an invented URL.

On a host: the hosts named, else every connected one in `host.list` (this machine too when asked), each call through `host.call` there. Codex: `agent.readQuota` (that machine's Codex account; any of its projects): `ok`: per window `usedPercent`, `windowDurationMins` (300: five-hour), `resetsAt` (epoch ms), `planType`; `unavailable`, its `reason`.

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.readQuota","args":{}}
```

Other CLIs, or Codex's credits: steps 1-6 there, owned tools only.

1. Resolve the agents with `agent.listAvailable`; say which you check and which you skip and why.
2. Revalidate with `slashCommands.list` for that `agentId`: a command qualifies only if its description says usage, quota, limits or status and none of manage, redeem, reset, buy, purchase, upgrade, top up or billing; a known view this build no longer lists is not trusted. List unavailable or declined: send nothing; give the user the known command. Nothing qualifies: report no in-CLI allowance view. Never guess a command.
3. Type only at an idle prompt in a terminal the user named that you own, or one you launch now with the exact `agentId`, `focusPolicy:"preserve"` and no `prompt`. Never your own pane. A fresh pane: decide nothing until one `terminal.waitUntilIdle`, then `terminal.getStatus` with `includeOutput`, shows its empty input line (a timeout: still starting; read again).
   - Folder-trust dialog on a pane you just launched: the user asked for this check here, so the foundation's trust rule applies: `terminal.sendKeysOwned` (sidebar: `terminal.sendKeys`) `choose:"<the label shown>"`, wait for the input line, and report that you trusted it.
   - Any other startup prompt (login, approval, update): the user's. Mark that agent not checked; the others go on.
4. Send exactly the verified command, with no `handback`, `notify` or `waitForReply`. Confirm the view opened; if the agent took it as a chat message, stop and never resend it in another form. If the screen offers to buy, redeem, reset or change billing, select nothing.
5. Read the panel with `terminal.getOutput` (`maxLines` about 80), exactly; still loading, one more read.
6. Close every terminal you launched for this check, one `terminal.closeMany` per machine, except one at a startup prompt or unread, left for the user. A close awaiting confirmation (`CONFIRMATION_TIMEOUT`): leave it, say so, never retry.

Report back, per machine and agent: remaining allowance per window as LEFT (convert USED as 100 − used, quoting the printed figure; an unlabelled percentage has unknown direction; session tokens are not an allowance); reset times exactly as shown with timezone; credits or premium requests; the source of every figure ("read from <agent>'s <command> screen", "<host>'s agent.readQuota") or "not checked: <reason>", never a figure you did not read now; every terminal you launched, with its host, closed or kept and why. Never add machines up: one account on two machines is one allowance shown twice. Most headroom: the largest % left on the same CLI and window, with plan and reset.
