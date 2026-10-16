---
id: daintree.orchestration.migration-sweep
title: Run a codebase-wide mechanical migration across agents
summary: Inventory every occurrence, land the shared-file changes once, pilot one partition into a rulebook and golden example, fan the remaining partitions out to agents, integrate them, and prove zero residual occurrences on the combined tree.
whenToUse: 'Use for a large MECHANICAL change repeated across many files or packages by several agents — "migrate every component off the old API", "replace all uses of the deprecated logger across the monorepo", "convert the tests from jest to vitest, split by folder", "roll this lint rule out everywhere with a few agents", "move moment to date-fns, one agent per package". One pattern applied everywhere is the signal. The deliverable is a migrated, integrated branch whose residual search count is zero or a listed set of exceptions. Do not use for a batch of unrelated issues or tasks (use daintree.orchestration.fan-out-work), for phased work whose steps depend on each other rather than one repeated pattern (use daintree.orchestration.execute-durable-workflow), for a dependency or framework upgrade one agent can carry (use daintree.deps.upgrade-dependency), for running database migrations (use daintree.runtime.migrate-worktree-database), for one migration across several repositories on the Remote Hosts (use daintree.hosts.migrate-across-repositories), or only to merge branches that already exist (use daintree.git.integrate-agent-branches).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.sendCommandMany
  - project.runCheck
---
The middle of a sweep is daintree.orchestration.fan-out-work's queue (disjointness gate, cap, setup gate, checkpoint, notice handling): fetch it with `get_runbook` if it did not come back with this one. This runbook adds what comes before and after it. Agents edit; you inventory, dispatch and verify. Without a pilot and a rulebook, every agent invents its own translation.

Procedure:
1. Inventory, read-only, in the target checkout: search commands that find every form of the pattern (the call, imports, config), with the paths and exclusions fixed now (generated, vendored and fixture code out unless the user includes it). Keep the exact commands: every later count reruns them. `rg -c` counts matching lines per file. Partition by directory or package, and mark any partition that consumes another's migrated API. State the pattern, counts and partitions; ask only if they go beyond what the user asked for.
2. Integration worktree: `worktree.createWithRecipe` new branch `migrate/<name>`, `collisionPolicy:"error"`, no `recipeId`, then `worktree.waitUntilReady`: only `setupState:"ready"` launches (`timedOut:true`: call again; any other state: stop and report). Shared files first, once: launch one agent there (`notify:true, handback:true`) to change the manifests, lockfile, central config, shared types or codemod script, commit, and report that it builds. End the turn; on its notice check its diff touches only those files. Every partition branches from `migrate/<name>`; no partition agent touches shared files, and one that needs a change reports it.
3. Pilot one small, typical partition: one agent in its own worktree from `migrate/<name>`, `notify:true, handback:true`; its brief asks it to migrate the partition, run its tests, commit, and draft the rulebook: before and after for each form, the edge cases met, what not to touch. End the turn. On its notice, verify its diff and tests yourself and settle the rulebook; the pilot commit is the golden example and merges like any partition. Show the rulebook and pilot diff; wait for the user only if the pilot changed the scope or found forms they did not expect.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"migrate/<name>-<partition>","baseBranch":"migrate/<name>","collisionPolicy":"error"}}
```

4. Fan the remaining partitions out per daintree.orchestration.fan-out-work, each worktree branched from `migrate/<name>`. A partition that consumes another's migrated API launches only after that one is merged. Each brief adds: the partition path and nothing outside it; the rulebook verbatim; the golden commit SHA; "Anything the rulebook does not cover: leave a `TODO(migration)` comment and report it, do not improvise"; never touch manifests, the lockfile or shared config; run the partition's tests, commit, and report changed files, test results and TODOs.

On notice or resume:
1. Handle the notice as fan-out-work says. Then check the rulebook held: `git -C <path> diff --stat migrate/<name>...HEAD` (three dots: only its own commits, however far the integration branch moved) for files outside the partition or shared files touched (a boundary failure: hold that partition), and the step-1 commands scoped to the partition for residuals.
2. Integrate finished partitions into `migrate/<name>` one at a time, in inventory order, checked after each (daintree.git.integrate-agent-branches).
3. When every partition is merged or recorded failed, on the combined tree: rerun the step-1 commands, then the full checks (`project.runCheck` or the repository's commands). Report the counts before and after; list every remaining match with file and reason, never hide one. Reported TODOs go to the user.

Stop and recovery: a partition that drifts from the rulebook gets one corrective send quoting the rule; a second drift goes to the user. A rulebook gap found mid-sweep: launch nothing more and amend the rulebook with the user. Send the amendment to each running partition (`terminal.sendCommandMany`, `notify:true, handback:true`); it lands as that agent's next prompt, after its current turn, so on its report check its diff against the amended rule. Never push or open a PR unless asked.

Report back: the pattern, search commands and counts per partition; the shared-file commit; the pilot diff and rulebook; per partition its worktree, branch, terminal id, status and evidence; the integration branch; counts before and after with listed exceptions; TODOs; anything still running.
