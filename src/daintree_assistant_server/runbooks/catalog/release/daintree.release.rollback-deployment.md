---
id: daintree.release.rollback-deployment
title: Roll a deployment back to a known-good release
summary: Identify the exact known-good release, check its code against the schema, config and flags the environment has now, restore it through the platform's traffic shift or the project's own rollback, and verify what serves and whether the symptom cleared.
whenToUse: 'Use when a DEPLOYED environment must go back to an earlier release now — "roll back prod to yesterday''s release", "the deploy broke checkout, put the previous version back", "revert staging to v2.4.1", "shift traffic back to the last good revision", "undo the deploy". The deliverable is one specifically identified known-good release (revision, tag, SHA or image) restored through the platform''s traffic shift or the project''s own rollback or redeploy, after a compatibility check (migrations applied since it, config and secret changes, feature flags), then verified: which revision serves, and whether the original symptom is gone. Do not use to investigate what broke (use daintree.debug.investigate-outage), to ship a new revision forward (use daintree.release.deploy-and-verify), to revert the bad commit in source, a code change through a PR (use daintree.edits.spawn-visible-agent), to undo an agent''s local edits (use daintree.git.undo-agent-changes), or to roll back a migration on a worktree''s local database (use daintree.runtime.migrate-worktree-database).'
risk: external
requiredTools:
  - worktree.list
  - agent.launch
  - terminal.list
optionalTools:
  - terminal.getStatus
  - terminal.sendCommandOwned
---
A rollback changes a live environment. It runs on the user's word naming the action and the environment ("roll back prod", "put the previous version back" about an evident environment). A question ("can we roll back?", "what would it take?") stops at the preview. It restores a release, nothing else.

On a host: shell steps run there (its accounts, not yours), per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Bind the environment exactly (account or project, service, region or cluster, namespace). Read what serves now and what was good from the platform the project deploys to (its deploy docs or CI say which), never from git history: the revision listing with traffic and images (`gcloud run revisions list --service <svc> --region <r>`, `kubectl rollout history deployment/<d>`, `fly releases`) and the version endpoint. The target is ONE thing, resolved to revision, full SHA and image digest: the user's, else the release serving just before the bad one. Known-good needs evidence (a passed smoke or health record, or the user's word); else it is "previous deployment, health unverified". Ambiguous or never deployed: ask. Note the traffic split to restore.
2. Mechanism, in order: the project's documented rollback (a `rollback.sh`, a Makefile target); else the platform's traffic shift to the existing revision; else a redeploy of the target SHA pinned as daintree.release.deploy-and-verify does. Never an invented command or flag.
3. Compatibility: `agent.launch` a checker in the main checkout (`worktree.list`, `isMain`), `name` "Rollback check: <env>", `notify:true, handback:true`. Brief: both SHAs, the environment, the mechanism, and: "Fetch if a SHA is missing. Compare what the target needs with what the environment has NOW, from read-only evidence: applied migrations or schema, config keys, secret references, flag state. Then `git diff --name-status <target>..<serving>` over migrations, schema, config and flags, and whether the mechanism itself migrates or changes config. Per migration: additive (usually safe) or a dropped, renamed or rewritten column or a NOT NULL the old code never fills (unsafe). List each key or flag the target needs that is gone or unknown to it. Unknown stays unknown. Read-only: change nothing, run nothing that writes, no down-migrations. Cite evidence. Prose, at most 40 lines."
4. Preview (after verifying the checker, below): environment, serving and target (revision, SHA, image), the exact command, the findings. Without the user's word to roll back, or with a risky or unknown finding the user has not accepted by name, stop and ask.
5. Just before executing, re-read: serving revision and traffic still as in step 1; no deploy or rollback in progress (`terminal.list`, the platform's operations, `gh run list --json status` for any not `completed`). Changed or busy: stop and report the new state; cancel nothing.
6. Only on that word ("can we?" is a question, not the word): `agent.launch` the executor in the main checkout, `name` "Rollback: <env> <target>", `notify:true, handback:true`. Brief: "Run exactly `<command>` once, from <path>: it moves <env> from <serving> to <target>. Accepted risks: <list>. First re-read the serving revision; not <serving>: stop. Nothing else: no migrations, config, flag or code changes, no retry. Past <deadline>: report pending with the operation id. A failure, login or credential prompt, or a question: stop and report. Never print secrets. Reply: command, exit status, operation id, last 30 output lines."

On notice or resume, by terminal `name`:
- "Rollback check": a claim; open each cited migration at the cited SHA before relying on it, then step 4.
- "Rollback:": verify, never preview or execute again; exit 0 is no proof. Platform: the target holds the intended traffic, or the rollout finished on its image; nothing else serves. Service: the version endpoint reports the target SHA; CDN lag: one re-read after a minute. Symptom: the documented read-only smoke check or a non-writing form of the failing request, with assertions and a timeout: cleared, still failing, or untested (reproducing needs a write nobody authorized). Target serving but symptom persists: report "version restored; symptom persists; cause unresolved": the bad release may have left data, queued work or state; offer daintree.debug.investigate-outage.

Stop and recovery: one rollback per authorization. After a failed or ambiguous run, read the platform state first; never repeat the command on a guess. Another release is a new request.

Report back: environment; before and after (revision, SHA, traffic) from the platform and the version endpoint; the command, operation id and terminal ids; compatibility findings and what the user accepted; the symptom check; what remains (the bad code is still on the branch; a revert PR is separate work).
