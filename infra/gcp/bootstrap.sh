#!/usr/bin/env bash
#
# One-time (and safely repeatable) GCP setup for the Daintree Assistant Server's
# continuous deployment pipeline. Creates everything cloudbuild.yaml assumes:
# the Artifact Registry repository, the two Secret Manager secrets, a runtime
# service account and a build service account with least-privilege roles, and
# the Cloud Build trigger that fires on every push to main.
#
# Idempotent by design — every step checks before it creates, so re-running is
# how you finish the setup rather than something to avoid. In particular the
# public-invoker grant at the end needs the Cloud Run service to exist, which
# only happens once the first build has run. The intended sequence is:
#
#     ./infra/gcp/bootstrap.sh     # everything except the invoker grant
#     <release to main>            # the trigger builds and creates the service
#     ./infra/gcp/bootstrap.sh     # picks up where it left off, grants invoker
#
# Secret VALUES are generated here and never leave Secret Manager. Re-running
# will NOT rotate an existing secret: rotating the state signing key would
# invalidate every outstanding state token, so that is a deliberate manual act.
#
# What the server needs at runtime is only state-signing material. There is no
# server-side model API key — every request's bearer token is the caller's own
# OpenRouter key, which both authenticates the request and funds its upstream
# calls (see README.md and docs/DAINTREE_API.md).

set -euo pipefail

PROJECT="${PROJECT:-daintree-research}"
REGION="${REGION:-us-central1}"              # Cloud Run + Artifact Registry
SERVICE="${SERVICE:-daintree-assistant}"
AR_REPO="${AR_REPO:-daintree}"
RUNTIME_SA="${RUNTIME_SA:-daintree-assistant-run}"
BUILD_SA="${BUILD_SA:-daintree-deploy}"
TRIGGER="${TRIGGER:-deploy-main}"
CONNECTION="${CONNECTION:-daintree-github}"
GITHUB_OWNER="${GITHUB_OWNER:-daintreehq}"
GITHUB_REPO="${GITHUB_REPO:-assistant-backend}"
DOMAIN="${DOMAIN:-assistant.daintree.org}"

RUNTIME_SA_EMAIL="${RUNTIME_SA}@${PROJECT}.iam.gserviceaccount.com"
BUILD_SA_EMAIL="${BUILD_SA}@${PROJECT}.iam.gserviceaccount.com"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()  { printf '    %s\n' "$*"; }

# IAM is eventually consistent: a service account is routinely not yet visible to
# the policy APIs for a few seconds after it is created, which fails any binding
# that references it. Retry rather than making the caller re-run the script.
retry() {
  local attempts="$1"; shift
  local n=1
  until "$@"; do
    if [ "$n" -ge "$attempts" ]; then
      echo "    giving up after ${attempts} attempts: $*" >&2
      return 1
    fi
    ok "not ready yet, retrying ($n/$attempts)"
    sleep 5
    n=$((n + 1))
  done
}

# True when the secret already has at least one version, in any state. Any
# version at all means hands off: adding another would silently rotate the state
# signing key and invalidate every state token already in the wild.
has_version() {
  [ -n "$(gcloud secrets versions list "$1" --project="$PROJECT" \
            --format='value(name)' --limit=1 2>/dev/null)" ]
}

# ---------------------------------------------------------------------------
say "Project"
gcloud config set project "$PROJECT" --quiet >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
ok "$PROJECT (number $PROJECT_NUMBER), serving region $REGION"

# ---------------------------------------------------------------------------
say "APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  developerconnect.googleapis.com \
  --project "$PROJECT" --quiet
ok "enabled"

# ---------------------------------------------------------------------------
say "Artifact Registry"
if gcloud artifacts repositories describe "$AR_REPO" \
     --location="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "repository $AR_REPO already exists"
else
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker --location="$REGION" --project="$PROJECT" \
    --description="Daintree Assistant Server container images"
  ok "created $AR_REPO"
fi

# ---------------------------------------------------------------------------
say "Secrets"
# STATE_SIGNING_KEY_CURRENT must be >=32 chars and STATE_SALT must not be the
# packaged default, or production readiness fails (Settings.weak_secrets).
ensure_secret() {
  local name="$1" generator="$2"
  if ! gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic --project="$PROJECT" >/dev/null
    ok "created secret $name"
  fi
  if has_version "$name"; then
    ok "$name already has a version (left alone — rotation is manual)"
  else
    printf '%s' "$($generator)" \
      | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" >/dev/null
    ok "added the first version of $name"
  fi
}
gen_key()  { openssl rand -hex 32; }   # 64 chars, comfortably over the 32 minimum
gen_salt() { openssl rand -hex 16; }
ensure_secret state-signing-key-current gen_key
ensure_secret state-salt gen_salt

