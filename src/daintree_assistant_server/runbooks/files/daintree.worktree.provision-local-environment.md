---
id: daintree.worktree.provision-local-environment
title: Give each worktree its own database, port and local environment
summary: Diagnose checkouts colliding over one shared database, schema, port, docroot, container or dev server — migrations clobbering each other, a second copy failing to boot, one branch's seed data showing up in another — and get the project's worktree lifecycle setup and teardown scripts written so each worktree provisions its own stack and deleting it releases them.
whenToUse: 'Use for TWO related asks. First, a shared runtime resource that several worktrees collide over so branches cannot run side by side — "my worktrees all share one local site", "each branch needs its own database", "give every worktree its own URL", "two worktrees fight over port 3000". Second, configuring the project''s worktree lifecycle scripts themselves — "add a setup script that runs when a worktree is created", "a teardown script to drop the database when I delete a worktree", "provision new worktrees automatically", "new worktrees have no .env or installed dependencies". The tell is a per-worktree RESOURCE (database, port, hostname, docroot, container) or the setup/teardown configuration that creates it. Do not use to move agents that are already running into separate worktrees (use daintree.worktree.relocate-agents), to create a worktree (use daintree.worktree.create-plain-worktree), to launch a worktree''s startup terminals or dev server (use daintree.recipe.run-or-create), for a cloud devbox or remote resource (use daintree.worktree.manage-resource), to free a port some process holds right now (use daintree.runtime.free-busy-port), to migrate or reseed a database the worktree already has to itself (use daintree.runtime.migrate-worktree-database), or to RUN a test suite, check or command in a worktree that already works (use daintree.ci.run-local-checks, daintree.ci.run-e2e-suite when it needs the app running, or daintree.terminals.run-visible-command).'
risk: project
requiredTools:
  - worktree.list
  - agent.launch
optionalTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.list
  - terminal.sendCommandOwned
---
You read Daintree's worktree and setup state, settle the split with the user, and verify; a visible agent writes the scripts and config. Write them yourself only when the user asks you to.

Worktrees isolate code, nothing else. Daintree's answer is lifecycle commands in a `config.json`: `setup` (array of shell commands) runs on create, `teardown` on delete. Daintree takes the FIRST valid file, whole, in order: user-level `~/.daintree/projects/…`, the worktree's `.daintree/config.json`, the main checkout's. Nothing merges; an invalid file is skipped silently. Repository commands run only after the user approves them in Daintree (`needs-approval` until then, and again after edits); user-level commands run without approval. Commands get `DAINTREE_WORKTREE_PATH`, `DAINTREE_WORKTREE_NAME`, `DAINTREE_PROJECT_ROOT` and, when known, `DAINTREE_BRANCH`.

Before `setup` runs, a new worktree gets the main checkout's `.daintree/` folder and every file matching the main checkout's `.worktreeinclude` (gitignore patterns) that git ignores there: `.env`, local certificates. Existing files are kept; symlinks, files over 10 MB and anything past 100 MB or 1000 files are skipped. Dependencies are never copied: an install in `setup` does that.

Procedure:
1. Read `worktree.list`. One worktree: nothing collides yet, but configuring setup/teardown is still this job. A new worktree missing `.env`, other local files or its dependencies is this job too: patterns in `.worktreeinclude` (only ignored files copy: an unignored `.env` must be ignored first), an install command in `setup`. A clash between two projects is not this problem: say so.
2. See what exists: every config location (a user-level file silently overrides the repo's), compose files, env templates. A failing `setup` needs its failure read (`setupState`, `stage`, `error`, then the command's own output), not a new design. `needs-approval` is fixed by the user's approval, never by a manual run or a copy into the user-level file.
3. Propose the split per worktree (database or schema, host or port, docroot, cache prefix, lock and pid files) versus shared (read-mostly assets, dependency caches, base images), with a reason each; warn when proposing shared writable state. Settle it, the destination (repo config reaches the team, user-level stays local) and what teardown may destroy with the user.
4. With their go-ahead, launch one agent in the checkout that holds the config, `notify:true, handback:true`. Its prompt carries the split with reasons, the destination and the rules: names derived from `DAINTREE_WORKTREE_NAME` plus a short path hash, failing rather than falling back to a shared name when a variable is empty; teardown is required; setup records what it created in a marker file in the worktree, and a retry adopts or cleans only its own partial resources; teardown releases exactly what the marker names, refuses on a missing or mismatched marker instead of deleting by prefix, and never touches a shared resource; export or warn rather than drop data the user may want; never re-point the shared docroot, symlink or active copy at one branch; secrets are copied from the main checkout by `.worktreeinclude`, never generated, echoed or committed. Acceptance: setup and teardown run once by hand in one worktree's path with the variables set. Report: files written, commands run and results. No commit unless asked. End the turn.

On notice or resume:
1. A question within the settled split: answer once with `notify:true`; a new resource, destination or anything destructive goes to the user.
2. Read the diff against the rules above. Then prove it on ONE worktree: the agent's run, or, if agreed, a new worktree (`worktree.createWithRecipe`, then `worktree.waitUntilReady`) once the user has approved the repository commands. Check yourself that the included files and the resource exist, the app answers on its own address, and it serves THAT worktree's code: what the running process actually loaded (cwd, docroot target, a branch marker), not an exit 0.
3. Found the shared docroot already switched to one branch: report it and name the test results now suspect.
4. Existing worktrees are not provisioned retroactively: list which still need setup run once, or the included files copied. Remind the user to approve repository commands in Daintree.

Stop and recovery: one corrective round to the same agent (`notify:true`), then the user. Reconcile an ambiguous launch with `terminal.list`, never relaunch.

Report back: the collision and worktrees affected; the split with reasons; which config wins; what teardown releases and doesn't; what was written where and by which agent (terminal id); approvals still owed; the worktree verified and the evidence; worktrees still unprovisioned.
