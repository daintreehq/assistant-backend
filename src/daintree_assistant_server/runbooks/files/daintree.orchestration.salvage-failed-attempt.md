---
id: daintree.orchestration.salvage-failed-attempt
title: Salvage the useful part of a failed attempt
summary: Choose the sound, self-contained parts of a failed agent attempt, have a builder carry only those into a fresh branch off the current base and make them pass there on their own, and leave the failed attempt untouched for reference.
whenToUse: 'Use when an attempt failed as a whole but parts of it are worth keeping — "that approach failed, but the parser and its tests are useful, keep those", "pull the bits worth keeping out of the broken refactor into a fresh branch", "rescue the good parts of codex''s attempt", "start over but keep its migration and fixtures". The deliverable is a NEW branch and worktree off the current base holding only the chosen files, hunks or commits with provenance, its own checks passing without the rest, and the failed attempt left as it was. Do not use to explain why it failed (use daintree.agents.postmortem-run), to reverse an agent''s changes in place (use daintree.git.undo-agent-changes), to port an already-chosen fix or commits onto other named branches (use daintree.git.cherry-pick-changes), to adopt a contest winner (use daintree.orchestration.competition), to have another agent carry on the SAME task (use daintree.agents.hand-off-task), to harden a prototype that worked (use daintree.orchestration.productionize-prototype), or to get back work that was lost (use daintree.git.recover-lost-work).'
risk: git
requiredTools:
  - worktree.list
  - terminal.list
  - agent.launch
  - worktree.createWithRecipe
  - worktree.waitUntilReady
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
  - actions.getContext
  - project.detectRunners
  - project.runCheck
---
You pick the subset with the user and verify; a read-only surveyor proposes it, a builder in a new worktree extracts it. The failed worktree is evidence: nothing edits, commits in, resets, stashes or deletes it.

On a host: shell steps and checks (`project.runCheck`) run there, per the hosts foundation.

Procedure:
1. Source: the failed attempt's worktree from `worktree.list` (path, branch). No agent there `working` (`terminal.list` with its `worktreeId`); one is: it must stop first (daintree.agents.steer-running-agent). Freeze F = its HEAD and record `git -C <src> status --porcelain`, `git -C <src> diff HEAD | shasum` and a `shasum` of each untracked file: uncommitted changes are part of the attempt. Freeze B = `git rev-parse origin/<base>` as last fetched (possibly stale: say so before creating); M = `git merge-base F B`.
2. Candidate units: the user's named parts, a post-mortem salvage list already in this conversation, else one surveyor in the source worktree with `waitForReply:true, handback:true`:

```json tool=agent.launch
{"agentId":"claude","name":"Survey: salvage <attempt>","worktreeId":"<source worktree id>","prompt":"<brief>","waitForReply":true,"handback":true,"waitSeconds":900}
```

   Its brief: "Read-only: change, commit or delete nothing. Compare <M>..<F> plus the uncommitted changes. List the salvageable units, each a set of files, hunks or commits that stands alone: what it does, what inside this attempt it depends on, which tests exercise it and whether they would pass without the broken parts, and why it is sound. Then list what is broken. At most 40 lines." Check it: every path it names is in `git -C <src> diff --stat <M>` or the status.
3. Show keep and drop with dependencies, each unit marked whole files or chosen hunks. A unit that calls into a dropped part, or whose tests only passed alongside broken code, needs that part or an adaptation: say which. The user confirms the set, unless the request named it exactly and nothing depends on a dropped part.
4. New worktree at the B SHA on <src>'s machine (the builder reads <src>; no `fromRemote`), then `worktree.waitUntilReady` with its `worktreeId`, at most three calls, until `setupState` is `ready`. `failed` or `needs-approval`, or still running after three: stop and report the worktree id, state, stage and error; resuming is another readiness check on that worktree, never a second create. Confirm `git -C <new> rev-parse HEAD` is B.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"salvage/<name>","baseBranch":"<B SHA>","collisionPolicy":"error"}}
```

5. Launch the builder there with `notify:true, handback:true`. The brief: the source path (read-only), M, F, the chosen units with exact paths, hunks or SHAs, the dropped list, the check commands, and verbatim: "First run the checks on this clean base and report what already fails. Bring in only the chosen units, one transfer method each: `git cherry-pick -x <sha>` for a wholly chosen commit; `git -C <src> diff --binary <M> -- <paths>` piped to `git apply --3way` only when every change in those files is chosen (it already carries <src>'s uncommitted edits: do not copy them again); otherwise a patch of only the chosen hunks; chosen untracked files copied. A conflicted apply: resolve it, never apply again. Adapt them to this base. Never modify, commit in, reset or delete <src>. Bring nothing from the dropped list; if a chosen unit needs something dropped, stop and report. The units' tests must pass here on their own; add a test where one leaned on broken code. Run the full checks; do not fix what already failed on the base. Commit, citing the source branch and F. No push. Report each unit and how it came across, adaptations, tests added, base and final checks."

On notice or resume:
1. A dependency on a dropped unit changes the salvage set: the user decides.
2. Verify yourself in the new worktree: `git status` clean; read the whole `git diff <B> HEAD`: every hunk maps to a chosen unit, a test added for one or a reported adaptation, nothing from the dropped list; the checks re-run (`project.runCheck`: the work's `projectId`, `runnerId` from `project.detectRunners`, `cwd` the new path), failures already on B reported apart; the source unchanged (`git -C <src> rev-parse HEAD` is F, status and hashes as recorded); `git ls-remote origin refs/heads/salvage/<name>` empty.

Stop and recovery: one corrective round with the failing evidence, then the user. Never relaunch the builder while its terminal lives. Delete neither worktree.

Report back: the source (worktree, branch, F, still intact); the new worktree id, path, branch and HEAD; the builder's terminal id; each unit kept with provenance and how it came across; what was dropped and why; adaptations and tests added; your checks, base failures apart; not pushed.
