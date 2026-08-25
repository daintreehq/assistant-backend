---
name: release
description: "Ship the current branch to production. Gates on GitHub CI being green, fast-forwards main, pushes to origin, then watches the Cloud Build trigger build the image and roll a new Cloud Run revision out — confirming the live /version reports the exact commit that was released. Use when the user says release, ship it, deploy, push to production, or asks to get what is on develop live."
argument-hint: "(optional) --dry-run to show what would ship without pushing anything"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - TaskCreate
  - TaskUpdate
  - AskUserQuestion
---

# Release

Ship what is on the working branch to production. A release here is nothing more
than **getting a commit onto `main` and pushed to origin** — the `deploy-main`
Cloud Build trigger does the rest: build the image, deploy it as a no-traffic
release candidate, smoke-test it, promote it to 100%.

Your job is to make sure the right commit gets there, that it was green before it
went, and that what is actually serving afterwards is what you shipped.

## Constants

| thing | value |
|---|---|
| GCP project | `daintree-research` |
| Cloud Run service | `daintree-assistant` |
| Serving region | `us-central1` |
| Build trigger | `deploy-main` |
| Repo | `daintreehq/assistant-backend` |

The trigger's region is **not** necessarily `us-central1` — it lives wherever the
Developer Connect repository link lives. Discover it once at the start and reuse
it for every `gcloud builds` call:

```bash
BUILD_REGION=$(for r in us-central1 europe-west1 us-east1 us-west1; do
  gcloud builds triggers describe deploy-main --region="$r" --project=daintree-research \
    >/dev/null 2>&1 && echo "$r" && break
done)
```

## Rules

- **Never force-push `main`.** If a merge will not fast-forward, use a merge
  commit; if that conflicts, stop and hand it back to the user.
- **No version tags, no CHANGELOG, no version bumps.** This is not a versioned
  system (see CLAUDE.md, "Versioning") — the commit SHA *is* the version. It tags
  the image, is baked in as `BUILD_SHA`, and comes back from `/version`.
- **No AI attribution** in the merge commit message.
- **Do not run the test suite locally.** GitHub CI already ran it on this exact
  head; use that verdict. Local runs are slow and prove nothing extra.
- **Never use a foreground `sleep`** to wait — the harness blocks it. Poll with
  a backgrounded `until` loop (`run_in_background: true`).
- **Never release red code.** A failed or missing CI run is a full stop, not a
  judgement call.

## Procedure

### 1. Preflight

```bash
git status --porcelain          # must be empty
git fetch origin --prune
git rev-parse --abbrev-ref HEAD # the release branch, usually develop
```

A dirty tree is a stop: report what is uncommitted and let the user decide. Then
confirm the local branch matches its remote (`git rev-list --left-right --count
origin/<branch>...<branch>` must be `0 0`) — releasing something that only exists
on this machine would build a commit GitHub has never tested.

Count what is actually shipping:

```bash
git rev-list --count origin/main..origin/<branch>
```

If that is `0`, nothing new is on the branch. Either main is already current (say
so and stop) or the user wants to redeploy the existing main — in that case skip
to step 4 and fire the trigger manually instead of pushing.

### 2. Show what is shipping

```bash
git log --oneline --no-merges origin/main..origin/<branch>
git diff --stat origin/main...origin/<branch>
```

Summarise it for the user in prose — what changes, and anything that looks
operationally risky (a config default, a prompt template, a new required secret,
a wire-contract change that the CLI would need in lockstep). If the arguments
included `--dry-run`, stop here.

### 3. Gate on GitHub CI

```bash
gh run list --branch <branch> --limit 5 \
  --json headSha,conclusion,status,workflowName,url,createdAt
```

Require a **completed, successful** run whose `headSha` equals the branch head.

- Still running → wait for it with a backgrounded poll loop, then re-check.
- Failed → stop. Report which job failed and its URL. Do not merge.
- No run for this SHA → stop and say so; CI is the only full-suite gate.

### 4. Merge to main and push

```bash
git checkout main
git merge --ff-only origin/main        # sync local main to the remote first
git merge --ff-only <branch>           # preferred: keeps history linear
```

If the fast-forward is refused, `main` has commits the branch does not (a PR
landed directly on main). Merge with a commit instead:

