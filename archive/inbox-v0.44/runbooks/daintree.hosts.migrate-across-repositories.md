---
id: daintree.hosts.migrate-across-repositories
title: Run one mechanical migration across repositories on the Remote Hosts
summary: 'From this Shell''s hub, carry one mechanical change through each repository the person selected, once per repository however many hosts clone it: lanes'' visible agents inventory it with recorded searches, a pilot fixes the rulebook every host follows, each repository''s units are integrated on their own, its searches rerun on the result, one row per repository.'
whenToUse: 'Use on the hub when the person wants ONE MECHANICAL CHANGE (an API migration, a codemod, a library or logger swap, a config or lint rollout) carried through SEVERAL REPOSITORIES held on their Remote Hosts: "move these services off the deprecated client using my other machines", "pilot the date library swap in one repo, then roll it through every selected project on the hosts", "a cross-repository migration over the remote hosts with residual counts per repo". The deliverable is one row per selected repository, counted once however many hosts hold a clone: its search commands and counts before, units each with one owner, a pilot whose rulebook every host follows and only the person amends, the repository''s units integrated on their own, its searches rerun on the integrated commit, and a repository no host can reach or that stopped partway shown as such. Do not use for one codebase migrated by agents on this machine (daintree.orchestration.migration-sweep), the same work once on every host (daintree.hosts.swarm-fan-out), different independent pieces placed on hosts (daintree.hosts.place-work), a generic plan of dependent units (daintree.hosts.execute-distributed-workflow), or only combining branches already finished (daintree.hosts.integrate-host-branches).'
risk: external
requiredTools:
  - host.list
  - host.listAssistants
  - host.sendMessage
  - host.getTask
optionalTools:
  - host.listTasks
hostEligibility: connected
---
Each unit is a daintree.hosts.delegate-to-host unit: its brief and checks (`get_runbook` if absent). Keep here the rulebook, the searches and each repository's units, one writer each; lost: send nothing until restored.

1. `host.list`, `host.listAssistants`; ask each lane that may hold a chosen repository its `origin` URL, default branch, SHA; end the turn. One target per remote however many hosts clone it (same host and path, ignoring scheme, user, `.git`; unsure: ask), one owner lane. Held by no lane: its row, inaccessible, why.
2. Inventory per target: one send, "Follow daintree.hosts.verify-for-hub" at its base SHA, checks `git grep -c <form> <SHA> -- . ':!<excluded>'` per form, `git grep -n` on the bare name (lines no form matches: unlisted), `git ls-files` for manifests, lockfile, config. Freeze units: shared files, then partitions by directory; ids `<repo>-<part>`, branches `<run>/<id>`.
3. Get the person's leave to push `<run>/*` branches (never a base branch); without it stop here. In one target its shared-file unit, then one typical partition from that SHA as pilot. Check it; rulebook v1: before and after per form, exceptions, what not to touch, golden SHA. Wait for the person only on a scope or form change.
4. Per target: shared-file unit first, partitions from its SHA. Per unit one `host.sendMessage`, `taskId` `<run>-<id>`, no `waitMs`, adding the rulebook verbatim; its paths only, never shared files; an unlisted form left as is, reported by line; push its branch. End the turn.

On notice or resume:
- Unlisted form: hold unsent units; amend with the person (v2: before, after, its search); running units get a new message naming theirs; follow-up units, new ids, where they met it.
- Unit done: `git diff --stat <base>...<SHA>` all within its paths; searches empty there or listed.
- A target's units done: integrate them per daintree.hosts.integrate-host-branches, allowed checks as its checks, searches as counts only; rerun the searches on the integrated SHA in a scratch clone. Rows close on passing checks, residuals zero or listed.

Stop and recovery: one send per unit. No force push, PR, deploy or merge across repositories.

Report back: a row per selected repository: remote, owner and unused clones, base SHA, rulebook version, units (owner, branch, SHA, state), integrated SHA, residuals before and after (how checked), checks, exceptions; inaccessible and partial rows as such.
