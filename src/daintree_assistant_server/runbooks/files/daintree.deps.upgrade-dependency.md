---
id: daintree.deps.upgrade-dependency
title: Upgrade a dependency or framework with verification
summary: Upgrade one package, framework, runtime or toolchain in its own worktree — baseline checks first, the upstream migration guide and codemods applied as their own commit, a constrained fix loop, and a before/after check comparison that separates what the upgrade broke from what was already red.
whenToUse: 'Use when the user wants a DEPENDENCY, FRAMEWORK, LANGUAGE RUNTIME or TOOLCHAIN version changed and proven working — "upgrade to React 19", "bump Next to 16 and fix what breaks", "move us to Node 24", "upgrade pydantic to v2", "update our minor deps and make sure nothing broke". The deliverable is an upgraded branch plus a baseline-versus-after check comparison and the migration-guide items applied or deferred. Do not use for a batch of Dependabot or Renovate PRs that are already open (use daintree.forge.batch-dependency-updates), to fix a check that was already red before any upgrade (use daintree.ci.diagnose-and-fix), to review someone else''s upgrade PR (use daintree.review.review-change), to merge the bump PR (use daintree.forge.land-pull-request), for several unrelated upgrades each given its own agent (use daintree.orchestration.fan-out-work), for one repeated code change split across several agents, an upgrade''s fallout included (use daintree.orchestration.migration-sweep), or to check dependencies for known vulnerabilities or licence problems without upgrading (use daintree.review.audit-codebase).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - actions.getContext
  - project.detectRunners
  - project.runCheck
  - terminal.list
  - terminal.sendCommandOwned
---
One visible agent does the upgrade in its own worktree; you isolate it, brief it and check the result. What makes it an upgrade: a baseline before anything changes, the guide read not remembered, no check made green by weakening it. Only when the user asks you yourself to do it, follow the brief in your own shell.

Procedure:
1. Resolve the package, the target version and the base (the one the user named, else the default branch; `git fetch` and verify the ref). One major bump per worktree.
2. Isolate: a new worktree off the verified base, `fromRemote:true` when the base is a remote ref; keep its `worktreeId` and path. Never the main checkout or a worktree an agent works in. An ambiguous create: reconcile with `worktree.list`, never create twice.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"chore/upgrade-<package>-<version>","baseBranch":"origin/main","fromRemote":true,"collisionPolicy":"error"},"focusPolicy":"preserve"}
```

3. Readiness: `worktree.waitUntilReady` with that `worktreeId`; a `timedOut` call is retried while setup is pending or running, at most three calls. Only `setupState:"ready"` goes on; `failed`, `timed-out`, `needs-approval` or `unknown`: report it and launch nothing.
4. Launch one agent: the one the user named, else `claude`; `name:"Upgrade: <package> <version>"`, the `worktreeId`, `notify:true, handback:true`; keep the `terminalId`. The prompt carries package, from and to versions, the base, and:
   - "Baseline before changing anything: install from the lockfile as it is (`npm ci`, `pnpm install --frozen-lockfile`, `uv sync --locked`), then build, typecheck, lint and tests. Record pass or fail per check, failing test names, and the runtime and package-manager versions. A red baseline is reported, never fixed here.";
   - "Read the release notes and migration guide from the source (`gh release view`, the upgrade guide, the CHANGELOG), list the breaking items that touch this code and cite them. Never migrate from memory.";
   - "Bump with the project's package manager so the lockfile moves with the manifest; never `--force`, `--legacy-peer-deps` or `--no-verify`. Update the companion packages its peer requirements need; stop and ask only if that takes another major bump or changes scope. A runtime or toolchain upgrade also updates its version files (`.nvmrc`, `engines`, `.python-version`, CI and container images) and shows each check ran on the new version.";
   - "Commit the bump, then the codemods alone, then each accepted fix set. Never weaken a check to go green: no skipped or deleted tests, `any`, `@ts-ignore`, `# type: ignore`, lint disables or pinning back. Change an assertion or snapshot only for a behaviour change the guide documents, and name it. About three rounds per failing area, then leave it as manual work.";
   - "Re-run the baseline checks on the final commit; each failure is new, fixed or pre-existing. A server or UI also gets started and probed.";
   - "Do not push or open a PR. Reply: versions before and after (manifest, lockfile, runtime), both matrices, guide items applied and deferred, commit SHAs, anything left uncommitted."
5. End your turn.

On notice or resume, before any report (its reply is the agent's claim), check yourself:
- `git -C <path> status --short`: anything uncommitted is reported as such, never passed off as part of HEAD. `git -C <path> log --oneline <base>..HEAD`: bump, codemods and fixes as separate commits.
- Scan `git -C <path> diff <base>...HEAD` for the bans: `ts-ignore`, `.skip`, `xit`, `: any`, removed assertions, snapshots rewritten with no guide item.
- Re-run the checks that matter yourself: `project.runCheck` with `projectId` (`actions.getContext`), `runnerId` (`project.detectRunners`) and `cwd` the worktree path (without it the main checkout is checked). A runtime upgrade: confirm the version it ran on. A server or UI: start and probe (daintree.terminals.run-visible-command) or reload its preview (daintree.runtime.debug-web-app)
- A question inside the upgrade: answer once with `terminal.sendCommandOwned`, `notify:true, handback:true`; a peer or scope decision goes to the user. A ban or a missing commit: one corrective send with the evidence.

Stop and recovery: one corrective round, then report. Never recreate the worktree or reset away the codemod commit without the user; an upgrade that cannot go green stays on its branch, reported, not reverted. Push or open a PR only if asked (daintree.workflow.prep-branch-for-review).

Report back: branch, worktree and the agent's terminal id; versions before and after (manifest, lockfile, runtime); baseline and after matrices with new, fixed and pre-existing failures, and which checks you re-ran; guide items applied and deferred; commits; uncommitted state; the runtime smoke result; what is left for a human.
