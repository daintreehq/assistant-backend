---
id: daintree.agents.run-agent-command
title: Run a CLI command in an open agent
summary: Drive an agent that is already open with one of its own slash commands — compact, clear or new, model switch, review, a project command — chosen from Daintree's command listing, typed at its idle prompt, menus answered by label, and the effect read back from its screen.
whenToUse: 'Use when an agent that is already open should run one of its OWN CLI slash commands rather than be given a task: "compact Claude''s context, focusing on the auth work", "/clear that agent''s context and brief it fresh on X", "switch the Codex pane to another model", "run /review in the codex terminal", "run our /deploy-check command in that agent", "what slash commands does Codex have here?". The deliverable is the command''s visible effect read back from that agent''s screen, or the list of commands it offers. Do not use for usage, quota or rate-limit figures (daintree.agents.check-usage-limits), for a command that is a NEW agent''s first turn (daintree.edits.spawn-visible-agent) or a per-issue command chain (daintree.orchestration.fan-out-work), for a plugin-contributed skill (daintree.skills.use-plugin-skill), for a correction in plain words (daintree.agents.steer-running-agent), or for moving the task to a fresh agent (daintree.agents.hand-off-task).'
risk: terminal
requiredTools:
  - terminal.list
  - terminal.getStatus
  - slashCommands.list
  - terminal.sendCommandOwned
optionalTools:
  - terminal.notifyWhenIdle
  - terminal.waitUntilIdle
  - terminal.sendKeysOwned
---
A slash command works only typed at the CLI's idle, empty input line; mid-turn it waits or is read as chat. Names differ per CLI and per build, so list them, never recall them: a guessed name is sent as a chat message the agent then acts on. `slashCommands.list` combines Daintree's built-in catalog with command files it finds on disk; it does not ask the running CLI, so the screen is the final word on what a command did.

Procedure:
1. Listing only ("what commands does Codex have here?"): `slashCommands.list` with that `agentId` and the project root as `projectPath`; report `label`, `description` and `scope` as returned, and stop. No terminal needed.
2. Resolve the terminal and its `agentId` from `terminal.list`: one you own or were handed, never your own pane. Not yours: give the user the command to type. One `terminal.getStatus` with `includeOutput:{"lines":30}`. `working`: arm `terminal.notifyWhenIdle` (note "run <command>") and end your turn. Asking a question: that comes first (daintree.agents.answer-waiting-agent).
3. Always, even for a command you know (`/compact`): `slashCommands.list` with that `agentId` and, as `projectPath`, the root the agent runs in. Pick the command whose `label` and `description` match the intent (compact, clear or new, model, review, the project command named); only a listed command is sent, as its `insertText` when present, else its `label`. None or several fit: tell the user and send nothing. A local command (`sourcePath` set) chosen by intent rather than by name: read that file first; one that also publishes, deletes, pushes or does work beyond the request is not sent. The listing unavailable or declined: give the user the command to type.
4. Guard what is lost:
   - Clear, new or reset wipes the conversation; compact summarises it and drops detail. Send one only when the user's words ask for it.
   - Compact: put what must survive in the focus text when its description shows it takes one (Claude: `/compact <focus>`).
   - Clear: before sending, have the brief to resend (the task, what is done, what is next), since the agent forgets everything.
   - Login, logout, billing, purchase or usage-credit commands are never sent from here; usage figures are daintree.agents.check-usage-limits.
5. Immediately before sending, the screen must show a blank input line: no user draft, no open selector. Otherwise touch nothing and report what is in the way. Send exactly the command, with only the arguments the user gave or its description documents, and never `handback` (it appends an instruction to your text, which would become compact focus or a chat message):

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"/compact Keep: the task is the auth token refresh fix; tests in tests/auth must stay green; do not touch the public API.","notify":true}
```

   Long commands (compact, review, init, a project command that does work): `notify:true` as above and end the turn. Quick ones (model, clear, a picker): no `notify`; one `terminal.waitUntilIdle` with `timeoutMs:15000`, then read the screen.
6. Classify the screen (`terminal.getStatus`, `includeOutput:{"lines":40}`), now and on every resume:
   - A picker or menu (model, a list): `terminal.sendKeysOwned` with `choose` set to the label of the option the user named, exactly as shown; `notify:true` when that choice starts work. The user named none, or it touches billing, login or permissions: choose nothing; leave it open and ask.
   - Still working: arm `terminal.notifyWhenIdle` and end the turn.
   - Done: read the effect (a compaction summary, the model now named, the command's output). The agent answered in prose about the command instead: it took it as chat; stop and never resend it in another form. After a clear the user wanted continued, send the fresh brief as its own submission with `notify:true, handback:true`.

On notice or resume: the wait armed in step 2 fired: continue at step 3. Any later notice means it stopped, not that the command finished: classify as step 6.

Stop and recovery: one send per command; a picker gets one choice. A command whose effect is unclear is reported, never re-typed.

Report back: agent and terminal id; the command as listed and as sent, with its `scope`; any menu choice; the effect quoted from the screen; what was lost (context) and what was preserved (focus text, brief resent).
