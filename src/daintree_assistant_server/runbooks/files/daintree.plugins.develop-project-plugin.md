---
id: daintree.plugins.develop-project-plugin
title: Build, load and debug a Daintree project plugin
summary: Take a project plugin under .daintree/plugins/ from written to verifiably loaded — validate its manifest, keep its built dist/ committed, reload and read its real state, and diagnose one that is invalid, staged, blocked, inert or whose buttons do nothing.
whenToUse: 'Use when the user or an agent is writing, changing or debugging a Daintree PROJECT PLUGIN, a manifest plus committed dist/ under the project''s .daintree/plugins/ — "build a Daintree plugin panel for our deploy board", "I changed the plugin, get Daintree to pick it up", "my plugin isn''t loading", "the plugin panel is blank", "the button in my plugin does nothing", "validate this plugin manifest", "why does the plugin manager say Unreadable or Staged", "my agent can''t see the plugin''s tools". The deliverable is the plugin active and exercised, or its exact state and the reason it is not. Do not use for Daintree itself misbehaving with no plugin of the user''s involved (use daintree.diagnostics), for a web app in a Dev Preview panel (use daintree.runtime.debug-web-app), for running a workspace recipe (use daintree.recipe.run-or-create), or for finding and following a skill a plugin contributes (use daintree.skills.use-plugin-skill).'
risk: project
requiredTools:
  - worktree.list
  - plugin.validate
  - plugin.reloadProject
  - plugin.diagnostics
  - agent.launch
optionalTools:
  - plugin.reloadPanel
  - terminal.list
  - terminal.sendCommandOwned
---
A visible agent writes and fixes the plugin; you verify with Daintree's plugin actions (each asks the user first in a core pane). Only the REGISTERED project root's `.daintree/plugins/` is scanned (`worktree.list`: the main worktree's path): a plugin in another worktree loads only once its commit reaches the root, so reloading never tests a worktree copy. Say so up front.

Procedure:
1. Pick the mode from the request:
   - validation only ("validate this manifest"): `plugin.validate` with the directory, report `origin` and `errors` by path (`warnings` never block), stop.
   - state only ("why Staged", "why isn't it loading"): `plugin.diagnostics` (`pluginId` the manifest id, `logLimit:100`) gives `projectState`, `disabled`, `loadError` and the log tail; report and stop. Neither mode edits, builds, reloads or runs plugin commands.
   - author, change or repair: continue.

```json tool=plugin.validate
{"path":".daintree/plugins/<publisher>.<name>"}
```

2. Checkout: the root checkout, unless the user wants a branch (then say it cannot load before merge, and verify by validation only). An agent `working` there (`terminal.list`, `terminal.getStatus`): ask before launching beside it.
3. Launch one agent: the one the user named, else `claude`; `name:"Plugin: <name>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries what the plugin should do (or the failing state, validator errors and diagnostics) and:
   - "Read `docs/plugins/agent-brief.md` from the Daintree repository first (load rules, a zero-build skeleton), then `docs/plugins/project-local.md`. If you cannot reach them, stop and say so; never write a manifest from memory. A new plugin starts from that skeleton at `.daintree/plugins/<publisher>.<name>/`.";
   - "Daintree never builds: it loads the manifest and `dist/`. Build, and make every module the manifest references (entry, views, chunks, assets) exist and not be git-ignored (`git check-ignore -v <file>` prints nothing). Commit nothing unless this brief says commit (only when the user asked); else report `dist/` built, uncommitted.";
   - "Common causes of a button that does nothing: a `registerHandler` callback reading its payload from the first parameter (the signature is `(ctx, args)`); stale `dist/`; `host.dispatch` returning `{ok:false}` unchecked; an `actionId` not `{manifestId}.{commandId}`; a command declared but never registered in `activate()`, or the reverse; a missing capability.";
   - "Do not push, touch the user plugins directory or other plugins. Reply: files changed, the build command, and each manifest-referenced file with its tracked or ignored state."
4. End your turn.

On notice or resume:
1. `plugin.validate` yourself. Errors: fix the validator's actual `errors`, not a rule from memory (a caret engine range passes the schema; only a panel view's `id` must match a panel).
2. Check the referenced files yourself: `git check-ignore -v`; if committing was asked, `git ls-files` and `git status` show them committed with the source.
3. `plugin.reloadProject` and find the row by `id` (manifest name) or `dirName` (an unreadable manifest's row carries the directory):
   - `active`: go to 4.
   - `invalid`: its `error` goes back to the agent.
   - `staged`: an id this project never ran waits for the user's Activate in the plugin manager. Not broken: touch nothing.
   - `blocked`: not running. The cause may be project trust or this plugin being turned off; do not guess or call it staged: point the user to the plugin manager.
   Never click through for the user, or copy the plugin into the user plugins directory to dodge the gate.
4. An open panel after a view change: `plugin.reloadPanel` with its `panelId` from `terminal.list`. `scheduled` is a remount under way, `not-mounted` means nothing was open; a refusal for unsaved work stages a confirmation for the user and is not success.
5. Exercise it: run the command or open the panel (or ask the user to), then `plugin.diagnostics` for new errors. A reload that returned is not a plugin that works.
6. A fault: one corrective send with the validator errors or diagnostics, `notify:true, handback:true`; at most two rounds per fault, then report it.

Agent tools: an agent CLI takes its MCP servers at launch; one started before the plugin loaded, got agent access, or changed what it declares for agents must be relaunched to see them. Say so; never relaunch the user's agents yourself.

Report back: plugin id, directory and checkout; the agent's terminal id; the validate verdict (origin, errors by path); each referenced file built, tracked or ignored; state and error after reload; what you exercised and what happened, new diagnostics, anything untested; what the user must still do (merge to the root, trust, Activate, relaunch agents, commit dist).
