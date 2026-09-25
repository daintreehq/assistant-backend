#!/usr/bin/env bash
#
# One-time (and safely repeatable) GCP setup for the Daintree runbook search
# service's continuous deployment pipeline. Creates everything cloudbuild.yaml
# assumes: the Artifact Registry repository, a runtime service account and a
# build service account with least-privilege roles, the GitHub connection, and
# the Cloud Build trigger that fires on every push to main.
#
# Idempotent by design — every step checks before it creates, so re-running is
# how you finish the setup rather than something to avoid. Some steps need the
# Cloud Run service to exist, which only happens once the first build has run.
# The intended sequence is:
#
#     ./infra/gcp/bootstrap.sh     # everything a build needs to run
#     <release to main>            # the trigger builds and creates the service
#     ./infra/gcp/bootstrap.sh     # picks up the domain mapping and the rest
#
# A NEW service is created private, and re-running never changes that on its
# own. Making it public — which an MCP server anyone can add must be — is a
# deliberate cutover; see the "Public access" section at the bottom of this file.
#
# The service holds no secrets. The only Secret Manager secret involved is the
# GitHub connection's token, which the build reads to fetch the LFS model.

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
    --description="Daintree container images"
  ok "created $AR_REPO"
fi

# ---------------------------------------------------------------------------
say "Runtime service account (what the container runs as)"
if gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  ok "$RUNTIME_SA_EMAIL already exists"
else
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name="Daintree runbook search Cloud Run runtime" --project="$PROJECT"
  ok "created $RUNTIME_SA_EMAIL"
fi

# ---------------------------------------------------------------------------
say "Build service account (what Cloud Build runs as)"
if gcloud iam service-accounts describe "$BUILD_SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  ok "$BUILD_SA_EMAIL already exists"
else
  gcloud iam service-accounts create "$BUILD_SA" \
    --display-name="Daintree runbook search Cloud Build deployer" --project="$PROJECT"
  ok "created $BUILD_SA_EMAIL"
fi
# logWriter is required because cloudbuild.yaml sets logging: CLOUD_LOGGING_ONLY;
# without it the build fails before running a step. builds.builder is what Cloud
# Build itself checks when a trigger names a user-specified service account —
# without it, trigger creation is rejected outright with PERMISSION_DENIED.
for role in roles/cloudbuild.builds.builder \
            roles/logging.logWriter \
            roles/artifactregistry.writer \
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

# The Developer Connect connection's GitHub OAuth token. The fetch-lfs build
# step reads it to pull LFS content (the model artifact) from the PRIVATE repo
# — the v2 checkout leaves no git credential behind. The secret's name carries
# a random suffix minted by Developer Connect, so it is discovered, not
# hardcoded. Granted here, after both the build service account and the
# connection exist, so neither is referenced before it is created.
GH_TOKEN_SECRET=$(gcloud secrets list --project="$PROJECT"   --filter="name~daintree-github-github-oauthtoken"   --format="value(name)" --limit=1 || true)
if [ -n "$GH_TOKEN_SECRET" ]; then
  retry 12 gcloud secrets add-iam-policy-binding "$GH_TOKEN_SECRET"     --member="serviceAccount:${BUILD_SA_EMAIL}"     --role=roles/secretmanager.secretAccessor     --project="$PROJECT" --quiet >/dev/null
  ok "granted deploy SA secretAccessor on $GH_TOKEN_SECRET (LFS fetch)"
else
  ok "!! no daintree-github oauthtoken secret found — LFS fetch will fail until granted"
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
# `allUsers roles/run.invoker` is what makes the MCP endpoint reachable by
# anybody. It is granted as a CUTOVER — once, on purpose, by naming the service —
# never as a side effect of finishing a setup:
#
#     PUBLIC_CUTOVER=daintree-assistant ./infra/gcp/bootstrap.sh
#
# Naming the service is the intent; the deployed revision is then read back
# with an identity token (it is still private) and must be ready and answering
# a runbook search before the binding is created.
#
# This section never REMOVES an existing public binding. Taking it away is an
# outage, so revoking is a command an operator runs deliberately.
PUBLIC_CUTOVER="${PUBLIC_CUTOVER:-}"
if ! gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" >/dev/null 2>&1; then
  ok "service does not exist yet — release to main, then re-run this script"
else
  URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
           --project="$PROJECT" --format='value(status.url)')"
  ok "service URL: $URL"
  # mktemp rather than a fixed name in /tmp. Two operators bootstrapping two
  # services on one machine would otherwise read each other's policy — and the
  # thing this file decides on the strength of that read is whether to publish a
  # service, which is the last decision that should depend on a shared filename.
  POLICY_FILE="$(mktemp)"
  trap 'rm -f "$POLICY_FILE"' EXIT
  gcloud run services get-iam-policy "$SERVICE" --region="$REGION" \
    --project="$PROJECT" --format=json > "$POLICY_FILE"
  ALREADY_PUBLIC="$(POLICY_FILE="$POLICY_FILE" python3 - <<'PYEOF'
import json, os

with open(os.environ["POLICY_FILE"]) as handle:
    policy = json.load(handle)
members = set()
for binding in policy.get("bindings", []):
    if binding.get("role") == "roles/run.invoker":
        members.update(binding.get("members", []))
# Only allUsers is anonymous. allAuthenticatedUsers still demands a Google
# identity, which an MCP client adding this server by URL does not send.
print("yes" if "allUsers" in members else "no")
PYEOF
)"

  if [ "$ALREADY_PUBLIC" = "yes" ]; then
    ok "already publicly invokable — left exactly as it is"
    ok "  revoking is a deliberate act too:"
    echo "    gcloud run services remove-iam-policy-binding $SERVICE \\"
    echo "      --region=$REGION --project=$PROJECT --member=allUsers --role=roles/run.invoker"
  elif [ "$PUBLIC_CUTOVER" != "$SERVICE" ]; then
    ok "private (no allUsers invoker) — and this script will not change that"
    ok "  to make it public:"
    echo "    PUBLIC_CUTOVER=$SERVICE ./infra/gcp/bootstrap.sh"
  else
    ok "cutover requested — checking the deployed revision serves searches"
    if ! bash "$(dirname "$0")/smoke.sh" --url "$URL" --identity-token; then
      ok "!! refusing the cutover: the deployed revision failed its smoke test"
      exit 1
    fi
    gcloud run services add-iam-policy-binding "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --member=allUsers --role=roles/run.invoker --quiet >/dev/null
    ok "granted allUsers roles/run.invoker"
  fi
fi

# ---------------------------------------------------------------------------
say "Custom domain"
# Cloud Run's built-in domain mapping: a CNAME straight at the service, no load
# balancer and no cost. It is deliberately the smaller of the two options — a
# global external Application Load Balancer (reserved anycast IP, Cloud Armor,
# CDN) can replace this later by swapping the CNAME for an A record, with
# `_INGRESS=internal-and-cloud-load-balancing` on the build. Nothing about the
# service changes.
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
