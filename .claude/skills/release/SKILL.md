---
name: release
description: "Ship the current branch to production. Does not wait for GitHub CI: fast-forwards main, pushes to origin, then watches the Cloud Build trigger build the image and roll a new Cloud Run revision out (or, with no gcloud login, watches the public service instead) — confirming the live /version reports the exact commit that was released. Use when the user says release, ship it, deploy, push to production, or asks to get what is on develop live."
argument-hint: "(optional) --dry-run to show what would ship without pushing anything"
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
  - TaskCreate
  - TaskUpdate
  - Monitor
  - AskUserQuestion
---

# Release

Ship what is on the working branch to production. A release here is nothing more
than **getting a commit onto `main` and pushed to origin** — the `deploy-main`
Cloud Build trigger does the rest: build the image, deploy it as a no-traffic
release candidate, smoke-test it, promote it to 100%.

Your job is to make sure the right commit gets there, that it was green before it
went, and that what is actually serving afterwards is what you shipped.

## Deployment settings

Set `PROJECT` from your local maintainer environment before any cloud operation.
Do not add account identifiers or credentials to this file.

| thing | value |
|---|---|
| GCP project | `$PROJECT`, set explicitly by the maintainer |
| Cloud Run service | `daintree-assistant` |
| Serving region | `us-central1` |
| Build trigger | `deploy-main` |
| Repo | `daintreehq/assistant-backend` |
| Public URL | `https://assistant.daintree.org` |

The trigger's region is **not** necessarily `us-central1` — it lives wherever the
Developer Connect repository link lives. In gcloud mode, discover it once in
preflight (after the auth check) and reuse it for every `gcloud builds` call:

```bash
BUILD_REGION=$(for r in us-central1 europe-west1 us-east1 us-west1; do
  gcloud builds triggers describe deploy-main --region="$r" --project="$PROJECT" \
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
- **Do not wait for GitHub CI and do not run the test suite.** CI is a report, not a
  release gate. What protects production is Cloud Build itself: it qualifies the
  commit's catalog and model inside the built image (the serving tests, `bench
  coverage` and `bench local`; `benchmarks/qualify_release.py`), deploys it as a
  no-traffic candidate, runs a real search against it (`smoke.sh`), and promotes it
  only if all of that passes. A red routing gate on `develop` therefore stops the
  release in the `qualify` step: report it, never loosen a floor to get past it.
- **Never use a foreground `sleep`** to wait — the harness blocks it. Watch the build
  with the Monitor tool (step 5).
- **Pick the mode before the push, never after.** `gh` must work. `gcloud` is
  optional: the push to `main` is the deploy, and a login only lets you watch it.
  With one, the build is watched step by step (**gcloud mode**). Without one, the
  release still goes out and is watched through the public `/version` (**public
  mode**). The verdict on what is live is the same. What public mode lacks until
  someone logs in: the build log when a release fails, the revision and traffic
  check, the service logs, and rollback. Preflight decides the mode, so an expired
  login is a choice made before `main` moves, not something found out after.

## Procedure

### 1. Preflight

```bash
git status --porcelain          # must be empty
git fetch origin --prune
git rev-parse --abbrev-ref HEAD # the release branch, usually develop
```

Prove both CLIs are authenticated, non-interactively:

```bash
gh auth status >/dev/null 2>&1 || echo "gh: not logged in"
gcloud auth print-access-token --quiet >/dev/null 2>&1 || echo "gcloud: token expired"
```

`gh` failing is a stop: tell the user to run `! gh auth login` and pick up from
here. `gcloud` working means gcloud mode: discover `BUILD_REGION` (see Constants).
`gcloud` failing means public mode. Do not stop. Tell the user in one line that
the release will be watched through the public URL, and that `! gcloud auth
login` at any point brings back the build log and rollback.

Refuse to start a release while another `deploy-main` build is still running.
`cloudbuild.yaml` has no ordering guard, so if two builds overlap the slower
one's `promote` can land after the newer one and quietly put the older commit back
on 100%. In gcloud mode:

```bash
gcloud builds list --region="$BUILD_REGION" --project="$PROJECT" \
  --filter='status=(QUEUED OR WORKING)' --format='value(id,substitutions.COMMIT_SHA)'