```bash
git merge --no-ff <branch> -m "chore(release): merge <branch> into main"
```

On conflict, stop and hand it back — do not improvise a resolution during a
release. Then record the released commit and push:

```bash
RELEASE_SHA=$(git rev-parse HEAD)
git push origin main
```

If there was nothing to merge and the user wants a redeploy of current main:

```bash
gcloud builds triggers run deploy-main --region="$BUILD_REGION" \
  --branch=main --project=daintree-research
```

### 5. Watch the build

The build takes a few minutes and appears a few seconds after the push. Find it
by commit, then poll it in the background until it settles:

```bash
gcloud builds list --region="$BUILD_REGION" --project=daintree-research \
  --filter="substitutions.COMMIT_SHA=$RELEASE_SHA" \
  --format='value(id,status)' --limit=1
```

Poll `gcloud builds describe <id> --region=$BUILD_REGION --format='value(status)'`
until it is no longer `QUEUED` or `WORKING`. Tell the user the build id and its
log URL while it runs; do not narrate every poll.

**If it ends in anything but `SUCCESS`:**

```bash
gcloud builds log <id> --region="$BUILD_REGION" --project=daintree-research \
  --stream=false > /tmp/release-build.log
tail -80 /tmp/release-build.log
```

Identify the failing step and report it. The `smoke` step failing is the
important case to explain clearly: the candidate revision was deployed with
**no traffic**, so production is untouched and still serving the previous
revision. A `deploy-rc` or `build` failure likewise never reached traffic.

### 6. Verify what is actually live

This is the part that makes it a release rather than a push.

```bash
URL=$(gcloud run services describe daintree-assistant --region=us-central1 \
  --project=daintree-research --format='value(status.url)')

curl -fsS "$URL/health"     # {"status":"ok"}
curl -fsS "$URL/readyz"     # {"status":"ready","catalog_revision":"...","runbooks":N}
curl -fsS "$URL/version"    # build_sha must equal $RELEASE_SHA
```

Use `/health`, never `/healthz`: Google's frontend reserves the `/healthz` path
on Cloud Run and answers it with its own 404, so it is unreachable from outside
the container no matter how healthy the service is. Or just run the script that
already knows all of this:

```bash
./infra/gcp/smoke.sh --expect-env production            # free checks only
./infra/gcp/smoke.sh --expect-env production --full     # + two real turns
```

Pass `--expect-env`: without it the script checks whatever is at the URL and never
notices that it is the wrong deployment, which is precisely the mistake a release
can make. `--full` needs no key — the server holds the upstream credential now, so
those two turns spend DAINTREE's money rather than yours, which is why they stay
opt-in.

`build_sha` disagreeing with `RELEASE_SHA` means traffic is still on an older
revision — say so plainly rather than reporting success. Confirm the rollout:

```bash
gcloud run revisions list --service=daintree-assistant --region=us-central1 \
  --project=daintree-research --limit=3 \
  --format='table(metadata.name,status.conditions[0].status,spec.containers[0].image)'
```

A `/readyz` 503 is a configuration failure — a missing or weak secret, a
malformed prompt template, an unparseable runbook file. The response withholds the
reason in production, so read the real error from the logs:

```bash
gcloud run services logs read daintree-assistant --region=us-central1 \
  --project=daintree-research --limit=50
```

### 7. Resync the working branch

So the working branch never silently drifts behind main:

```bash
git checkout <branch>
git merge --ff-only main
git push origin <branch>
```

### 8. Report

Lead with whether it is live. Then: the commits that shipped, the released SHA,
the revision now serving, the service URL, and the build log URL. If anything was
skipped or degraded, say which and why.

## Rolling back

Traffic moves back to the previous revision immediately — no rebuild:

```bash
gcloud run revisions list --service=daintree-assistant --region=us-central1 \
  --project=daintree-research --limit=5 --format='value(metadata.name)'

gcloud run services update-traffic daintree-assistant --region=us-central1 \
  --project=daintree-research --to-revisions=<previous-revision>=100
```

Then fix forward on the branch and release again. Do not revert `main` to undo a
bad deploy unless the commit itself is the problem — traffic routing is the fast
lever, and git history is not.
