---
id: daintree.debug.investigate-outage
title: Investigate a production incident with read-only agents
summary: Production is broken now — pin the code to the deployed revision, fan out read-only investigators on the deploy diff, the logs and the failing code path, correlate a timeline and ranked suspects, and hand the user mitigation options without executing any.
whenToUse: 'Use when PRODUCTION or another deployed environment is broken right now — an outage or live incident, errors or 500s since a deploy — and the user wants it investigated fast: "prod is down", "prod is on fire, get some agents on it", "errors spiked after the deploy", "get agents looking at the logs, the recent deploys and the diff", "what changed that broke checkout?", "help me triage this incident". The deliverable is a timeline, suspects ranked with evidence (commit or PR, log lines, code path) and mitigation options with their blast radius for the user to choose and run; no agent deploys, rolls back, restarts or changes config. Do not use for a failing test or CI check (use daintree.ci.diagnose-and-fix), a bug in the local dev preview (use daintree.runtime.debug-web-app), reproducing a reported bug that is not a live incident (use daintree.debug.reproduce-bug), finding the commit behind a regression you can reproduce locally (use daintree.git.bisect-regression), Daintree itself misbehaving (use daintree.diagnostics), several agents debating one question to a synthesis (use daintree.orchestration.multi-agent), or carrying out a rollback the user has already chosen (use daintree.release.rollback-deployment).'
risk: project
requiredTools:
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - agent.launch
optionalTools:
  - worktree.list
  - terminal.getStatus
  - terminal.sendCommandOwned
---
Posture: read-only against production, fast, provisional. Investigators dig; you frame, pin, correlate and report. Nobody in this runbook deploys, rolls back, restarts, scales, migrates, flips a flag, edits config or writes production data; the user decides and runs mitigation.

Procedure:
1. Frame in one exchange: the service, the symptom, when it was first seen, and the deployed revision with the last known good one. Take the revisions from the user, a read-only deploy status (`kubectl get deploy -o yaml`, a `/version` endpoint) or the release tag. Only the service and the time are worth blocking on.
2. Pin the code to what is running: local `main` is not production. `git fetch --quiet origin`, then check first: a checkout with empty `git status --porcelain` and `git rev-parse HEAD` equal to the deployed SHA is used as it is. Otherwise `git branch incident/<short sha> <deployed sha>`, then create a worktree on it with no recipe (the one local write here; its setup may run project scripts), then `worktree.waitUntilReady`:

```json tool=worktree.createWithRecipe
{"source":{"kind":"existingBranch","branchName":"incident/ba16ea7"}}
```

   `failed` or `needs-approval` does not block reading code: say so and continue. Deployed SHA unknown: create nothing; launch in the main checkout, and every brief says the code is `main`, not production.
3. Suspect window, only with both SHAs verified: `git log --first-parent --format='%h %s' <good sha>..<deployed sha>`. PRs come from those commits (a `(#123)` suffix, `Merge pull request #123`, else `gh api repos/<owner>/<repo>/commits/<sha>/pulls --jq '.[].number'`), never from a merged-since-date search. Good SHA unknown: no window; the diff lens waits for it and is reported blocked.
4. Investigators, one lens each, as separate `agent.launch` calls (prompts differ) in the incident worktree, distinct `name`s, `notify:true, handback:true`:
   - Diff: the window's changes ranked by risk (migrations, config, dependency bumps, flags, timeouts, retries, caching).
   - Logs: first error time, error signatures and rates, through the CLI access the user already has (`gcloud logging read`, `kubectl logs`, the provider's CLI), read-only.
   - Code path: from the stack trace or failing endpoint to the lines that can produce it at the deployed SHA.
   Every prompt carries the service, symptom, times, both SHAs (or which is unknown), its lens, "stop and report at your first strong lead or after about 10 minutes, whichever comes first, saying what you would check next", and: "Read-only: no deploys, rollbacks, restarts, scaling, migrations, config or flag changes, no writes to production data or the repository, no posts to incident channels. Quote evidence with timestamps; never paste secrets, tokens or personal data."
5. While they run, build the timeline yourself: last good deploy, suspect deploy, first error, spikes, config changes the user mentions. Then end the turn.

```json tool=agent.launch
{"agentId":"claude","name":"Incident: logs","worktreeId":"<incident worktree id>","prompt":"<service, symptom, times, SHAs, lens, read-only line>","notify":true,"handback":true}
```

On notice or resume: each notice is one lens's stop. Fold its evidence into the timeline and tell the user a strong lead as soon as it appears; do not wait for every lens. A suspect needs a mechanism and matching timing; timing alone is a lead, not a cause. Check a named suspect commit is in the window (`git merge-base --is-ancestor <sha> <deployed sha>`). To dig further, send that investigator its next check by `terminal.sendCommandOwned` with `notify:true, handback:true`. An investigator asking to run anything that writes or restarts: the answer is no, sent the same way, and the ask goes to the user.

Stop and recovery: one follow-up per investigator unless the user asks for another round. When every lens has reported once, or the user wants an answer now, issue the report as provisional with any missing lens marked pending. Never retry a mitigation or run one yourself. A mitigation command comes from the repo's scripts, its deploy docs or tooling the user named, never invented; a revert names the suspect commit, not the deploy tip. A revert the user orders is a change (daintree.edits.spawn-visible-agent, then a PR); a rollback runs only on the user's explicit order, via daintree.release.rollback-deployment. A post-incident write-up only if asked. Leave the incident worktree for the user.

Report back: the timeline, suspects ranked with evidence and confidence, causes ruled out, mitigation options (revert PR X, roll back to revision Y, disable flag Z) each with blast radius and its grounded command (or "command unknown") marked as the user's to run, open questions (unknown revisions included), lenses pending, investigator terminal ids, and the incident worktree.
