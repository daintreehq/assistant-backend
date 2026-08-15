---
id: daintree.worktree.create-plain-worktree
title: Create a plain Daintree worktree
summary: Create a standalone Daintree worktree for a new or existing branch through the typed worktree wrapper, without launching a recipe or agent.
whenToUse: Use when the user asks to create, add, or set up a plain worktree for a new branch or an existing branch — "create a worktree", "make a new branch in its own worktree", or "check out this branch as a worktree" — and they do not want a recipe or agent launched. Do not use for a worktree with a workspace recipe (use daintree.recipe.run-or-create), for starting a forge issue with an agent (use daintree.workflow.start-work-on-issue), for spawning an agent into an existing worktree (use daintree.edits.spawn-visible-agent), or for deleting a worktree (use daintree.orchestration.workspace-cleanup).
risk: project
requiredTools:
  - context.snapshot
  - worktree.list
  - worktree.getCurrent
  - worktree.createWithRecipe
  - user.askMultipleChoice
---
Use when: the user wants a standalone worktree for a new or existing branch, with no recipe or agent required.

`worktree.createWithRecipe` is also the typed wrapper for PLAIN worktree creation: omit `recipeId` and it creates only the worktree. Use this wrapper directly even though its name mentions recipes.

Procedure:

1. Route specialized requests first. If the user wants a startup recipe, use `daintree.recipe.run-or-create`; if they want to begin a forge issue with an agent, use `daintree.workflow.start-work-on-issue`; if they only need an agent in a worktree that already exists, use `daintree.edits.spawn-visible-agent`.

2. Inventory existing worktrees with `worktree.list` (and `worktree.getCurrent` or `context.snapshot` when the active project is ambiguous). Check whether the requested branch is already checked out in another worktree. If it is, report that worktree instead of trying to create a duplicate checkout.

3. Settle the creation mode before mutating:
   - New branch off a local base: `branchName` plus its `baseBranch`.
   - New branch off a REMOTE base: the same, with `baseBranch` as the remote ref (`origin/main`) and `fromRemote: true`. `fromRemote` says the BASE is remote — it is not a switch for reusing a remote branch.
   - Reusing an EXISTING branch is not verified through this path. `useExistingBranch` exists in the schema, but the action asks for an available branch name first and suffixes one that is taken, so the reuse then looks for a branch that does not exist. Do not offer it as a mode: report what the branch is already checked out in (step 2), or create a new branch and say so.
   If the user says the name can be anything, choose one short sanitized branch name, state it, and proceed. If the base is ambiguous, ask rather than guessing.

4. Make ONE typed call. It mutates project state, so the harness confirms it at the active tier — do not ask for approval yourself on top of that. Every worktree field belongs inside `arguments`; only an optional `requestKey` is a top-level sibling.
   - New branch:

       worktree.createWithRecipe({
         "arguments": {
           "branchName": "<new-branch>",
           "baseBranch": "<base-branch>"
         }
       })

   - New branch off a remote base:

       worktree.createWithRecipe({
         "arguments": {
           "branchName": "<new-branch>",
           "baseBranch": "origin/<base-branch>",
           "fromRemote": true
         },
         "requestKey": "<stable-id-for-this-request>"
       })

   Omit `recipeId`; do not list or inspect recipes for a plain worktree. Pass a
   `requestKey` so a retry after a timeout cannot create a second worktree.

5. Use the typed `worktree.createWithRecipe` tool directly. Do NOT send this action through `daintree.call`, and do NOT improvise `git checkout`, `git branch`, or `git worktree add` in the main terminal. A raw checkout can move the main worktree onto the new branch, making the subsequent worktree creation fail because that branch is already in use.

6. Read the returned values: `{ worktreeId, worktreePath, branch, recipeLaunched, spawnedTerminalCount, failedTerminalCount, ... }`. For a plain worktree, `recipeLaunched` should be false and no recipe terminals should be expected. If the call fails, preserve the current worktree state, report the exact error, and correct the typed parameters rather than falling back to raw git.

7. Verify with `worktree.list` that the returned worktree id/path and branch now appear and that the original main worktree remains on its prior branch. Do not claim success from an intended branch name alone.

8. Stop after creation unless the user separately asks for an agent or recipe. A plain worktree has nothing to supervise.

Confirmation: creating a worktree and a branch mutates project state, and the wrapper is marked as such — the harness prompts at the active tier. Do not add a confirmation turn of your own.

Report back: the created worktree id/path, its branch and base or existing-branch mode, confirmation that the original worktree was not moved, and any failure or partial result. State explicitly that no recipe or agent was launched.
