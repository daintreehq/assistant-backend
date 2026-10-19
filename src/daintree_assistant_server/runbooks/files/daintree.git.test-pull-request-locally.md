---
id: daintree.git.test-pull-request-locally
title: Check out a pull request and try it locally
summary: Put a PR's exact head into its own Daintree worktree, screen what running it would execute, run its tests, a reproduction or the app, and report what works at that commit, without pushing to or commenting on the PR.
whenToUse: 'Use when the user wants a pull request RUN or TRIED on this machine rather than read — "check out #340 and see if it works", "pull down that contributor''s PR and run the tests", "spin up the PR so I can click around", "does #77 actually fix the bug for me?". The deliverable is a hands-on result bound to the PR''s head commit: what running it would execute (screened first for outside contributors), setup state, each check or reproduction with its outcome, on the base as well when a bug is named, and the worktree left standing for the user. Do not use to only create a worktree for a PR with nothing run (use daintree.worktree.create-plain-worktree); to judge code quality and produce findings (use daintree.review.review-change, or daintree.review.review-pr-queue for several PRs); to run the checks on your own branch or worktrees (use daintree.ci.run-local-checks); to reproduce a reported bug with no PR involved (use daintree.debug.reproduce-bug); to confirm an agent''s claim about its own work (use daintree.orchestration.verify-agent-work); to debug a preview that is already open (use daintree.runtime.debug-web-app); or to stage a finished feature with sample data and a walkthrough for someone to see (use daintree.workflow.prepare-feature-demo).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
optionalTools:
  - worktree.list
  - project.detectRunners
  - project.runCheck
  - agent.launch
  - terminal.new
  - recipe.list
  - recipe.run
---
This tries the PR; it never pushes to the PR's branch, comments on it or approves it. The worktree stays for the user. You run Daintree's tools; a reproduction goes to an agent in the PR's worktree. `<remote>` is the one whose URL is the PR's repository (`git remote -v`; `upstream` when `origin` is a fork).

On a host: shell steps via its command runner and checks via its `project.runCheck`, else delegate; own shell: `gh -R <repo>`.

Procedure:
1. Read the PR: `gh pr view <n> --repo <owner/name> --json number,title,author,headRefName,headRefOid,baseRefName,isCrossRepository,state,files,url`. Record `headRefOid`: every result binds to it.
2. Screen before anything executes. For an outside contributor or fork (`isCrossRepository`, or an author the user does not vouch for), know the project's setup commands, then read the PR's diff (`gh pr diff <n> --repo <r>`) of whatever they, the build or the tests execute: package manifests and lifecycle scripts (`postinstall`, `prepare`), dependency sources in lockfiles, Makefiles and build scripts, test config and fixtures that execute, `.daintree/` config. Daintree holds changed setup commands for approval, but an approved install still runs the PR's own scripts. Tell the user what running it would do; get a yes before step 3. Something suspicious stops here, with the evidence.
3. A worktree at that head. Reuse a `worktree.list` row with that `prNumber`, a clean tree and HEAD at `headRefOid`; otherwise create:

```json tool=worktree.createWithRecipe
{"source":{"kind":"pullRequest","pullRequestNumber":340}}
```

   Never `gh pr checkout` in the user's own checkout: it moves their branch. A fetch error naming the head branch: `git fetch <remote> pull/<n>/head:pr-<n>`, then create with `{"kind":"existingBranch","branchName":"pr-<n>"}`. Then `worktree.waitUntilReady` with the returned `worktreeId`, up to about ten calls (`timedOut:true`: call again), until `setupState:"ready"`. `needs-approval`: the user approves setup on the worktree card; `failed`, `unknown` or still running at your limit: report `setupState`, `stage` and `error`. Confirm `git -C <path> rev-parse HEAD` equals `headRefOid`.
4. Run what the user asked for, in that worktree:
   - Tests or build: `project.runCheck` with a `runnerId` from `project.detectRunners` and `cwd` = the path (read `passed`; `timedOut`, `aborted` or `outputTruncated` is incomplete). Know what a runner does first: detection also lists publish and deploy scripts. A command with no runner: `terminal.new` with that `cwd` and `command`, visible to the user.
   - "Let me click around": a Dev Preview panel in that worktree, or a project recipe (`recipe.list`): read what its terminals run and run only one that fits the request (`recipe.run` with the `worktreeId`; record `spawnedTerminalIds` and any `failedTerminals`). On a host the server runs there: its localhost is not yours. Never start a server through `project.runCheck`: it blocks until its timeout. Console errors there are daintree.runtime.debug-web-app.

```json tool=agent.launch
{"agentId":"dev-preview","worktreeId":"<PR worktree id>","name":"Preview #340"}
```

5. A named bug needs both sides, or "it works on the PR" proves nothing. One `agent.launch` in the PR's `worktreeId`, `notify:true, handback:true`, then end the turn. The brief: the bug and its reproduction or the PR's test; "Note your branch. Base side: `git fetch <remote> <base>`, `git checkout --detach $(git merge-base <remote>/<base> HEAD)`; for a test the PR adds, also `git checkout <headRefOid> -- <its test files>`. Reinstall if the manifests differ; run it (it should fail). PR side: `git checkout -f <branch>`, reinstall if needed, run again (it should pass). Change no code, commit and push nothing. Finish with HEAD at <headRefOid> and `git status` clean. Reply: each command, which side, and its result."

On notice or resume: the result is a claim. Confirm HEAD equals `headRefOid` and the tree is clean, and re-run the PR side yourself (`project.runCheck` when a runner fits). A failure is a result: report it with its output; never fix it on their branch. Fixes the user wants go on a branch of their own, or into review comments they post.

Stop and recovery: one recap request per cut or unclear reply, then that side is "not established". An ambiguous launch is reconciled with `terminal.list`, never relaunched. HEAD left elsewhere or a dirty tree: report it, change nothing.

Report back: PR, author, fork or not, head commit tested; what the screen found and what the user approved; worktree id, path and setup state; each command or reproduction with its result (base against PR where a bug was named) and who ran it; what is running and where; what could not be checked; and that nothing was pushed or posted.
