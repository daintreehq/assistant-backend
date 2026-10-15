---
id: daintree.plugins.develop-project-plugin
title: Build, load and debug a Daintree project plugin
summary: Take a project plugin under .daintree/plugins/ from written to verifiably loaded — validate its manifest, keep its built dist/ committed, reload and read its real state, and diagnose one that is invalid, staged, blocked, inert or whose buttons do nothing.
whenToUse: 'Use when the user or an agent is writing, changing or debugging a Daintree PROJECT PLUGIN, a manifest plus committed dist/ under the project''s .daintree/plugins/ — "build a Daintree plugin panel for our deploy board", "I changed the plugin, get Daintree to pick it up", "my plugin isn''t loading", "the plugin panel is blank", "what does my plugin''s panel look like", "show me how the plugin view renders", "the button in my plugin does nothing", "validate this plugin manifest", "why does the plugin manager say Unreadable or Staged", "my agent can''t see the plugin''s tools". The deliverable is the plugin active and exercised (a panel seen through its preview where it has one), or its exact state and the reason it is not. Do not use for Daintree itself misbehaving with no plugin of the user''s involved (use daintree.diagnostics), for a web app in a Dev Preview panel (use daintree.runtime.debug-web-app), for running a workspace recipe (use daintree.recipe.run-or-create), or for finding and following a skill a plugin contributes (use daintree.skills.use-plugin-skill).'
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
Plugin actions ask first in a core pane. Only the REGISTERED project root's `.daintree/plugins/` is scanned (`worktree.list`: the main worktree): a worktree copy loads once its commit reaches the root. Say so up front.

No Daintree action shows a panel. Never ask for a Daintree relaunch with debug flags, or attach Playwright or any CDP client to it: unsupported, and a debug port exposes every terminal.

Procedure:
1. Pick the mode from the request:
   - validation only ("validate this manifest"): `plugin.validate` with the directory; report `origin` and `errors` by path, stop.
   - state only ("why Staged", "why isn't it loading"): `plugin.diagnostics` (`pluginId` the manifest id, `logLimit:100`); report its state, `loadError` and log tail, stop.
   - look only ("what does the panel look like"): a `test:preview` script in the plugin's `package.json`: steps 2-3 with only the preview bullet, editing no source; none: `plugin.diagnostics`, then ask the user to open the panel and say what they see.
   - author, change or repair: continue. Only this mode edits, builds, reloads or runs plugin commands.

```json tool=plugin.validate
{"path":".daintree/plugins/<publisher>.<name>"}
```

2. Checkout: the root, unless the user wants a branch (it cannot load before merge: verify by validation and preview). An agent `working` there (`terminal.list`): ask before launching beside it.
3. Launch one agent (the user's pick, else `claude`): `name:"Plugin: <name>"`, the `worktreeId`, `notify:true, handback:true`; the prompt carries the goal (or failing state, validator errors, diagnostics) and:
   - "Read `docs/plugins/agent-brief.md` from the Daintree repository first; unreachable: stop and say so, never write a manifest from memory.";
   - "Daintree never builds; it loads the manifest and `dist/`. Build, and make every module the manifest references exist and not be git-ignored (`git check-ignore --no-index <file>` prints nothing). Commit only if this brief says so; else report `dist/` built, uncommitted.";
   - "A dead button: that brief's 'When a button does nothing' list, in order.";
   - "To see a panel: `npm run test:preview` (once: `npx playwright install chromium`), then read the PNGs under `test-results/`; report any failure verbatim. No such script: say so, add none unasked. Never drive or attach to Daintree.";
   - "Do not push or touch the user plugins directory or other plugins. Reply: files changed, the build command, each referenced file tracked or ignored, what each screenshot shows."
4. End your turn.

On notice or resume (look only: relay the screenshots and stop, or the preview's failure, then the no-preview path):
1. `plugin.validate` yourself. Errors: step 6 with the validator's actual `errors`, not a rule from memory (a caret engine range passes the schema; only a panel view's `id` must match a panel).
2. `git check-ignore --no-index` the referenced files yourself; if committing was asked, `git status` and `git ls-files` show them committed.
3. `plugin.reloadProject` and find the row by `id` (manifest name) or `dirName` (an unreadable manifest's row carries the directory):
   - `active`: go to 4.
   - `invalid`: its `error` goes back to the agent.
   - `staged`: an id the project never ran waits for the user's Activate in the plugin manager; not broken, touch nothing.
   - `blocked`: not running (project trust, or turned off); do not guess or call it staged: point the user to the plugin manager.
   Never click through for the user or copy the plugin to the user plugins directory to dodge the gate.
4. An open panel after a view change: `plugin.reloadPanel` (`panelId` from `terminal.list`): `scheduled` is remounting, `not-mounted` no view was mounted; an unsaved-work refusal is not success.
5. Exercise it: run the command, then `plugin.diagnostics` (host-logger lines, not view errors). A view: the preview screenshots (mock host: activation and live data need Daintree); none, ask the user to open the panel and say what they see (a crashed view's pane offers Copy diagnostics) and take their answer to 6. A reload that returned is not a plugin that works.
6. A fault: one corrective send with the validator errors or diagnostics, `notify:true, handback:true`; at most two rounds per fault, then report it.

Agent tools: a CLI reads its MCP servers at launch, so agents started before the plugin loaded, got agent access or changed its tools need a relaunch; say so, never relaunch them yourself.

Report back: plugin id, directory, checkout; validate verdict (origin, errors by path); referenced files built, tracked or ignored; state and error after reload; what was exercised and seen (preview or the user), new diagnostics, what is untested; what the user must still do (merge to the root, trust, Activate, relaunch agents, commit dist).
