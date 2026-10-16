---
id: daintree.plugins.develop-project-plugin
title: Build, load and debug a Daintree project plugin
summary: Take a project plugin under .daintree/plugins/ from written to verifiably loaded — validate its manifest, keep its built dist/ committed, reload and read its real state, and diagnose one that is invalid, staged, blocked, inert or whose buttons do nothing.
whenToUse: 'Use when the user or an agent is writing, changing or debugging a Daintree PROJECT PLUGIN, a manifest plus committed dist/ under the project''s .daintree/plugins/ — "build a Daintree plugin panel for our deploy board", "I changed the plugin, get Daintree to pick it up", "my plugin isn''t loading", "the plugin panel is blank", "what does my plugin''s panel look like", "show me how the plugin view renders", "the button in my plugin does nothing", "validate this plugin manifest", "why does the plugin manager say Unreadable or Staged", "my agent can''t see the plugin''s tools", "my plugin works here but is missing on the remote host", "the host loaded the plugin but the Shell refuses its panel". The deliverable is the plugin active and exercised (a panel seen through its preview where it has one), or its exact state and the reason it is not, including, for a project or installed plugin on a Remote Host, which gate holds: plugin absent, incompatible or untrusted there, or the Shell''s own consent to its panel. Do not use for Daintree itself misbehaving with no plugin of the user''s involved (use daintree.diagnostics), for a web app in a Dev Preview panel (use daintree.runtime.debug-web-app), for running a workspace recipe (use daintree.recipe.run-or-create), for adding or connecting a machine as a Remote Host (use daintree.hosts.connect-host), for copying this machine''s settings to a host (use daintree.hosts.copy-portable-settings), or for finding and following a skill a plugin contributes (use daintree.skills.use-plugin-skill).'
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
Only the registered project root's `.daintree/plugins/` loads (`worktree.list`: the main worktree).

No Daintree action shows a panel. Never relaunch Daintree with debug flags or attach Playwright or a CDP client to it: a debug port exposes every terminal.

Procedure:
1. Pick the mode from the request:
   - validation or state only: `plugin.validate` (`path` the plugin directory; `origin`, `errors` by path) or `plugin.diagnostics` (`pluginId` the manifest id, `logLimit:100`; state, `loadError`, log tail); stop.
   - look only: a `test:preview` script (`package.json`) gives steps 2-3 with only the preview bullet, no source edits; else, or after a failed preview, `plugin.diagnostics`, then the user opens the panel and says what they see.
   - absent, not running or refused on a remote host: Remote host below, no edits; a requested repair is author mode.
   - author, change or repair: continue. Only it edits, builds, reloads or runs plugin commands.
2. Checkout: the root, unless the user wants a branch (a worktree copy loads once its commit reaches the root: say so; verify by validation and preview). A `working` agent there (`terminal.list`): ask before launching beside it.
3. Launch one agent (the user's pick, else `claude`): `name:"Plugin: <name>"`, the `worktreeId`, `notify:true, handback:true`; the prompt carries the goal (or failing state, validator errors, diagnostics) and:
   - "Read `docs/plugins/agent-brief.md` in the Daintree repository first (a dead button: its 'When a button does nothing' list); unreachable: stop, never write a manifest from memory.";
   - "Daintree never builds, it loads the manifest and `dist/`: build, and every module the manifest references must exist and not be git-ignored (`git check-ignore --no-index <file>` prints nothing). Commit only if told; else report `dist/` built, uncommitted.";
   - "To see a panel: `npm run test:preview`, then read the PNGs in `test-results/`; report a failure verbatim. No such script: say so, add none unasked. Never drive or attach to Daintree.";
   - "Do not push or touch the user plugins directory or other plugins. Reply: files changed, build command, each referenced file tracked or ignored, what each screenshot shows."

On notice or resume (look only: relay screenshots and stop; a failed preview: the no-preview path):
1. `plugin.validate`, and `git check-ignore --no-index` on the referenced files; if committing was asked, `git status` and `git ls-files` show them committed. Errors: step 5 with its actual `errors`, not a rule from memory.
2. `plugin.reloadProject`, the row by `id` or, for an unreadable manifest, `dirName`: `active`: step 3; `invalid`: its `error` goes back to the agent; `staged`: a new id waits for the user's Activate; `blocked`: not running (trust, or off), the plugin manager is the user's.
3. After a view change, an open panel: `plugin.reloadPanel` (`panelId` from `terminal.list`); `not-mounted` or an unsaved-work refusal is not success.
4. Exercise it: run a changed command the request covers, then `plugin.diagnostics` (log lines, not view errors). A view: the preview screenshots; none, the user opens the panel and says what they see, then 5. A reload that returned proves nothing.
5. A fault: one corrective send with the validator errors or diagnostics, `notify:true, handback:true`; at most two rounds per fault, then report it.

Remote host: code, settings, secrets and trust live on the host; only the panel renders on the Shell. `plugin.diagnostics` reads the host (not found: not running, not proof of absence), but a host-side session gets NOT_FOUND from `plugin.*` while a Shell drives the project. No action reads the Shell's side; its panel's message does (the person relays it):
- "isn't installed on <host>": a project plugin comes with the repository (commit and `dist/` in the host's project root; staged awaits Activate, blocked is host trust); an installed one: Settings → Hosts → <host> → Plugins gives the reason it can't run, and Install or Update (never a downgrade) is theirs to click. Settings and secrets are never copied.
- "shows in the window driving this project": Take control is theirs to click.
- "blocked on this computer": the Shell has not allowed that exact bundle (or could not check it), apart from host trust: their yes in a native dialog; reset in Settings → Hosts → <host> → Plugin views, then Try again. Never widen trust or answer for them.

Report back: plugin id, directory, checkout (or host); validate verdict; referenced files built, tracked or ignored; state and error after reload; what was exercised and seen, what is untested; on a host, which gate holds; what the user must still do (merge, trust, Activate, install on the host, take control, allow the view, commit dist, relaunch agents that predate the plugin, its agent access or declarations).
