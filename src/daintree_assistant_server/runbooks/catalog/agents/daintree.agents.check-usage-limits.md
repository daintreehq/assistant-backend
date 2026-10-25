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
Asked in prose, a model guesses: read the agent's own usage screen (no turn; it may never stop). A Claude Code pane's warning ("You've used 80% of your weekly limit · resets …") is a reading as of when seen; none proves nothing. A standing watch ("stop at 90%"): re-read before more work goes to that account once a reading is 20 minutes old.

Read-only views (September 2026; revalidate); a command to avoid never skips its CLI.

- Claude Code: `/usage` (aliases `/cost`, `/stats`). Never `/usage-credits` (was `/extra-usage`): billing. Shows % USED; an API-key login, session cost only.
- Codex: `/status`. Never `/usage` there: a menu that can REDEEM a banked limit reset. Shows % LEFT, resets, credits.
- Grok: `/usage` (alias `/cost`), Usage limit tab; older `/session-info`. Never `/usage manage` (billing). Its weekly bar has no used/left label.
- Antigravity: `/usage` (alias `/quota`). Avoid `/credits` (purchase). Shows "Remaining" % per model family.
- Unchecked: Gemini `/stats model`, Copilot `/usage`, Kimi `/usage`.

Cursor's `/usage` is activity, not allowance. Web pages: claude.ai/settings/usage, chatgpt.com/codex/settings/usage, console.x.ai; else the provider's account page; never invent one.

On a host: the hosts named, else every connected one in `host.list`. With `host.call`, this machine is a host too (`local`): no pane in a project. Codex: `agent.readQuota` (any project there): `ok`: per window `usedPercent`, `windowDurationMins` (300: five-hour), `resetsAt` (epoch ms), `planType`; `unavailable`, its `reason`.

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"agent.readQuota","args":{}}
```

Other CLIs, or Codex's credits: steps 1-2 in any project there (only a built-in command; no project: not checked), then a throwaway run, never a project:

```json
{"hostId":"<hostId>","throwaway":true,"tool":"throwaway.launch","args":{"agentId":"<agentId>"},"newCallId":"<uuid>"}
```

Keep `result.structured.run.runId`; ambiguous: that `callId`, never a relaunch (no listing has it). Then, `args` `{runId,…}`: poll `throwaway.output` (`maxLines` 80) a few seconds apart, a minute at most, for the input line; answer a trust dialog (this check's) with `throwaway.sendKeys`, the key shown (`keys:["1"]`); `throwaway.sendCommand` (`command`); `throwaway.output` per step 5. Finally `throwaway.close` every run, whatever happened (`close-failed`: once more, say so); expiry is no cleanup. A launch answered `unsupported` (older Daintree, or throwaway off): steps 3-6 in one of its projects, owned tools.

1. Resolve the agents with `agent.listAvailable`; say which you check, which you skip, and why.
2. Revalidate with `slashCommands.list` for that `agentId`: a command qualifies only if its description says usage, quota, limits or status and none of manage, redeem, reset, buy, purchase, upgrade, top up, billing; a known view it no longer lists is untrusted. No list (unavailable, declined): send nothing, give the user the known command. Nothing qualifies: no in-CLI view. Never guess a command.
3. Type only at an idle prompt: a terminal the user named that you own, or one you launch now (exact `agentId`, `focusPolicy:"preserve"`, no `prompt`), never your own pane. A fresh pane: act once `terminal.waitUntilIdle` then `terminal.getStatus` (`includeOutput`) shows its empty input line (timeout: still starting; read again).
   - Folder-trust dialog on what you just launched: the foundation's trust rule (`choose`); wait for the input line; say you trusted it.
   - Any other startup prompt (login, approval, update), here or in a throwaway run: the user's. That agent: not checked; the others go on.
4. Send exactly the verified command, with no `handback`, `notify` or `waitForReply`. Confirm the view opened; taken as a chat message: stop, never resend it in another form. If the screen offers to buy, redeem, reset or change billing, select nothing.
5. Read the panel with `terminal.getOutput` (`maxLines` about 80), exactly; still loading, one more read.
6. Close every terminal you launched for this check, one `terminal.closeMany` per machine, except one at a startup prompt or unread, left for the user. A close awaiting confirmation (`CONFIRMATION_TIMEOUT`): leave it, say so, never retry.

Report back, per machine and agent: % LEFT per window (USED as 100 − used, quoting the printed figure; an unlabelled percentage has unknown direction; session tokens are no allowance); resets as shown, with timezone; credits or premium requests; each figure's source (screen and command, or `agent.readQuota`) or "not checked: <reason>", never a figure not read now; each terminal or run you launched, closed or kept and why. Never add machines up: one account on two machines is one allowance. Most headroom: the largest % left on the same CLI and window, with plan and reset.
