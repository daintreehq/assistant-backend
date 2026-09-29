---
id: daintree.release.deploy-and-verify
title: Deploy a revision and verify it is serving
summary: Run the project's own deploy once for a pinned revision to the named environment in a visible Daintree pane, then prove from the platform and the service itself that exactly that revision is serving and behaves, not just that the command exited 0.
whenToUse: 'Use when the user wants a revision DEPLOYED to an environment now and confirmed live — "deploy main to staging and check it''s up", "ship v2.5 to production", "push the release out and make sure the new version is serving", "put this build on staging", "redeploy the api from the release tag". The deliverable is the project''s own deploy mechanism (its scripts, Makefile, CI workflow or platform CLI, never an invented one) run once for a pinned SHA to the named environment where the user can watch it, then proof that this SHA is what serves traffic (a version endpoint, the platform''s revision and traffic listing) and passes a smoke check. Do not use for deciding whether to ship (use daintree.release.assess-readiness), going back to an earlier release (use daintree.release.rollback-deployment), a broken production to investigate (use daintree.debug.investigate-outage), any other shell command in a panel (use daintree.terminals.run-visible-command), being told when a deploy someone else started finishes (use daintree.orchestration.watch-outcome), merging a PR (use daintree.forge.land-pull-request; proving the deploy its merge started is this runbook), or publishing a package to a registry (use daintree.release.publish-package).'
risk: external
requiredTools:
  - agent.launch
  - terminal.list
optionalTools:
  - project.detectRunners
  - worktree.list
  - worktree.createWithRecipe
  - worktree.waitUntilReady
  - terminal.getStatus
  - terminal.sendCommandOwned
---
A deploy runs only when the request names the environment, or implies the only one the project has; production needs the user's explicit word ("deploy to prod", "ship to production"). "Ship it" alone names none: ask. The deploy is a visible agent's work; finding the mechanism, pinning the revision and proving what serves are yours.

Procedure:
1. Find the mechanism, reading only: deploy docs; `Makefile`; deploy scripts (`scripts/deploy*`, `infra/`); `.github/workflows/*` with `workflow_dispatch` or a push-to-branch deploy; platform files (`cloudbuild.yaml`, `fly.toml`, `vercel.json`, k8s manifests). `project.detectRunners` lists scripts; read one before trusting its name. Note the exact command or trigger, its target (account or project, service, region, environment), how it picks the revision, how long it takes, and how the project verifies (smoke script, version endpoint). Two or none clear: ask. Never invent a command, flag or credential.
2. Pin from the remote, never a local branch: `git ls-remote origin refs/heads/<branch>` (a tag: `refs/tags/<tag> 'refs/tags/<tag>^{}'`; the `^{}` line is the commit). Say "shipping origin/<branch> at <sha>". The deploy must take that SHA:
   - From a checkout: create one at it (below), then `worktree.waitUntilReady` with its `worktreeId` and `timeoutMs:25000`, again until `setupState:"ready"`; `failed` or `needs-approval`: stop and report. Never a dirty tree or an agent's working worktree.
   - From CI: `gh workflow run --ref` takes a branch or tag, not a SHA: use it only when the workflow takes the SHA as an input, or the ref still resolves to the pin just before dispatch; never create or move a tag to fit. A push-to-branch deploy is a `git push` of exactly that SHA, an outward write needing the user's ask.
   - Already running (a merge's auto-deploy): find that run, pin its `headSha` and go straight to verification; start no second deploy.

```json tool=worktree.createWithRecipe
{"source":{"kind":"newBranch","branchName":"deploy/3fa9c21","baseBranch":"<full sha>","collisionPolicy":"error"}}
```

3. Record what serves NOW, the rollback point (platform revision and image, version endpoint SHA), and the smoke check with what passes (a status AND a body assertion such as the version field). Tell the user the environment, SHA and command. Production without the explicit word: stop here and ask.
4. One deploy at a time, checked just before launching: `terminal.list`, with `terminal.getStatus` on any deploy pane; the platform's in-progress operations; `gh run list --workflow <file> --json status,headSha` for any run not `completed`. One found: stop and report it.
5. `agent.launch` one deploy agent in the deploy checkout (CI: the main one, `worktree.list`), `name` "Deploy: <env> <short sha>", `notify:true, handback:true`; end your turn. Brief: "Run exactly `<command>` once, from <path>. First confirm `git rev-parse HEAD` is <sha> and `git status --porcelain` is empty, else stop. Add no flags; edit, commit, tag or push nothing <unless the mechanism is an authorized push>. Note the run or operation id the moment it is submitted. A CI deploy: follow it with `gh run watch <id> --exit-status`. Past <deadline>: stop following and report it pending with its id. A failure, login or credential prompt, or a scope-widening question (another environment, a migration, force): stop and report; never retry or redispatch. Never print secrets. Reply: command, exit status, run or operation id, the revision, image or URL reported, last 30 output lines."

On notice or resume: exit 0 says the command ended, not that the SHA serves. Verify yourself:
- Platform: the revision taking traffic and its image or source SHA, with the project's tool (`gcloud run services describe <svc> --region <r> --format='value(status.traffic)'`, `kubectl rollout status` then the image, `fly releases`). A new revision with no traffic is not serving; a CI run's `headSha` alone does not prove the artifact.
- Service: the version or build-sha endpoint reports the pin; a CDN lag gets one re-read after a minute.
- Behaviour: step 3's smoke check and assertions, read-only, with a timeout. A status alone proves reachability, not behaviour.
A question within the brief: one answer by `terminal.sendCommandOwned`, `notify:true, handback:true`. Pending, failed or half-done: report the platform state and run id; never re-run unasked.

Stop and recovery: one deploy per authorization. A wrong revision serving or a failed smoke is reported with the rollback point; going back is daintree.release.rollback-deployment, on the user's word.

Report back: environment, pinned SHA and ref, the command, run or operation id, deploy terminal id, result, what serves now (platform revision, traffic, version endpoint) against the pin, the smoke result, the rollback point, and anything unverified.