```

If anything is listed, watch it to completion with `watch-build.sh` (step 5) and
only then continue.

In public mode, compare the last push to `main` with what is serving:

```bash
gh api 'repos/daintreehq/assistant-backend/activity?ref=refs/heads/main&per_page=1' \
  --jq '.[0] | "\(.after) \(.timestamp | fromdateiso8601)"'   # <sha> <pushed-at epoch>
curl -fsS https://assistant.daintree.org/version              # build_sha
```

The same SHA in both means nothing is in flight. If they differ and that push was
under 35 minutes ago, its build may still be running: watch it with
`watch-live.sh <sha> <pushed-at>` (step 5) and only then continue. If they differ
and the push is older, its build is over and never went live. Tell the user, since
this release ships that commit again, then continue.

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
to step 4 and fire the trigger manually instead of pushing. Firing the trigger
needs gcloud: in public mode, ask for `! gcloud auth login` first.

### 2. Show what is shipping

```bash
git log --oneline --no-merges origin/main..origin/<branch>
git diff --stat origin/main...origin/<branch>
```

Summarise it for the user in prose — what changes, and anything that looks
operationally risky (a config default, a runbook that changes routing, a change to
the MCP tool contract that callers depend on). If the arguments
included `--dry-run`, stop here.

### 3. GitHub CI is not a gate

Do not wait for it. If a run for this head has already FAILED, glance at which job
and mention it in the report, but release anyway unless the failure is in the app
itself starting (then the Cloud Build smoke step would stop it too).

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
PUSHED_AT=$(date +%s)
git push origin main
```

If there was nothing to merge and the user wants a redeploy of current main:

```bash
gcloud builds triggers run deploy-main --region="$BUILD_REGION" \
  --branch=main --project="$PROJECT"
```

### 5. Watch the build

In public mode, skip to "Public mode" below. In gcloud mode, run `watch-build.sh`
(next to this file) under the **Monitor** tool, with
`timeout_ms: 1800000` (the Monitor cap); the build's own timeout is 3600s, so
re-arm it once if it expires with the build still running:

```bash
.claude/skills/release/watch-build.sh "$RELEASE_SHA" "$BUILD_REGION"
```

It finds the build for that commit from this trigger, waiting up to 5 minutes for
it to appear. Then it polls `gcloud builds describe` every 20s and prints only
changes:

- `BUILD <id> <console url>`: pass the URL on to the user once.
- `STEP <id>=<status>`: one line per step transition.
- `DONE <status> <id> [detail]`: the build's verdict. The script exits 0 only on
  `SUCCESS`.
- `ERROR ...`: no build appeared, or gcloud failed 5 times in a row (usually auth
  expiring mid-watch). This means **unknown**, not failed. If the login expired,
  carry on in public mode with `watch-live.sh "$RELEASE_SHA" "$PUSHED_AT"`.
  Otherwise check the console before you report anything.

It polls rather than using `gcloud builds log --stream`, because that command's
exit code does not reliably reflect how the build ended.

Pay attention to step events before the build finishes. `deploy-rc`, `smoke` and
`promote` have `allowFailure`, so one of them can show `STEP smoke=FAILURE` while
the build keeps running. `release-gate` still has to remove the candidate tag
before it turns the build red. As soon as you see that line you know the release
will not go live, and you can start reading the log. Don't narrate the other
steps.

If no build appears, check the trigger history in the console before firing
`gcloud builds triggers run` by hand. A late webhook plus a manual run means two
overlapping builds, which is exactly the risk preflight guards against.

**If it ends in anything but `SUCCESS`:**

```bash
gcloud builds log <id> --region="$BUILD_REGION" --project="$PROJECT" \
  --stream=false > /tmp/release-build.log
tail -80 /tmp/release-build.log
```

Identify the failing step and report it. `TIMEOUT` means the whole build ran past
3600s. Steps it never reached show `CANCELLED`, and it may have stranded an
`rc-` tag (see CLAUDE.md, "Deploying"). The `smoke` step failing is the
important case to explain clearly: the candidate revision was deployed with
**no traffic**, so production is untouched and still serving the previous
revision. A `qualify`, `qualify-record`, `deploy-rc` or `build` failure likewise
never reached traffic; `qualify` prints a PASS/FAIL line per check.