# ---------------------------------------------------------------------------
say "Runtime service account (what the container runs as)"
if gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  ok "$RUNTIME_SA_EMAIL already exists"
else
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name="Daintree Assistant Cloud Run runtime" --project="$PROJECT"
  ok "created $RUNTIME_SA_EMAIL"
fi
# Scoped per-secret rather than project-wide: this identity can read exactly the
# secrets it mounts and nothing else in the project.
for s in state-signing-key-current state-salt; do
  retry 12 gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor \
    --project="$PROJECT" --quiet >/dev/null
done
ok "granted secretAccessor on state-signing-key-current, state-salt"

# The rotation-overlap secret, granted only if it exists. It is created by hand at
# the start of a rotation and deleted once the old token TTL has expired, so it is
# absent most of the time — but a rotating deploy MOUNTS it, and a mount the
# runtime identity cannot read fails the revision at startup. Re-run bootstrap
# after creating it (it is idempotent), or the rotation release will not come up.
if gcloud secrets describe state-signing-key-previous --project="$PROJECT" >/dev/null 2>&1; then
  retry 12 gcloud secrets add-iam-policy-binding state-signing-key-previous \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor \
    --project="$PROJECT" --quiet >/dev/null
  ok "granted secretAccessor on state-signing-key-previous (rotation in progress)"
else
  ok "no state-signing-key-previous — not in a rotation window, nothing to grant"
fi

# ---------------------------------------------------------------------------
say "Build service account (what Cloud Build runs as)"
if gcloud iam service-accounts describe "$BUILD_SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  ok "$BUILD_SA_EMAIL already exists"
else
  gcloud iam service-accounts create "$BUILD_SA" \
    --display-name="Daintree Assistant Cloud Build deployer" --project="$PROJECT"
  ok "created $BUILD_SA_EMAIL"
fi
# logWriter is required because cloudbuild.yaml sets logging: CLOUD_LOGGING_ONLY;
# without it the build fails before running a step. builds.builder is what Cloud
# Build itself checks when a trigger names a user-specified service account —
# without it, trigger creation is rejected outright with PERMISSION_DENIED.
# secretmanager.viewer is the deploy's own need, not the app's: cloudbuild.yaml
# asks whether `state-signing-key-previous` EXISTS to decide whether to mount it,
# and without `secrets.get` that question comes back PERMISSION_DENIED. The build
# now fails closed on that rather than reading it as "no previous key", so the
# missing grant would break every deploy rather than silently skipping the
# rotation — but the grant is what makes it work at all. Viewer reads metadata
# only; it cannot read a secret's VALUE.
for role in roles/cloudbuild.builds.builder \
            roles/logging.logWriter \
            roles/artifactregistry.writer \
            roles/secretmanager.viewer \
            roles/run.admin; do
  retry 12 gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${BUILD_SA_EMAIL}" --role="$role" \
    --condition=None --quiet >/dev/null
  ok "granted $role"
done
# Deploying a service that runs AS the runtime SA requires impersonation rights
# over that one account — granted on the SA resource, not project-wide.
retry 12 gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL" \
  --member="serviceAccount:${BUILD_SA_EMAIL}" \
  --role=roles/iam.serviceAccountUser \
  --project="$PROJECT" --quiet >/dev/null
ok "granted serviceAccountUser on $RUNTIME_SA"

# ---------------------------------------------------------------------------
say "GitHub repository connection"
# Cloud Build triggers require a Cloud Build *v2 connection* plus a repository
# resource under it. Note that the Cloud Run console's "connect a repository"
# wizard creates a DEVELOPER CONNECT link instead, which looks equivalent but is
# a different resource — passing one to a trigger fails with "Invalid GCB repo
# name". So the Cloud Build kind is created explicitly here, in $REGION, which
# also keeps builds, registry, and service in a single region.
#
# Creating the connection stores a GitHub OAuth token in Secret Manager on the
# caller's behalf, which is why Cloud Build's own service agent needs to be able
# to manage secrets.
retry 6 gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-cloudbuild.iam.gserviceaccount.com" \
  --role=roles/secretmanager.admin --condition=None --quiet >/dev/null
ok "Cloud Build service agent can manage connection secrets"

if gcloud builds connections describe "$CONNECTION" \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "connection $CONNECTION already exists"
else
  gcloud builds connections create github "$CONNECTION" \
    --region="$REGION" --project="$PROJECT"
  ok "created connection $CONNECTION"
fi

