---
id: daintree.worktree.create-plain-worktree
title: Create a plain Daintree worktree
summary: Create a standalone Daintree worktree for a new or existing branch with Daintree's own worktree creator, without launching a recipe or agent.
whenToUse: Use when the user asks to create, add, or set up a plain worktree for a new branch or an existing branch — "create a worktree", "make a new branch in its own worktree", or "check out this branch as a worktree" — and they do not want a recipe or agent launched. Do not use for a worktree with a workspace recipe (use daintree.recipe.run-or-create), for starting a forge issue with an agent (use daintree.workflow.start-work-on-issue), for spawning an agent into an existing worktree (use daintree.edits.spawn-visible-agent), for giving agents that are ALREADY RUNNING one worktree each (use daintree.worktree.relocate-agents), or for deleting a worktree (use daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
optionalTools:
  - actions.getContext
---
Use when: the user wants a standalone worktree for a new or existing branch, with no recipe or agent.

Availability: Core. `worktree.createWithRecipe` without `recipeId` is Daintree's plain creator despite its name: it creates the checkout, copies project config, initialises submodules and starts the project's setup in the background, and launches no terminals. It runs without an approval; do not add a confirmation turn of your own. No agent, notice or checkpoint is involved. An API-key client has no `worktree.waitUntilReady`: read `setupState` from `worktree.list` instead. Do not substitute `git worktree add` or a `git checkout` in the shell for a Daintree worktree: that skips Daintree's config copy and setup, and a checkout in the main worktree moves the user's branch.

Procedure:
1. Route first. A startup recipe is daintree.recipe.run-or-create; starting a forge issue with an agent is daintree.workflow.start-work-on-issue; an agent in a worktree that already exists is daintree.edits.spawn-visible-agent.
2. Read `worktree.list` (`id`, `path`, `branch`, `isMain`, `setupState`). Note the main worktree's branch so you can show later that it did not move. If the requested branch is already checked out in a worktree, report that worktree instead of creating another: git cannot check one branch out twice. Use `actions.getContext` only when the project itself is ambiguous.
3. Settle the `source` mode before creating. Each mode is strict: a field from another mode is rejected.
   - New branch: `{"kind":"newBranch","branchName":…,"baseBranch":…}`. Pass the base the user named; omitted, it defaults to the main worktree's branch. The name must already be a valid git ref: nothing rewrites it.
   - New branch off a remote base: `baseBranch` as the remote ref (`origin/develop`) plus `fromRemote:true`. `fromRemote` describes the BASE; it does not reuse a remote branch.
   - Existing local branch: `{"kind":"existingBranch","branchName":…}` checks it out exactly as named, never suffixed, and never replaced by a new branch if it is missing. For a branch that exists only on the remote, offer a new local branch of the same name based on `origin/<name>` with `fromRemote:true`, and say that is what you did.
   - Pull request: `{"kind":"pullRequest","pullRequestNumber":N}` alone, never with a branch name. A closed or merged PR is accepted while its head ref exists.
   - Collisions: the default `collisionPolicy:"suffix"` may quietly REUSE an existing branch of that name that nothing has checked out, with whatever commits it holds, or create `<name>-2`. Pass `"collisionPolicy":"error"` whenever the user named a branch they expect to be new; keep `suffix` for when they said the name can be anything.
   - Pass `issueNumber` only when the user tied the worktree to an issue; it does not attach the issue. With it, set `assignToSelf` to what they asked, because omitting it applies their saved preference.
   If the name is up to you, pick one short valid name and state it. If the base is ambiguous, ask.
4. Create once, with no `recipeId`:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<new branch>","baseBranch":"<verified base>","collisionPolicy":"error"}}
```

```json tool=worktree.createWithRecipe
{"source":{"kind":"existingBranch","branchName":"<existing local branch>"}}
```

5. Read the result. Keep `worktreeId` and `worktreePath` verbatim, both of them. Compare `requestedBranch` with `effectiveBranch` (they differ only after a suffix collision) and report a difference plainly. `recipeLaunched` should be false, with no spawned terminals. Its `setupState` is a snapshot from when git finished, not the current state.
   - An error is reported as given and fixed in the arguments (an invalid ref, a taken name under `error`). Never fall back to raw git.
   - A lost or ambiguous reply is reconciled with `worktree.list` (look for the branch and path) before anything else. Identical arguments within 120 seconds return the cached earlier result, success or failure, without creating anything, so a blind retry proves nothing.
6. Check setup before calling the checkout ready. Setup keeps running after creation, and work started early can run against an unpopulated tree:

```json tool=worktree.waitUntilReady
{"worktreeId":"<returned worktreeId>","timeoutMs":25000}
```

   - `ready` is the only success.
   - `pending` or `running` with `timedOut:true` is not a failure: call again, at most three calls in all, then report the worktree as created with setup still running at `stage`.
   - `failed` or `timed-out`: report `stage` and `error`. The worktree exists; do not delete or recreate it.
   - `needs-approval`: the repository's setup commands are waiting for the user to approve them in Daintree. You cannot approve them; say so.
   - `unknown`: this Daintree did not create it, or has restarted since; setup cannot be confirmed.
7. Confirm with `worktree.list` that the new id, path and branch appear and that the main worktree is still on its earlier branch.
8. Stop. A creation-only request needs no agent, terminal or notice.

Report back: the worktree id and path, its branch (requested and effective if they differ) with its mode and base, the setup state (ready, still running at a stage, failed with its error, or awaiting the user's approval), that the original worktree's branch did not move, and that no recipe or agent was launched.