#### Public mode

Run `watch-live.sh` (next to this file) under the **Monitor** tool, with
`timeout_ms: 1800000`, re-armed once if it expires before its own deadline:

```bash
.claude/skills/release/watch-live.sh "$RELEASE_SHA" "$PUSHED_AT"
```

It polls the public `/version` every 20s and prints only changes:

- `SERVING <sha>`: what is live now (`unreachable` if `/version` does not answer).
  The first line is the old commit, and that is expected while the build runs.
- `LIVE <sha>`: the release is serving. Exits 0.
- `DEADLINE ...`: 35 minutes after the push, the commit is still not live. Exits 1.

Its deadline counts from the push, so a Monitor expiry before `LIVE` or
`DEADLINE` is not a verdict. Re-arm it with the same arguments. `DEADLINE` means
the build failed or timed out, and production is untouched: a failed build never
takes traffic. Say so, and say the reason is in the build log, which needs
`! gcloud auth login`. Once logged in, find the build with `gcloud builds list
--filter="substitutions.COMMIT_SHA=$RELEASE_SHA"` and read it as above.

### 6. Verify what is actually live

This is the part that makes it a release rather than a push.

```bash
URL=$(gcloud run services describe daintree-assistant --region=us-central1 \
  --project="$PROJECT" --format='value(status.url)')
# public mode: URL=https://assistant.daintree.org

curl -fsS "$URL/health"     # {"status":"ok"}
curl -fsS "$URL/readyz"     # {"status":"ready","catalog_revision":"...","runbooks":N}
curl -fsS "$URL/version"    # build_sha must equal $RELEASE_SHA
```

Use `/health`, never `/healthz`: Google's frontend reserves the `/healthz` path
on Cloud Run and answers it with its own 404, so it is unreachable from outside
the container no matter how healthy the service is. Or just run the script that
already knows all of this:

```bash
./infra/gcp/smoke.sh --expect-build-sha "$RELEASE_SHA"
```

Pass `--expect-build-sha`. Without it the script checks whatever is at the URL
and won't notice an older commit, which is exactly the mistake a release can
make. It is free: it runs one real search through the shipped selector, which
makes no model call. In public mode, also pass `--url https://assistant.daintree.org`,
since finding the URL is the script's only use of gcloud.

`build_sha` disagreeing with `RELEASE_SHA` means traffic is still on an older
revision — say so plainly rather than reporting success. The checks below need
gcloud. In public mode, skip them and report them as not checked. Confirm which
revision holds the traffic and that the service is Ready:

```bash
gcloud run services describe daintree-assistant --region=us-central1 \
  --project="$PROJECT" \
  --format='value(status.traffic.filter(percent=100).revisionName,status.conditions.filter(type=Ready).status)'
```

That should print the new revision followed by `True`. Also check the
`commit-sha` label on that revision (`gcloud run revisions describe <rev>
--format='value(metadata.labels.commit-sha)'`), which should be the short form of
`RELEASE_SHA`.

A `/readyz` 503 is a startup failure — an unparseable runbook file, or a
missing or corrupt selector artifact. The response withholds the
reason in production, so read the real error from the logs:

```bash
gcloud run services logs read daintree-assistant --region=us-central1 \
  --project="$PROJECT" --limit=50
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
skipped or degraded, say which and why. In public mode, the revision and the build
log URL were not checked.

## Rolling back

Rolling back needs gcloud: in public mode, ask for `! gcloud auth login` first.
Traffic moves back to the previous revision immediately — no rebuild:

```bash
gcloud run revisions list --service=daintree-assistant --region=us-central1 \
  --project="$PROJECT" --limit=5 --format='value(metadata.name)'

gcloud run services update-traffic daintree-assistant --region=us-central1 \
  --project="$PROJECT" --to-revisions=<previous-revision>=100
```

Then fix forward on the branch and release again. Do not revert `main` to undo a
bad deploy unless the commit itself is the problem — traffic routing is the fast
lever, and git history is not.
