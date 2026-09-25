# Deploying Daintree runbook search

The service is a stateless, unauthenticated MCP server with one tool,
`search_runbooks`, served at:

```
https://assistant.daintree.org/v1/daintree/mcp
```

It holds no secret, calls no upstream, and keeps no state between requests. The
only other routes are `/health`, `/healthz`, `/readyz`, `/version` (build metadata
only) and a redirect at `/`.

The deployment is `cloudbuild.yaml`: pushing to `main` builds an image, raises a
no-traffic candidate, smokes it, promotes it, and retires the candidate's URL.
Nothing is built or pushed from a developer machine. The scripts here support
that pipeline — they do not replace it.

| file | what it is |
|---|---|
| `bootstrap.sh` | idempotent one-time GCP setup: registry, both service accounts, the GitHub connection, the build trigger, the domain mapping, and the public cutover when asked for |
| `smoke.sh` | the assertion suite. Run by hand against any host, and run by the build against the candidate as the pre-promotion gate |
| `rollback.sh` | moves traffic back to the previous Ready revision, selected by name |

## The pipeline

1. **`substitution-guard`** refuses any of `_SERVICE`, `_REGION`, `_AR_REPO`,
   `_RUNTIME_SA`, `COMMIT_SHA` or `SHORT_SHA` that is not a plain name or a hex
   sha. Those six are expanded into shell source rather than passed as
   environment, so a value outside that alphabet would be a program. A new
   substitution written into a gcloud flag must be added to the guard or passed
   as an environment variable; a test fails otherwise.
2. **`release-state-init`** empties `/release`, a build-scoped volume, and stamps
   it with the build id. Every marker the later steps write opens with that id,
   and every read checks it, because those markers are the release's verdict and
   `/workspace` is the repository checkout.
3. **`fetch-lfs`, `build`, `push`** — the selector model is in Git LFS; the build
   fails if either encoder is still a pointer file.
4. **`deploy-rc`** validates `_ENVIRONMENT` (`development|test|staging|production`)
   and `_INGRESS` (`all|internal|internal-and-cloud-load-balancing`), writes the
   revision's whole environment — `ENVIRONMENT` and `LOG_LEVEL`, nothing else —
   and deploys with `--clear-secrets`. It then reads the revision back and fails
   if it carries a variable the build did not generate, is missing one, or mounts
   any secret at all.
5. **`smoke`** runs `smoke.sh --url <candidate> --expect-build-sha <commit>`
   against the candidate's own tag URL.
6. **`promote`** moves 100% of traffic to the revision the smoke proved — by name,
   never `--to-latest` — and removes this build's tag in the same call.
7. **`release-gate`** always runs: it retires this build's `rc-<build>` tag if it
   is still attached, then fails the build if any earlier step did not record
   success.

Whether the service exists is read once, before the deploy, and "absent" and
"unreadable" are different answers. An existing service gets a no-traffic
`rc-<build>` candidate. A missing one is created at full traffic behind a
`/readyz` check with an identity token (a new service is private, so there is no
anonymous URL to smoke). Anything else — a denied permission, a transient API
error — refuses rather than guessing.

**Not guaranteed:** a build that is cancelled, or hits the top-level timeout,
kills `release-gate` with it and leaves the tag attached. Check after a cancelled
release:

```bash
gcloud run services describe daintree-assistant --region=us-central1 \
  --format='value(status.traffic[].tag)'
```

## Public access

A new service is IAM-private, and `bootstrap.sh` never changes that as a side
effect of finishing a setup. Granting `allUsers roles/run.invoker` is a cutover
you ask for by naming the service:

```bash
PUBLIC_CUTOVER=daintree-assistant ./infra/gcp/bootstrap.sh
```

The script first runs `smoke.sh --identity-token` against the deployed revision
and refuses the grant unless it passes. It never removes an existing public
binding; revoking is a command you run yourself:

```bash
gcloud run services remove-iam-policy-binding daintree-assistant \
  --region=us-central1 --member=allUsers --role=roles/run.invoker
```

An invoker binding is per-SERVICE, so on a public service every candidate tag URL
is public too. That is harmless here — the endpoint is meant to be public and
holds nothing — but it is why the pipeline retires the tag on every path.

## Reading a deployment

```bash
./infra/gcp/smoke.sh --url https://assistant.daintree.org
./infra/gcp/smoke.sh --url https://assistant.daintree.org --expect-build-sha <commit>
./infra/gcp/smoke.sh --identity-token        # a service that is still private
```

Free: a search runs the local classifier. The suite checks `/health`, `/readyz`
(ready, non-empty catalog), `/version`, MCP `initialize` and `tools/list`, a real
`search_runbooks` call, that the search and `/readyz` agree on the catalog
revision, and that `GET` on the MCP endpoint is 405.

Cloud Run reserves `/healthz` and answers it itself, so an external probe uses
`/health` or `/readyz`. `/healthz` still serves the container's own probes.

## Rolling back

```bash
./infra/gcp/rollback.sh --list        # what is available
./infra/gcp/rollback.sh               # show the plan, ask, then roll back
./infra/gcp/rollback.sh --to daintree-assistant-00042-abc
```

Rolling forward again is the same command, `--to` the revision you left. The next
push to `main` releases normally over it.
