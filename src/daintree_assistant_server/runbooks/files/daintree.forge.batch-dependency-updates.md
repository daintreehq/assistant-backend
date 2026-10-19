---
id: daintree.forge.batch-dependency-updates
title: Work through a batch of dependency-update PRs
summary: Gather the open Dependabot or Renovate PRs, sort them by risk, have an agent fold the safe ones into one batch branch you verify, or land them one at a time, send breaking majors to a proper upgrade, and close superseded bot PRs only after their replacement merges.
whenToUse: 'Use when a pile of automated dependency-update pull requests needs handling — "deal with the dependabot PRs", "merge the safe renovate bumps", "combine these dependency updates into one PR", "which of the bot PRs are safe to take?", "clear out the dependency PRs". The deliverable is a disposition for each bot PR (landed, folded into one batch branch, needs work with the breaking change named, left alone) with evidence, and superseded bot PRs closed only after what replaces them has merged. Do not use to upgrade ONE dependency or framework yourself and fix what breaks (use daintree.deps.upgrade-dependency); to merge a single PR (use daintree.forge.land-pull-request); for a red check with no dependency bump behind it (use daintree.ci.diagnose-and-fix); to resolve one PR''s merge conflicts with its base (use daintree.git.sync-with-base); to review the general PR queue (use daintree.review.review-pr-queue); or to split unrelated work across agents (use daintree.orchestration.fan-out-work).'
risk: external
requiredTools:
  - forge.listPRs
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.sendCommandOwned
  - project.detectRunners
  - project.runCheck
---
Merging and closing are public writes: only when the user asked to land or clear the PRs, through `gh` with `--repo <owner/name>`, each read back. An agent builds the batch branch; you gather, classify, brief, verify and land. Never push to a bot's branch, never hand-edit a lockfile, never merge several bot PRs in a burst: they share the lockfile and each merge makes the rest stale. `<remote>` is the one whose URL is `<owner/name>` (`git remote -v`).

On a host: shell steps and checks (`project.runCheck`) run there, per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Gather: `forge.listPRs` with `"state":"open"`, `"search":"author:app/dependabot"` (Renovate: `author:app/renovate`), `"perPage":50`. Per PR: `gh pr view <n> --repo <r> --json title,body,files,headRefOid,baseRefName,mergeable` and `gh pr checks <n> --repo <r>`. From title and body: package or group, from and to versions, ecosystem, runtime or dev dependency.
2. Classify each: patch, minor, `0.x` minor (breaking risk: read its release notes before batching it), major, security fix; CI for that head. Green CI on an old base says little about the combined result. For every major, read the breaking-changes section the PR body quotes or links, never recall it.
3. Strategy: the user's; otherwise propose one and wait: green patch and minor bumps into one batch branch, majors and failing bumps on their own.
   - Batch: `git fetch <remote> <base>` and record its SHA; a worktree off it, then `worktree.waitUntilReady` (`timedOut:true`: call again) until `setupState:"ready"`:

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"deps/batch-<date>","baseBranch":"<remote>/<base>","fromRemote":true,"collisionPolicy":"error"}}
```

     One `agent.launch` there, `notify:true, handback:true`. The brief: the ordered PRs with each `headRefOid`; "Per PR: record `git rev-parse HEAD`; `git fetch <remote> pull/<n>/head`; FETCH_HEAD must equal the given SHA, else skip it and say so; `git merge --no-edit <sha>`. On a conflict keep both bumps in the manifest, regenerate the lockfile with the project's package manager (`npm install`, `uv lock`, …), never by hand, and commit; or `git merge --abort` and leave it out. Run the tests after each merge; a bump that breaks them: `git reset --hard <recorded SHA>` (this branch holds only your merges), report its failing output. No push, no comments, no bot PR changes. Reply per PR: included or dropped and why; the final commit."
   - One at a time, you merge each: `gh pr view <n> --repo <r> --json state,mergeable,reviewDecision,headRefOid`, `gh pr checks <n> --repo <r> --required` all passed, `headRefOid` re-read unchanged; then `gh pr merge <n> --repo <r> --<method> --match-head-commit <headRefOid>` and read back `state,mergedAt`. Unknown or changed evidence stops that PR. Each merge makes the rest stale: ask a bot to rebase (`@dependabot rebase`; Renovate: its rebase checkbox) only when the user asked you to land them, else report it pending; it needs fresh CI before its turn (daintree.orchestration.watch-outcome). Never force it.
   - A breaking major is its own upgrade: daintree.deps.upgrade-dependency on a branch of its own, with the failing check and the breaking-changes section. Leave the bot PR open until that lands.

On notice or resume: the agent's result is a claim. Each included SHA is in the branch (`git -C <path> merge-base --is-ancestor <sha> HEAD`), `git -C <path> diff <base SHA> --stat` touches only files those PRs touched, the tree is clean; re-run the tests yourself on the final commit (`project.runCheck`, `cwd` = the path). One corrective send for a gap. When the user asked for one PR or to land the batch: daintree.workflow.prep-branch-for-review (push, reuse an open PR for that head branch or create one listing every included bot PR, read back URL and head SHA).

Closing: only after the replacement merged (`gh pr view <batch> --repo <r> --json state,mergedAt`). Per bot PR, confirm its package is at that version or newer in the fetched base (`git show <remote>/<base>:<lockfile>`); bots often close their own, so re-read and close only those still open: `gh pr close <n> --repo <r> --comment "Included in #<batch>"`, read back.

Stop and recovery: a lost merge or close reply is settled by reading the PR, never by repeating it. An ambiguous launch is reconciled with `terminal.list`. A dropped bump stays open with its reason; never retry it by loosening tests or pinning something else back.

Report back: per bot PR its number, package, from and to, class, CI on its head, disposition and evidence; the batch branch, base SHA, included PRs and the checks you ran on its final commit; merges and closures as read back; what is left and why.
