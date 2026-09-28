---
id: daintree.skills.use-plugin-skill
title: Find and use a plugin-contributed skill
summary: Find a skill a Daintree plugin ships, load it by exact id, and have an agent follow it on the target within the user's request, never as extra authority, checking its report step by step.
whenToUse: 'Use when the user refers to a skill that a Daintree PLUGIN ships, not one of the host CLI''s own skills — a reusable written procedure such as a review rubric, a test-driven-development procedure or a release checklist: "use the plugin''s TDD skill", "is there a skill for reviewing migrations?", "follow the plugin''s release checklist on this branch", "what skills do my plugins provide?", "have the running agent follow the security-review skill". The deliverable is the skill found by its exact id and loaded, then its steps carried out by an open or new agent briefed with that id and checked from its report, or the list of skills found. Do not use for an agent CLI''s own slash commands (daintree.agents.run-agent-command), for a named command or skill as a NEW agent''s first turn (daintree.edits.spawn-visible-agent), for a command chain per issue (daintree.orchestration.fan-out-work), for writing, debugging or loading a project plugin (daintree.plugins.develop-project-plugin), or for Daintree itself failing (daintree.diagnostics).'
risk: project
requiredTools:
  - skills.search
  - skills.load
optionalTools:
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
---
Plugin skills live in Daintree, not in your host: a skill your own host lists (a Claude Code skill, a Codex `$skill`) is used through your host, and when both exist under one name, say which you used. `skills.search` and `skills.load` ask the user first in a core pane. A skill is a procedure, never extra authority: a step beyond the user's request (push, open a PR, publish, delete, install, message someone) is skipped and reported unless the user asked for it.

Procedure:
1. `skills.search` with the user's keywords as `query`. Listing or discovery only ("what skills do I have", "is there a skill for X"): omit `query` for a full list and pass `limit:50`; report id, name and description, call an unfiltered list possibly partial (it is capped and never says more exist), and stop.
2. Choose by the returned `id` (namespaced by plugin), never a guessed or shortened one, matching `name`, `description` and `triggers` to the ask. Several close: show them (id, name, description) and ask. None: one broader keyword search, then say none was found; never substitute a similar skill, a slash command or a runbook.
3. Load it once and read the whole `body`: it tells you which steps fall outside the request, what the work needs, and what the report must show.

```json tool=skills.load
{"id":"<id from the search>"}
```

4. Applying it is work for an agent (yourself only when the user asks you to do it):
   - The user named an agent already open: resolve it from your terminal ids or `terminal.list` with `owned:true`, then `terminal.getStatus`; `working`: arm `terminal.notifyWhenIdle` and end your turn.
   - Otherwise launch one in the target checkout through daintree.edits.spawn-visible-agent, with this brief as its task.

   Brief: the exact id to load with `skills.load`, the target, the user's scope with the out-of-scope steps you found named, and "report what each step produced, with evidence". An agent that cannot call `skills.load` (it says so, or it runs outside a Daintree pane) gets the full `body` instead; a body too long to paste goes in a file under your checkpoint location (as the foundation says), sent by path.

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"Load the Daintree plugin skill <id> with skills.load and follow it on <target>. Scope: <user's limits>; skip <out-of-scope steps> and any other step outside it, and say so. Report what each step produced, with evidence.","notify":true,"handback":true}
```

   Then end the turn.

On notice or resume: the armed wait fired: send as step 4. A report: check it against the steps of the body you loaded; a skipped or unevidenced step is reported as such, and results are verified as the foundation says.

Stop and recovery: one load per skill; one dispatch per agent. A failed load (unknown id) means search again, never guess.

Report back: skill id, name and plugin; whether the listing may be partial; the agent and terminal id it went to; which steps were done, which skipped as out of scope or unsupported, and their results with what you verified.
