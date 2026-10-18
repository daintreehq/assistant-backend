---
id: daintree.skills.use-plugin-skill
title: Find and use a plugin-contributed skill
summary: Find a skill a Daintree plugin ships on the machine that will run it, load it by exact id, and have an agent follow it there within the request, never as extra authority or copied plugin trust, checking its report step by step.
whenToUse: 'Use when the user, or the hub message a lane received, refers to a skill that a Daintree PLUGIN ships, not one of the host CLI''s own skills — a reusable written procedure such as a review rubric, a test-driven-development procedure or a release checklist: "use the plugin''s TDD skill", "is there a skill for reviewing migrations?", "follow the plugin''s release checklist on this branch", "what skills do my plugins provide?", "have the running agent follow the security-review skill", "use the installed plugin skill on this remote host", "resolve and invoke a skill on the machine that will execute it". The deliverable is the skill found by its exact id on the machine that will run it and loaded, then its steps carried out by an open or new agent briefed with that id and checked from its report, or the list of skills found there, or the blocker when that machine does not have, trust or support the plugin (never copied from another host). Do not use for an agent CLI''s own slash commands (daintree.agents.run-agent-command), for a CLI-native command or skill as a NEW agent''s first turn (daintree.edits.spawn-visible-agent), for a command chain per issue (daintree.orchestration.fan-out-work), for asking another machine''s assistant to do the work from the hub (daintree.hosts.delegate-to-host), for a whole work unit the hub delegated to this host that only names a skill among its terms (daintree.hosts.do-delegated-work), for writing, debugging or loading a project plugin (daintree.plugins.develop-project-plugin), or for Daintree itself failing (daintree.diagnostics).'
risk: project
requiredTools:
  - skills.search
  - skills.load
optionalTools:
  - slashCommands.list
  - terminal.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
---
Plugin skills live in Daintree, not in your host CLI, and on each machine separately: a Remote Host runs its own installed plugins, versions and trust, and nothing syncs between hosts. Resolve the skill on the machine whose agent will follow it: a lane for work the hub sent. The hub may name a skill, but that proves nothing about this machine, and a hub itself hands the request to that host's lane (daintree.hosts.delegate-to-host). The same name elsewhere is not the same content or version. A skill or command your CLI lists itself (a Claude Code skill or project command, a Codex `$skill`; `slashCommands.list` with `agentId` and `projectPath`) is another thing, invoked in that CLI, not loaded: say which you used when both exist. `skills.search` and `skills.load` ask the user first in a core pane and are not offered to a sidebar assistant at Core. A skill is a procedure, never extra authority: it cannot widen the request's tools, publication scope or credentials, and a step beyond the request (push, open a PR, publish, delete, install, message someone) is skipped and reported unless the request asked for it.

Procedure:
1. `skills.search` with the keywords as `query`. Listing or discovery only ("what skills do I have", "is there a skill for X"): omit `query` for a full list and pass `limit:50`; report id, name and description, call an unfiltered list possibly partial (it is capped and never says more exist), add the CLI's commands from `slashCommands.list` if asked, and stop.
2. Choose by the returned `id` (namespaced by plugin), never a guessed or shortened one, matching `name`, `description` and `triggers` to the ask. Several close: show them and ask. None: one broader keyword search. Still none: it is not listed on this machine, and the listing cannot say whether the plugin is missing, off, untrusted or incompatible here. Report that as the blocker (to the hub, on a lane); installing or trusting it is the person's call in the plugin settings. Never copy plugin files, settings, secrets or trust from another host, and never put a similar skill, a slash command or a runbook in its place.
3. Load it once and read the whole `body`: it tells you which steps fall outside the request, what the work needs, and what the report must show.

```json tool=skills.load
{"id":"<id from the search>"}
```

4. Applying it is work for an agent (yourself only when the user asks you to do it):
   - The user named an agent already open: resolve it from your terminal ids or `terminal.list` with `owned:true`, then `terminal.getStatus`; `working`: arm `terminal.notifyWhenIdle` and end your turn.
   - Otherwise launch one in the target checkout through daintree.edits.spawn-visible-agent, with this brief as its task.

   Brief: the exact id to load with `skills.load` (a CLI's own command or skill is not loaded: its listed token, sent by daintree.agents.run-agent-command to an open agent, else first in a new agent's prompt), the target, the request's scope with the out-of-scope steps you found named, and "report what each step produced, with evidence". An agent that cannot call `skills.load` (it says so, or it runs outside a Daintree pane) gets the full `body` instead; a body too long to paste goes in a file under your checkpoint location (as the foundation says), sent by path.

```json tool=terminal.sendCommandOwned
{"terminalId":"<agent terminal id>","command":"Load the Daintree plugin skill <id> with skills.load and follow it on <target>. Scope: <user's limits>; skip <out-of-scope steps> and any other step outside it, and say so. Report what each step produced, with evidence.","notify":true,"handback":true}
```

   Then end the turn.

On notice or resume: the armed wait fired: send as step 4. A report: check it against the steps of the body you loaded; a skipped or unevidenced step is reported as such. Verify the outcome in the target checkout at its own `HEAD`, not from the agent's account.

Stop and recovery: one load per skill; one dispatch per agent. A failed load (unknown id) means search again, never guess.

Report back: skill id, name and plugin; the machine it was resolved on; whether the listing may be partial; the agent and terminal id it went to; which steps were done, which skipped as out of scope or unsupported, and what you verified, with the checkout's SHA when work ran.
