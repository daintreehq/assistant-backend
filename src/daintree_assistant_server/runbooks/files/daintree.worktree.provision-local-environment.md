---
id: daintree.worktree.provision-local-environment
title: Give each worktree its own database, port and local environment
summary: Diagnose checkouts colliding over one shared database, schema, port, docroot, container or dev server — migrations clobbering each other, a second copy failing to boot, one branch's seed data showing up in another — and get the project's worktree lifecycle setup and teardown scripts written so each worktree provisions its own stack and deleting it releases them.
whenToUse: 'Use for TWO related asks. First, a shared runtime resource that several worktrees collide over so branches cannot run side by side — "my worktrees all share one local site", "each branch needs its own database", "give every worktree its own URL", "two worktrees fight over port 3000". Second, configuring the project''s worktree lifecycle scripts themselves — "add a setup script that runs when a worktree is created", "a teardown script to drop the database when I delete a worktree", "provision new worktrees automatically". The tell is a per-worktree RESOURCE (database, port, hostname, docroot, container) or the setup/teardown configuration that creates it. Do not use to move agents that are already running into separate worktrees (use daintree.worktree.relocate-agents), to create a worktree (use daintree.worktree.create-plain-worktree), to launch a worktree''s startup terminals or dev server (use daintree.recipe.run-or-create), for a cloud devbox or remote resource (use daintree.worktree.manage-resource), or to RUN a test suite, check or command in a worktree that already works (that is an ordinary check run, not a provisioning problem).'
risk: project
requiredTools:
  - context.snapshot
  - worktree.list
  - worktree.getCurrent
  - fs.read
  - fs.list
  - fs.search
  - user.askMultipleChoice
  - daintree.call
---
Use when: the user has several worktrees and one of something they all need — a database, a docroot, a port, a dev server — so only one branch can actually be run or tested at a time.

Worktrees isolate CODE. They isolate nothing else. A second checkout does not bring a second database, a second hostname or a second free port, and this is the point at which parallel agents stop being useful: every agent can edit its own branch, but only one of them can look at the result. Daintree's answer is the project's own worktree lifecycle scripts — a `setup` command run when a worktree is created and a `teardown` command run when it is deleted — so a new worktree arrives already runnable and removing it takes its resources with it.

**You cannot write those files yourself.** `fs.read`, `fs.list` and `fs.search` are read-only; there is no write tool. Everything below either hands the user exact content, or delegates the writing to an agent — never pretends a file appeared.

Procedure:

1. CONFIRM THE SHAPE before proposing anything. The symptom is several worktrees and one runnable thing. Read `worktree.list` for what exists and each entry's setup/lifecycle state, and `worktree.getCurrent` or `context.snapshot` when the project is ambiguous. If there is only one worktree, or the collision is between two projects rather than two branches, this is not that problem — say so.

2. FIND WHAT IS ALREADY CONFIGURED rather than assuming nothing is. `fs.list` the project root for a `.daintree/` directory and `fs.read` its `config.json` if present; `fs.search` finds a compose file, an env template or an existing provisioning script. A project can also carry user-level configuration outside the repo. Project settings themselves have no typed tool — reach them through the foundation's discovery/schema/invoke path rather than assuming a wrapper exists. Report what you found. A project that already has a `setup` command and a failing worktree is a different problem from a project that has none — the first needs the failure read, not a new design.

3. NAME WHAT MUST BE PER-WORKTREE AND WHAT SHOULD STAY SHARED, and say why for each. This is the whole design and it is cheap to get wrong in both directions:
   - Per-worktree, because they collide: the database or schema, the hostname or port, the docroot or served directory, any cache keyspace or prefix, any lockfile or pidfile.
   - Shared, because copying is waste: large read-mostly assets, upload or media directories, dependency caches, base images.
   - Derive per-worktree names from the worktree's own path or branch so nothing has to be edited per worktree. Ask the user for the one or two facts you cannot read — which local stack serves the project, and which resources they consider safe to share.
   Shared mutable state is a real cost, not a free win: a branch that rewrites shared uploads or shared fixtures affects every other worktree. Say that plainly when you propose sharing something writable.

4. HAND OVER THE SCRIPTS, or get an agent to write them. Two honest routes; offer the one that fits and let the user choose with `user.askMultipleChoice` when both do:
   - Give the user the exact `setup` and `teardown` command lines and the script bodies, and say where they go — the project's `.daintree/config.json` for settings the whole team should get, or the user-level project config for a machine-specific one.
   - Or spawn an agent to write them in the project worktree with `daintree.edits.spawn-visible-agent`, briefed with the design from step 3. That is the route when the scripts need to discover the local stack themselves. The agent writes; you still verify.
   Teardown is not optional. A setup script that creates a database and a teardown that does not drop it turns every deleted worktree into litter the user finds months later.

5. PROVE IT ON ONE WORKTREE BEFORE FANNING OUT. Run the setup path for a single worktree and check the result end to end: the resource exists, the app answers on its own address, and it is serving THAT worktree's code rather than another one's. Resolve the path the running app actually loaded; do not infer it from the script having exited zero. A setup script that half-succeeds leaves a worktree that looks provisioned and serves the wrong branch, which is worse than one that failed outright.

6. NEVER MAKE ROOM BY SWITCHING THE SHARED ONE OVER. Deactivating the shared app's copy and pointing it at one branch is the reflex, and it silently breaks every other agent: their tests then exercise the switched-in code and pass or fail for reasons that have nothing to do with their branch. If a worktree cannot be provisioned yet, say it cannot be tested yet. If you find the shared environment already switched over — a swapped symlink, a re-pointed docroot, an activated variant — report it before anyone reads a test result, and say which agents' results are now suspect.

7. CLOSE THE LOOP ON THE EXISTING WORKTREES. Worktrees created before the scripts existed do not retro-provision themselves; each one needs the setup path run once, by hand or by whatever the project now offers. List which are provisioned and which are not rather than implying the fix reached all of them.

Report back: the collision you found and which worktrees it affected; the per-worktree and shared split with the reason for each; where the scripts and configuration were written and BY WHOM (the user, or a named agent and its terminal id); the single worktree you verified and the evidence you verified it with; which existing worktrees are still unprovisioned; and anything the user must still create themselves.
