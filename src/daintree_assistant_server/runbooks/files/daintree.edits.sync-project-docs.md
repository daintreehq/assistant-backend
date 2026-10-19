---
id: daintree.edits.sync-project-docs
title: Bring the project's docs in line with its code
summary: Have one agent rewrite the project's README, setup, API or configuration docs from what the code actually does, then a second agent follow them literally from a clean checkout and run the examples, and fix or report what fails.
whenToUse: 'Use when the project''s OWN documentation files no longer match what its code does and must be rewritten to match — "the README setup steps are out of date, fix them", "update the API docs for the new flags", "make sure our docs match what the CLI actually does now", "the config reference has been wrong since the refactor", "our install guide doesn''t work any more". The deliverable is corrected README, setup, API, CLI or configuration docs in the repository, written from the implementation by one agent, then followed literally by a second agent that runs the examples, with what failed fixed or reported. Do not use for release notes or a CHANGELOG entry for a version (use daintree.forge.draft-release-notes), for standing instructions to agents in AGENTS.md or CLAUDE.md (use daintree.memory.manage-project-memory), for a one-line typo fix with nothing to run (use daintree.edits.spawn-visible-agent), or to have the code explained rather than documented (use daintree.explore.repository).'
risk: project
requiredTools:
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
  - terminal.getStatus
---
The code is the truth and only the docs change, unless the user says the behaviour is the bug. A writer works from the implementation, a separate reader follows the result without the code, and you check both.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Scope: the doc files (README setup, API reference, CLI usage, config reference) and the checkout they describe (`worktree.list`). The change range: the user's, else `git log -1 --format=%H -- <doc files>` to HEAD; keep `git log --oneline <range>` for the brief. One writer owns the whole named set.
2. Mode, chosen once. Branch mode (default): a new branch off the described branch, so the edit is committed and a clean copy can be made from it. User-checkout mode, only if the user wants the edits in their own checkout: the writer does not commit, the reader runs in that same checkout (no clone, reader worktree or merge), and the report says nothing was validated clean.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"docs/<topic>","baseBranch":"<described branch>","collisionPolicy":"error"},"focusPolicy":"preserve"}
```

   Keep each returned `worktreeId` (writer, later reader) and pass it to `worktree.waitUntilReady` (`timedOut:true`: call again, about ten calls at most) and to the launch there; only `setupState:"ready"` goes on, any other state: report it, launch nothing, create nothing again.
3. Record the writer's start SHA. Launch the writer (`name` "Docs: <topic>", `worktreeId`, `notify:true, handback:true`). Brief: the files, the range, and: "Work from the implementation, not the diff or commit messages: find every command, flag, environment variable, config key, default, endpoint and example in the source (argument parser, config schema, routes) or the built tool's `--help`, and cite file:line. Change only these docs files, never code, tests or config to make a doc true; a mismatch that looks like a code bug goes in your report. Keep the docs' structure and style; remove what documents removed features. Run each changed example that is safe (no deploys, publishing, real credentials or writes outside this checkout). Commit on this branch (user-checkout mode: do not commit); do not push. Reply: files changed, each changed claim with its source, examples run with results, suspected code bugs."

On notice or resume:
1. Writer done: `git -C <path> diff --name-status <start sha>..HEAD` and `git status --porcelain` must show the named docs files only (else stop: a boundary failure); open at least three of its citations yourself and keep any claim its source does not bear out for the corrective round.
2. Reader (branch mode): a second worktree, `newBranch` `docs/<topic>-read` with `baseBranch` the writer's branch, ready as above; a DIFFERENT agent there (another kind where installed), `notify:true, handback:true`. Brief: "You are a new user. Follow <doc files> literally and in order, without reading the source or the diff to fill gaps. Run every setup step and every example in <sections>. For install or first-run steps: `git clone --branch <writer branch> <writer worktree path> <new temp dir>`, check its HEAD is <writer sha>, and follow them there. Do not repair source or docs; setup may create files only inside that temp dir or this checkout. Skip, and list as skipped, deploys, publishing, paid or external writes, global installs, shared resources and anything needing real credentials. Reply per step: doc file:line, the command, worked as documented / output differed / failed (the error), and every place you had to guess."
3. Reader done: your citation findings and each failed or differing step, quoted with its location, go to the writer in one `terminal.sendCommandOwned` (`notify:true, handback:true`): fix the doc and commit, or report it as a code bug. End the turn. On its notice, check the new diff as in step 1, then send the reader: "`git merge --ff-only <writer branch>`, then re-run only steps <list>, install steps in a fresh temp clone" (`notify:true, handback:true`); end the turn. Report after that notice.
4. Verify a sample yourself in the reader's checkout: at least two documented commands run by you (a `--help` flag, a setup step) and two config keys or defaults found in the source. A claim still unsupported is reported, never smoothed over.

Stop and recovery: one writer, one reader, one corrective round. A writer never changes code to match the docs. Push or a PR only if asked (daintree.workflow.prep-branch-for-review); the reader's worktree is left for cleanup.

Report back: the docs changed (branch, worktree, commit); each changed claim and its source; the reader's steps with outcomes, re-runs after the fix, and skipped steps; what you ran yourself; suspected code bugs for the user; writer and reader terminal ids.
