---
id: daintree.orchestration.spike-options
title: Spike competing options in parallel
summary: Give each option under consideration (a library, an approach, a UI direction) its own agent and throwaway worktree building the same scenario, measure every spike yourself, then recommend one or lay the variants side by side for the user to pick; nothing is merged.
whenToUse: 'Use when the user wants several OPTIONS tried hands-on before deciding — "spike Drizzle, Prisma and Kysely and tell me which to use", "prototype both approaches and compare", "try zod vs valibot on our real forms", "mock up three versions of the settings page and let me look", "give me a minimal, a dense and a playful take on the dashboard". Each agent is ASSIGNED a different option or design direction and builds the same scenario or screen in its own throwaway worktree. The deliverable is your measured comparison with a recommendation, or runnable variants for the user to choose between, with nothing merged. Do not use when every agent tackles the same task its own way and a winner is voted or judged (use daintree.orchestration.competition), for opinions with nothing built (use daintree.agents.poll-pinned-agents), for a debate converging on one answer (use daintree.orchestration.multi-agent), for rival CAUSES of a bug (use daintree.orchestration.hypothesis-split), for one agent given a time budget (use daintree.orchestration.timebox-attempt), for a batch of unrelated tasks (use daintree.orchestration.fan-out-work), for a broken existing dev preview (use daintree.runtime.debug-web-app), or to build the picked spike properly for production (use daintree.orchestration.productionize-prototype).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
  - terminal.interruptOwned
  - recipe.list
  - recipe.run
---
Spikes are throwaway: no push, PR or merge unless the user adopts one. A spike agent's view of its own option is advocacy; your measurements are the evidence.

On a host: shell steps via its command runner, or delegate.

Procedure:
1. Fix these before launching and state them in your reply:
   - The options, 2 to 4, genuinely different: the user's, or ones you propose and they confirm. One agent per option, never picking its own.
   - The kind. Technical options all build ONE concrete scenario ("read an order with its lines in one transaction, with a test"), judged on criteria you name, each with how you will measure it. Design variants build the same screen under shared constraints (existing components and tokens only, no new dependencies, only <paths>, the same fixture data), and only the user judges them.
   - The base: a branch and its SHA. Worktrees get committed code only: say so if the user has uncommitted changes.
   - A time bound per spike, stated in the brief; Daintree enforces none.
2. One worktree per option, created one after another, no `recipeId`, then `worktree.waitUntilReady`. Only `setupState:"ready"` launches; `timedOut:true`: call again; `failed`, `timed-out`, `needs-approval` or `unknown`: that option stops and is reported. The branch tip can move: `git -C <path> rev-parse HEAD` must equal the base SHA, else stop that option and tell the user.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"spike/<option>","baseBranch":"<base branch>","collisionPolicy":"error"}}
```

3. Launch one agent per option in the same turn, each its own `agent.launch`. The prompt: the scenario or screen verbatim; "Use <option> only; do not switch to another option"; the constraints; the time bound; "Throwaway prototype: work only in this worktree, commit on this branch, do not push or open a PR, leave no server running"; a report of at most 30 lines: what works, what fought you, workarounds, dependencies added, and the exact command and port to run or view it.

```json tool=agent.launch
{"agentId":"<agent id>","name":"Spike: <option>","worktreeId":"<option worktree id>","prompt":"<the brief above>","focusPolicy":"preserve","notify":true,"handback":true}
```

On notice or resume:
1. Map the terminal to its option. A question inside the brief: answer once with `terminal.sendCommandOwned` and `notify:true, handback:true`; "can I use <another option>" gets no. Anything else goes to the user.
2. Measure that spike yourself before comparing anything. In its worktree: `git -C <path> status --porcelain`, `git -C <path> diff --stat <base sha>` (a committed log or build file is a finding), the manifest and lockfile diff, the scenario test run by you, size or timing where a criterion asks. A design variant is checked against the constraints only (new dependency, files outside the paths), never ranked. A spike that did not build the scenario is incomplete, not a loss.
3. Once every option is measured or recorded failed:
   - Technical: a matrix of your measurements per option, each cell from a command you ran, friction notes attributed to that spike's agent, and a recommendation naming the deciding criterion and the open unknowns. A second opinion only on request: one fresh read-only reviewer, never a spike author, given the diffs under shuffled neutral labels and the criteria.
   - Design: every variant in one identical format, unranked: direction, worktree and branch, how to view it, files changed, deviations. The user picks. Live viewing only on request: `recipe.list`, then `recipe.run` with that `recipeId` and the variant's `worktreeId`; report any `failedTerminals` and check the address loads before giving it and its machine; several worktrees fighting over one port is daintree.worktree.provision-local-environment.
4. After a pick, as the user asks: "B but with A's header" is one send to B's agent (`notify:true, handback:true`) quoting A's hunk, never your own merge. Shipping B as built: a PR through daintree.workflow.prep-branch-for-review, flagged as a prototype; then the other spikes go through daintree.orchestration.workspace-cleanup. Building it properly: daintree.orchestration.productionize-prototype.

Stop and recovery: one launch per option; a failed option is reported, never quietly replaced. A spike past its bound: recheck it as the foundation says; stop it (`terminal.interruptOwned`) only if the user said to stop at the bound, then ask once for its report so far. Delete nothing unasked.

Report back: the options, base SHA and criteria; per option its worktree, branch, terminal id, what was built and your measurements (or the variant's view command and deviations), its agent's friction notes as claims; the recommendation and deciding criterion, or that the user's pick is awaited; spikes still running; worktrees left for cleanup.
