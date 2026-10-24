---
id: daintree.release.deploy-and-verify
title: Deploy a revision and verify it is serving
summary: Run the project's own deploy once for a pinned revision to the named environment in a visible Daintree pane, then prove from the platform and the service itself that exactly that revision is serving and behaves, not just that the command exited 0.
whenToUse: 'Use when the user wants a revision DEPLOYED to an environment now and confirmed live — "deploy main to staging and check it''s up", "ship v2.5 to production", "push the release out and make sure the new version is serving", "put this build on staging", "redeploy the api from the release tag". The deliverable is the project''s own deploy mechanism (its scripts, Makefile, CI workflow or platform CLI, never an invented one) run once for a pinned SHA to the named environment where the user can watch it, then proof that this SHA is what serves traffic (a version endpoint, the platform''s revision and traffic listing) and passes a smoke check. Also when a swarm of hosts shares the work and exactly one host is the deployer of a deploy authorised now: the deploy driven there from this Shell, its check of its own target, and its lost reply reconciled against the platform. Do not use for deciding whether to ship (use daintree.release.assess-readiness), going back to an earlier release (use daintree.release.rollback-deployment), a broken production to investigate (use daintree.debug.investigate-outage), any other shell command in a panel (use daintree.terminals.run-visible-command), being told when a deploy someone else started finishes (use daintree.orchestration.watch-outcome), merging a PR (use daintree.forge.land-pull-request; proving the deploy its merge started is this runbook), or publishing a package to a registry (use daintree.release.publish-package).'
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
  - host.list
  - host.call
---
A deploy runs only when the request names the environment (or the project's only one); production needs the user's explicit word; "ship it" alone names none: ask. A visible agent deploys; you find the mechanism, pin the revision and prove what serves.

Across hosts, one designated host per environment deploys (the person names it, else ask); the others only build and verify (daintree.hosts.run-validation-matrix), no login or deploy. The checkout, the account preflight, step 4's checks and the agent are the deployer's, each through `host.call` there as the hosts foundation says. Pin in your shell; read the platform from it if it can, else on the deployer. A line per environment: host, SHA, run id.

On a host: shell steps run there (its accounts, not yours), per the hosts foundation; own shell: `gh -R <repo>`.

Procedure:
1. Find the mechanism, reading only: docs, `Makefile`, `scripts/deploy*`, `infra/`, CI workflows, platform files (`fly.toml`, `cloudbuild.yaml`, k8s); `project.detectRunners` lists scripts (on a host: Full only; at Core read the manifests by `project.runCommand`). Note the exact command or trigger, its target (account or project, service, region, environment), how it picks the revision, how the project verifies. Two or none clear: ask.
2. Pin from the remote, never a local branch: `git ls-remote origin refs/heads/<branch>` (a tag: `refs/tags/<tag> 'refs/tags/<tag>^{}'`, the `^{}` line is the commit). The deploy must take that SHA:
   - Checkout (a host: `git fetch origin <ref>`, then `git cat-file -e <sha>^{commit}`): `worktree.createWithRecipe` with `source:{"kind":"newBranch","branchName":"deploy/<short sha>","baseBranch":"<full sha>","collisionPolicy":"error"}`, then `worktree.waitUntilReady` until `setupState:"ready"`; `failed` or `needs-approval`: stop and report.
   - CI: `gh workflow run --ref` takes a branch or tag, not a SHA: only if the workflow takes the SHA as input or the ref still resolves to the pin at dispatch; never move a tag. A push-to-branch deploy is a `git push` of that SHA, on the user's ask.
   - Already running (a merge's auto-deploy): pin that run's `headSha`, verify, start no second deploy.
3. Preflight, reading only: the deployer's active account, project or cluster (`gcloud config get project`, `kubectl config current-context`) is the named environment's and may deploy there; else ask. Record what serves NOW, the rollback point (revision, image, version endpoint SHA) and the smoke check with what passes (status AND a body assertion e.g. the version field). Tell the user environment, SHA and command.
4. One deploy at a time, checked just before launch: `terminal.list` (`terminal.getStatus` on a deploy pane), in-progress platform operations, `gh run list --workflow <file> --json status,headSha`. Any run not `completed`: stop and report.
5. `agent.launch` one deploy agent in the deploy checkout (CI: the main one, `worktree.list`), `name` "Deploy: <env> <short sha>", `notify:true, handback:true`. Brief: "Run exactly `<command>` once from <path>, after confirming `git rev-parse HEAD` is <sha> and `git status --porcelain` is empty, else stop. No flags; edit, commit, tag or push nothing <unless the mechanism is an authorized push>. Note the run or operation id when submitted (CI: `gh run watch <id> --exit-status`). A failure, login or credential prompt, or scope-widening question (another environment, migration, force): stop and report, never retry. Reply: command, exit status, run or operation id, revision or URL, last 30 lines."

On notice or resume: exit 0 says the command ended, not that the SHA serves. Verify yourself:
- Platform: the revision taking traffic and its image or source SHA, with the project's tool (`gcloud run services describe <svc> --region <r>` for traffic, `kubectl rollout status` then the image). A new revision with no traffic is not serving.
- Service: the version endpoint reports the pin (CDN lag: one re-read after a minute); step 3's smoke check passes, read-only.
A question within the brief: one answer by `terminal.sendCommandOwned`, `notify:true, handback:true`.

Stop and recovery: one deploy per authorization. A submitted deploy with its answer lost: read the platform's operations and traffic for the pin first (a host you cannot reach: only public reads, the version endpoint); serving: verify; running or neither: report it open with its id. A repeat is a second deploy, on the user's word, from the same host. A wrong revision or failed smoke is reported with the rollback point; going back is daintree.release.rollback-deployment, on the user's word.

Report back: environment, pinned SHA and ref, command, run or operation id, what serves now (platform revision, traffic, version endpoint) against the pin, smoke result, rollback point, the deployer host's claims vs what you read, anything unverified.
