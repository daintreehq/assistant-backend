---
id: daintree.workflow.prepare-feature-demo
title: Prepare a demo of what the agents built
summary: Make a finished feature inspectable by a person — the right worktree at a recorded revision, set up and running in a preview or visible terminal, safe sample data in that checkout's own database, a short walkthrough, and an honest works, stubbed and missing list checked by exercising the steps.
whenToUse: 'Use when the user wants to SEE or SHOW a feature the agents built — "show me what the agents built", "set up a demo of the new onboarding flow", "get the feature running so I can click through it", "prepare a demo for the standup", "get it into a state I can show the team". The deliverable is a running, reproducible demonstration: the right worktree (the integrated branch when several agents built it), setup done, a preview or app running, safe sample data seeded only in that checkout''s own local database, a walkthrough of steps and what each shows, and a plain list of what works, what is stubbed and what is missing. Do not use to try a pull request''s head (use daintree.git.test-pull-request-locally), only to open, look at or fix a Dev Preview (use daintree.runtime.debug-web-app), only to start a server in a terminal (use daintree.terminals.run-visible-command), to check one agent''s done claim (use daintree.orchestration.verify-agent-work), for a written report of the work done (use daintree.git.summarize-activity), when seeding a worktree''s database is the whole ask (use daintree.runtime.migrate-worktree-database), or to run a saved workspace recipe (use daintree.recipe.run-or-create).'
risk: project
requiredTools:
  - worktree.list
  - terminal.list
  - terminal.getStatus
  - agent.launch
optionalTools:
  - browser.getConsoleMessages
  - devPreview.reloadPreview
  - recipe.list
  - recipe.run
  - terminal.new
  - terminal.sendCommandOwned
  - terminal.notifyWhenIdle
  - devPreview.restart
  - worktree.setActive
---
The person needs to see it work and to know what is real. A prep agent makes the worktree runnable, seeds safe data and drafts the walkthrough; you start the demo, exercise what you can and hand it over. Nothing is merged, pushed or deployed, and feature code does not change during prep.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Pick the checkout: `worktree.list` (branch, linked issue or PR), then `terminal.list` and `terminal.getStatus` for agents there. Several agents' branches: the branch integrating them; none yet: ask whether to show each alone or integrate first (daintree.git.integrate-agent-branches). An agent still `working` there: `terminal.notifyWhenIdle` on it; unarmed, report the demo pending. Record `git -C <path> rev-parse HEAD` as <sha>: the demo binds to it. Never demo the main checkout by default.
2. Launch one prep agent in that `worktreeId`, `name:"Demo prep: <feature>"`, `notify:true, handback:true`. The brief:
   - "Make <feature> runnable here at <sha> with the project's own install and setup commands. Do not start the dev server.";
   - "Sample data only in this checkout's own local database: prove it is not shared with the main checkout or another worktree (compare the resolved server and database, or SQLite's real path); never staging or production. Use the project's seeders or fixtures with invented data: no real people, emails, payment details or credentials. Unproven: seed nothing and say so. Record what you seeded and how to reset it.";
   - "Change no feature code; commit and push nothing. A blocker is reported with its evidence, not fixed.";
   - "Walkthrough, 3 to 8 steps, each: where (URL path, screen or command), what to do, what the viewer should see. Then: works (exercised by you, how), stubbed or hardcoded (file:line), missing, known bugs.";
   - "Reply: setup commands and results, the seed record, the dev server command and port, the walkthrough and that list."
3. The notice is not the end (nothing runs yet): check its claims: HEAD is still <sha>; `git -C <path> diff <sha> --stat` and `status --porcelain --untracked-files=all` show nothing beyond setup artefacts; the seed record names a database the agent proved local.
4. Start the demo where the user sees it:
   - Web app: a `kind:"dev-preview"` panel already in that worktree (`terminal.list` with the `worktreeId`) gets `devPreview.restart` with its `panelId` if prep changed dependencies or env, else `devPreview.reloadPreview`; none there: launch one and keep its `terminalId`. It starts only while its worktree is shown: `worktree.setActive` on it and say so. It runs the project's configured dev server command; none configured, or the port held by another checkout, is the user's to settle (daintree.runtime.free-busy-port).

```json tool=agent.launch
{"agentId":"dev-preview","worktreeId":"<worktree id>","name":"Demo: onboarding"}
```

   - A recipe whose terminals are exactly the demo's processes, no agents (`recipe.list`): `recipe.run` with the `worktreeId`; keep the spawned ids; compare what started with what it defines; never re-run a partial start.
   - CLI or API: `terminal.new` with `cwd` = the worktree path, then `terminal.sendCommandOwned` for the server or first command.
5. A panel is not a server, and Daintree picks the preview's port: find the listener whose cwd is <path> (`lsof -nP -iTCP -sTCP:LISTEN`, then `lsof -a -p <pid> -d cwd`) and probe it, bounded (`curl --max-time 5`); none: report it unavailable, never start one yourself. Then exercise what you can: `browser.getConsoleMessages` (`terminalId` = the preview panel id, `limit:100`) for errors on load; API steps and every seeded record's page with `curl`; CLI steps from the worktree; the seeded rows with one read query. A step needing clicks or looks is "not exercised by me" without real pixels from your browser tool, which reaches a host's server only through a forward. A failing step goes to known bugs; fixing a console error is daintree.runtime.debug-web-app.

On notice or resume: the prep agent's "works" is a claim until step 5 shows it. A question within scope: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; seeding an unproven database or changing code goes to the user.

Stop and recovery: one corrective round to the prep agent for a setup failure; then hand over what stands. A preview panel never sends a notice: do not end the turn waiting on it. Never launch a second preview. Leave everything running; stop only on request.

Report back: worktree, branch and the revision shown; how to open the demo (panel or terminal id, URL); what was seeded, where, and how to reset it; the walkthrough; works (exercised by you or claimed by the agent, kept apart), stubbed, missing, known bugs; what is running and the prep agent's terminal id.
