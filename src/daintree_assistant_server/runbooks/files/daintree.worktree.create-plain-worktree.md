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
---
`worktree.createWithRecipe` with no `recipeId` is Daintree's plain creator: checkout, config copy, submodules, background setup, no terminals. It needs no confirmation turn of yours. Never substitute `git worktree add` or a shell checkout: that skips Daintree's setup, and a checkout in the main worktree moves the user's branch.

1. Read `worktree.list`. Note the main worktree's branch. A branch already checked out in a worktree: report that worktree instead of creating another.
2. Pick the `source` mode; fields from another mode are rejected.
   - New branch: `{"kind":"newBranch","branchName":…,"baseBranch":…}` (base defaults to the main worktree's branch; the name must already be a valid ref). A remote base: `baseBranch:"origin/<b>"` plus `fromRemote:true`, which describes the base, not a reused remote branch.
   - Existing local branch: `{"kind":"existingBranch","branchName":…}` checks it out exactly as named, never suffixed, never replaced by a new branch if missing. Remote-only: offer a new local branch of that name from `origin/<name>` with `fromRemote:true`, and say so.
   - Pull request: `{"kind":"pullRequest","pullRequestNumber":N}` alone.
   - The default `collisionPolicy:"suffix"` may silently reuse an unchecked-out branch of that name or create `<name>-2`: pass `"collisionPolicy":"error"` when the user named a branch they expect to be new.
   - `issueNumber` only when the user tied it to an issue; then set `assignToSelf` as asked.
   Name up to you: pick one short valid name and state it. Ambiguous base: ask.
3. Create once:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<new branch>","baseBranch":"<verified base>","collisionPolicy":"error"}}
```

4. Keep `worktreeId` and `worktreePath`. Report any `requestedBranch`/`effectiveBranch` difference. An error is fixed in the arguments, never by raw git. A lost reply is reconciled with `worktree.list`: identical arguments within 120 seconds return the cached result, so a blind retry proves nothing.
5. `worktree.waitUntilReady` (`timeoutMs:25000`), at most three calls. `ready` is the only success; `timedOut:true` means still running at `stage`. `failed`/`timed-out`: report `stage` and `error`, never delete or recreate. `needs-approval`: the user approves setup commands in Daintree, not you. `unknown`: setup cannot be confirmed. API-key clients read `setupState` from `worktree.list` once.
6. Confirm with `worktree.list` that the new worktree appears and the main worktree's branch did not move (if it did, someone else moved it: report, don't revert). Stop: launch nothing.

Report back: worktree id and path, branch (requested and effective) with mode and base, setup state, that the main branch did not move, and that nothing was launched.
