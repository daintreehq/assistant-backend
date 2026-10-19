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
  - host.list
  - host.call
---
Worktrees isolate code, nothing else. Daintree's answer is `config.json` lifecycle commands: `setup` runs on create, `teardown` on delete. It takes the FIRST valid file, whole: user-level `~/.daintree/projects/…`, then the worktree's `.daintree/config.json`, then the main checkout's. Repository commands need the user's approval in Daintree (`needs-approval` until then, and after any change to them): unapproved setup runs nothing, and an unapproved teardown is skipped while the delete goes ahead, leaving its resources. User-level commands need none. Commands get `DAINTREE_WORKTREE_PATH`, `DAINTREE_WORKTREE_NAME`, `DAINTREE_PROJECT_ROOT` and `DAINTREE_BRANCH` when known: no host id, project id or port.

Identity: a worktree id is a path on one machine, so a path or a distinct worktree name proves nothing across hosts. The host and project owning a worktree own its config, approval, setup and teardown; git carries no ignored env files, credentials, runtimes or container state to another. Compare canonical endpoints across hosts, never secrets.
Ports are per machine: 3000 on two hosts is two ports. A database, cache, queue or bucket is its resolved endpoint: follow env files, aliases and tunnels (localhost may be a tunnel; one server may serve two hosts) to server plus database, schema or prefix, or provider plus resource id. Shared means another worktree, project or machine reaches it; unknown counts as shared.

On a host: every step runs there through `host.call`, per the hosts foundation.

Procedure:
1. Read `worktree.list`. One worktree still needs setup/teardown configured, or a missing `.env` or dependencies fixed: a new worktree gets the main checkout's `.daintree/` and its git-ignored `.worktreeinclude` files; an install in `setup` provides the rest. Another project on the same resource: report it.
2. See what exists: every config location and where each resource variable resolves. Read a failing `setup` (`worktree.waitUntilReady`: `setupState`, `stage`, `error`, then its output) before redesigning. `needs-approval` is cleared only by the user's approval, never by a manual run or a copy into the user-level file.
3. Propose the split per worktree (database or schema, port, docroot, cache prefix, lock files) versus shared, with a reason and resolved target each. A shared writable resource blocks destructive setup (migrate, reset, seed, create, drop): offer a target exclusive to this worktree, or stop. Settle it, the destination (repo config reaches the team, user-level stays local) and what teardown may destroy with the user.
4. With their go-ahead, launch one agent in the checkout that holds the config, `notify:true, handback:true`. Its prompt carries the split, the destination and the rules: names from `DAINTREE_WORKTREE_NAME` plus a hash of the host UUID (made once, kept outside git), project root and path, failing rather than falling back to a shared name; target resolved before any write, a shared or unresolved one refused; teardown required; setup records an owner (that triple) and each resource made with its target in a marker file on that host outside the checkout, (a skipped teardown leaves it); a retry adopts or cleans only what the marker names for that owner; teardown releases exactly that, refuses on a missing or mismatched marker instead of deleting by prefix, never touches a shared resource, volume or service; never re-point a shared docroot or active copy at one branch; secrets stay host-local, never generated, echoed, committed or sent to another host. Acceptance: setup run once (the app's own run once approved, else one manual run with the variables set), handed back live; teardown only after you check it. No commit unless asked.

On notice or resume:
1. Read the diff against the rules above, then prove it on ONE worktree: the agent's run, or, if agreed, a new one (`worktree.createWithRecipe`, `worktree.waitUntilReady` on the same id; `timedOut`: wait again, never recreate) once approved. Check the included files and resource exist, its target matches the marker and no other worktree's or host's, and the app serves THAT worktree's code, per what the process actually loaded (cwd, docroot target, branch marker), not an exit 0.
2. Existing worktrees are not provisioned retroactively: list which need setup, by host; approvals are the user's, on the owning host.

Stop and recovery: one corrective round to the same agent, then the user.

Report back: worktrees and collision; each resource's target and who else reaches it; the split; winning config; what teardown releases and doesn't; files, agent (terminal id); approvals owed; the worktree verified, with evidence; worktrees unprovisioned, by host.
