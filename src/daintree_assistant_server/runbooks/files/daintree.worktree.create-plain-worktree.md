---
id: daintree.worktree.create-plain-worktree
title: Create a plain Daintree worktree
summary: Create a standalone Daintree worktree for a new or existing branch with Daintree's own worktree creator, without launching a recipe or agent.
whenToUse: Use when the user asks to create, add, or set up a plain worktree for a new branch or an existing branch — "create a worktree", "make a new branch in its own worktree", or "check out this branch as a worktree" — and they do not want a recipe or agent launched. Do not use for a worktree with a workspace recipe (use daintree.recipe.run-or-create), for starting a forge issue with an agent (use daintree.workflow.start-work-on-issue), for spawning an agent into an existing worktree (use daintree.edits.spawn-visible-agent), for giving agents that are ALREADY RUNNING one worktree each (use daintree.worktree.relocate-agents), for deleting a worktree (use daintree.orchestration.workspace-cleanup), to rebase or update an existing branch onto its base (use daintree.git.sync-with-base), or when a PR is to be run or tried, not just checked out (use daintree.git.test-pull-request-locally).
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
optionalTools:
  - worktree.list
---
`worktree.createWithRecipe` with no `recipeId` is Daintree's plain creator: checkout, config copy, submodules, background setup, no terminals. It needs no confirmation turn of yours. Never substitute `git worktree add` or a shell checkout: that skips Daintree's setup, and a checkout in the main worktree moves the user's branch.

On a host: call these tools on the work's machine; an agent there runs any shell step.

1. Pick the `source` mode; fields from another mode are rejected. A new branch off the default base needs no listing first: the base defaults to the main worktree's branch and `collisionPolicy:"error"` refuses a taken name. Read `worktree.list` only for an existing branch (one already checked out in a worktree: report that worktree instead of creating another) or a base you must check.
   - New branch: `{"kind":"newBranch","branchName":…}`, plus `baseBranch` only when the user named one (the name must already be a valid ref). A remote base: `baseBranch:"origin/<b>"` plus `fromRemote:true`, which describes the base, not a reused remote branch.
   - Existing local branch: `{"kind":"existingBranch","branchName":…}` checks it out exactly as named, never suffixed, never replaced by a new branch if missing. Remote-only: offer a new local branch of that name from `origin/<name>` with `fromRemote:true`, and say so.
   - Pull request: `{"kind":"pullRequest","pullRequestNumber":N}` alone.
   - The default `collisionPolicy:"suffix"` may silently reuse an unchecked-out branch of that name or create `<name>-2`: pass `"collisionPolicy":"error"` for a new branch.
   - `issueNumber` only when the user tied it to an issue; then set `assignToSelf` as asked.
   Name up to you: pick one short valid name and state it. Ambiguous base: ask.
2. Create once:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"<new branch>","collisionPolicy":"error"}}
```

3. Keep `worktreeId` and `worktreePath`; `requestedBranch`/`effectiveBranch` report the branch, so no confirming listing is needed. An error is fixed in the arguments, never by raw git. A lost reply is reconciled with `worktree.list`: identical arguments within 120 seconds return the cached result, so a blind retry proves nothing.
4. `worktree.waitUntilReady` (`timeoutMs:25000`), at most three calls. `ready` is the only success; `timedOut:true` means still running at `stage`. `failed`/`timed-out`: report `stage` and `error`, never delete or recreate. `needs-approval`: the user approves setup commands in Daintree, not you. `unknown`: setup cannot be confirmed. API-key clients read `setupState` from `worktree.list` once. Stop: launch nothing.

Report back: worktree id and path, branch (requested and effective) with mode and base, setup state, and that nothing was launched.
