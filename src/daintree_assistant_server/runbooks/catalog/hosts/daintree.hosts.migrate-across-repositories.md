---
id: daintree.hosts.migrate-across-repositories
title: Run one mechanical migration across repositories on the Remote Hosts
summary: 'From this Shell, carry one mechanical change through each repository the person selected, once per repository however many hosts clone it: its owner host inventories it with recorded searches, a pilot fixes the rulebook every host follows, each repository''s units are integrated on their own, its searches rerun on the result, one row per repository.'
whenToUse: 'Use from this Shell when the person wants ONE MECHANICAL CHANGE (an API migration, a codemod, a library or logger swap, a config or lint rollout) carried through SEVERAL REPOSITORIES held on their Remote Hosts: "move these services off the deprecated client using my other machines", "pilot the date library swap in one repo, then roll it through every selected project on the hosts", "a cross-repository migration over the remote hosts with residual counts per repo". The deliverable is one row per selected repository, counted once however many hosts hold a clone: its search commands and counts before, units each with one owner, a pilot whose rulebook every host follows and only the person amends, the repository''s units integrated on their own, its searches rerun on the integrated commit, and a repository no host can reach or that stopped partway shown as such. Do not use for one codebase migrated by agents on this machine (daintree.orchestration.migration-sweep), the same work once on every host (daintree.hosts.swarm-fan-out), different independent pieces placed on hosts (daintree.hosts.place-work), a generic plan of dependent units (daintree.hosts.execute-distributed-workflow), or only combining branches already finished (daintree.hosts.integrate-host-branches).'
risk: external
requiredTools:
  - host.list
  - host.call
hostEligibility: connected
---
Keep in this conversation, and for a run spanning sessions a checkpoint as the foundation says, the rulebook, the searches and each repository's units, one writer each; lost: launch nothing until restored. Each unit is one agent you drive on its repository's owner host through `host.call`; the shared remote you read in your own shell, in a scratch clone per remote.

Procedure:
1. Targets: `host.list`; per host project that may hold a chosen repository, its `origin` URL and default branch by `project.runCommand` there (`git remote get-url origin`, `git symbolic-ref --short refs/remotes/origin/HEAD`). One target per remote however many hosts clone it (same host and path, ignoring scheme, user, `.git`; unsure: ask), one owner host and project; its base SHA by `git ls-remote`. Held by none: its row, inaccessible, why.
2. Inventory per target at its base SHA, read-only, every command recorded: `git grep -c <form> <SHA> -- . ':!<excluded>'` per form, `git grep -n` on the bare name (lines no form matches: unlisted), `git ls-files` for manifests, lockfile, config. Run them on the owner host:

```json tool=host.call surface=help-core
{"hostId":"<hostId>","projectId":"<projectId>","tool":"project.runCommand","args":{"projectId":"<projectId>","command":"git grep -c '<form>' <SHA> -- . ':!<excluded>'","timeoutMs":120000}}
```

   Freeze units: shared files, then partitions by directory; ids `<repo>-<part>`, branches `<run>/<id>`.
3. Get the person's leave to push `<run>/*` branches (never a base branch); without it stop here. In one target its shared-file unit, then one typical partition from that SHA as pilot. Check it; rulebook v1: before and after per form, exceptions, what not to touch, golden SHA. Wait for the person only on a scope or form change.
4. Per target: shared-file unit first, partitions from its SHA. Per unit on the owner host a worktree on `<run>/<id>` from its base (`worktree.createWithRecipe`, `collisionPolicy:"error"`, then `worktree.waitUntilReady` with its `worktreeId`), then one `agent.launch` there with `name` `<run>: <id>`, `notify:true, handback:true`. The brief: the rulebook verbatim and the golden SHA; its paths only, never shared files; an unlisted form left as is, reported by line; its tests; commit, push its branch, reply with the SHA. Many units on one host: delegate them to its assistant as the hosts foundation says, the brief saying it may split them among its own agents. End the turn.

On notice or resume:
- Unlisted form: hold unlaunched units; amend with the person (v2: before, after, its search); each running unit gets one `terminal.sendCommandOwned` there naming theirs, `notify:true, handback:true`; follow-up units, new ids, where they met it.
- A unit's report is a claim: its SHA on `<run>/<id>` (`git ls-remote`); `git diff --stat <base>...<SHA>` all within its paths; the searches empty there or listed.
- A target's units done: integrate them per daintree.hosts.integrate-host-branches, the allowed checks as its checks, searches as counts only; rerun the searches on the integrated SHA in your scratch clone. Rows close on passing checks, residuals zero or listed.

Stop and recovery: one agent per unit; before any launch, `terminal.list` there. A target whose host refuses or drops stays partial in its row, never moved to another clone unasked. No force push, PR, deploy or merge across repositories.

Report back: a row per selected repository: remote, owner host and unused clones, base SHA, rulebook version, units (owner, branch, SHA, state), integrated SHA, residuals before and after (how checked), checks, exceptions; inaccessible and partial rows as such.