# The OAuth handshake and GitHub App install are interactive and cannot be
# scripted. Stop with the link rather than failing further down.
STAGE="$(gcloud builds connections describe "$CONNECTION" --region="$REGION" \
           --project="$PROJECT" --format='value(installationState.stage)')"
if [ "$STAGE" != "COMPLETE" ]; then
  printf '    connection is %s and needs a one-time GitHub authorization.\n' "$STAGE"
  printf '    Open this, authorize Cloud Build, install its GitHub App on the %s\n' "$GITHUB_OWNER"
  printf '    org with access to %s, then re-run this script:\n\n' "$GITHUB_REPO"
  gcloud builds connections describe "$CONNECTION" --region="$REGION" \
    --project="$PROJECT" --format='value(installationState.actionUri)'
  exit 1
fi
ok "connection authorized"

if gcloud builds repositories describe "$GITHUB_REPO" \
     --connection="$CONNECTION" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "repository ${GITHUB_OWNER}/${GITHUB_REPO} already linked"
else
  gcloud builds repositories create "$GITHUB_REPO" \
    --connection="$CONNECTION" --region="$REGION" --project="$PROJECT" \
    --remote-uri="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git"
  ok "linked ${GITHUB_OWNER}/${GITHUB_REPO}"
fi

# ---------------------------------------------------------------------------
say "Cloud Build trigger"
if gcloud builds triggers describe "$TRIGGER" \
     --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "trigger $TRIGGER already exists"
else
  gcloud builds triggers create github \
    --name="$TRIGGER" \
    --region="$REGION" \
    --project="$PROJECT" \
    --repository="projects/${PROJECT}/locations/${REGION}/connections/${CONNECTION}/repositories/${GITHUB_REPO}" \
    --branch-pattern='^main$' \
    --build-config=cloudbuild.yaml \
    --substitutions="_SERVICE=${SERVICE},_REGION=${REGION},_AR_REPO=${AR_REPO},_RUNTIME_SA=${RUNTIME_SA}" \
    --service-account="projects/${PROJECT}/serviceAccounts/${BUILD_SA_EMAIL}" \
    --description="Build and deploy ${SERVICE} on every push to main"
  ok "created trigger $TRIGGER in $REGION"
fi

# ---------------------------------------------------------------------------
say "Public access to the service"
# The app's own bearer auth is the gate, not Google IAM: every request carries
# the caller's OpenRouter key, and the unauthenticated surface is limited to
# /healthz, /readyz, and /version. Granting allUsers roles/run.invoker is what
# makes the service reachable at all. This step needs the service to exist, so
# on a first run it is expected to be skipped.
if gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --region="$REGION" --project="$PROJECT" \
    --member=allUsers --role=roles/run.invoker --quiet >/dev/null
  URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
           --project="$PROJECT" --format='value(status.url)')"
  ok "granted allUsers roles/run.invoker"
  ok "service URL: $URL"
else
  ok "service does not exist yet — release to main, then re-run this script"
fi

# ---------------------------------------------------------------------------
say "Custom domain"
# Cloud Run's built-in domain mapping: a CNAME straight at the service, no load
# balancer and no cost. It is deliberately the smaller of the two options — the
# global external Application Load Balancer (reserved anycast IP, Cloud Armor,
# CDN) is documented in infra/terraform/README.md and can replace this later by
# swapping the CNAME for an A record. Nothing about the service changes.
#
# Google must already believe you own the domain: verify it once at
# https://search.google.com/search-console (verifying the apex daintree.org
# covers every subdomain). That step is interactive and cannot be scripted.
if ! gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "service does not exist yet — skipping (re-run after the first release)"
elif gcloud beta run domain-mappings describe --domain="$DOMAIN" \
       --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "$DOMAIN is already mapped"
  gcloud beta run domain-mappings describe --domain="$DOMAIN" \
    --region="$REGION" --project="$PROJECT" \
    --format='table(status.resourceRecords[].name,status.resourceRecords[].type,status.resourceRecords[].rrdata)'
elif gcloud beta run domain-mappings create --service="$SERVICE" --domain="$DOMAIN" \
       --region="$REGION" --project="$PROJECT" 2>/dev/null; then
  ok "mapped $DOMAIN -> $SERVICE"
  ok "DNS: CNAME $DOMAIN -> ghs.googlehosted.com."
else
  cat <<EOF
    Could not create the mapping for ${DOMAIN}. The usual cause is that the
    domain is not verified yet. Verify it once here, then re-run this script:
      https://search.google.com/search-console
    DNS record to add at your registrar (safe to add before the mapping exists):
      CNAME  assistant  ->  ghs.googlehosted.com.
EOF
fi

say "Done"
