#!/usr/bin/env bash
#
# One-time (and safely repeatable) GCP setup for the Daintree Assistant Server's
# continuous deployment pipeline. Creates everything cloudbuild.yaml assumes:
# the Artifact Registry repository, the two Secret Manager secrets, a runtime
# service account and a build service account with least-privilege roles, and
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
# THE SERVICE IT CREATES IS PRIVATE, and re-running never changes that. This
# script used to grant `allUsers roles/run.invoker` on every run — which, since
# the server holds the upstream credential and funds every turn, published an
# open Daintree-funded model endpoint as a side effect of finishing a setup.
# Public access is now a cutover, performed once and on purpose, against a
# revision that has been seen to enforce; see the "Public access" section at the
# bottom of this file and infra/gcp/README.md.
#
# Secret VALUES are generated here and never leave Secret Manager. Re-running
# will NOT rotate an existing secret: rotating the state signing key would
# invalidate every outstanding state token, so that is a deliberate manual act.
# One secret is never given a value here at all — the private-staging subject
# gate's membership list, which this script creates empty and never reads.
#
# What the server needs at runtime is state-signing material PLUS the upstream
# credential (TOGETHER_API_KEY), which it holds itself and funds every turn from.
# Callers supply no key of their own (see README.md and docs/KEY_HANDLING.md).

set -euo pipefail

PROJECT="${PROJECT:-daintree-research}"
REGION="${REGION:-us-central1}"              # Cloud Run + Artifact Registry
# The Cloud Run service. `cloudbuild.yaml` holds a CLOSED service -> environment
# table and refuses a name it does not know, so this is not a free-form value:
# `daintree-assistant` is the production service and `daintree-assistant-staging`
# is the staging one. A genuinely new service is a row added to that table, in a
# commit, before it can receive a release — see infra/gcp/README.md.
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
#
# The two upstream credentials are DIFFERENT IN KIND from the two below: they
# cannot be generated, only supplied. So this script creates the secret shell and stops —
# a random value here would deploy a service that fails /readyz on a credential
# nobody realised was fake.
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

# The upstream credential gets a shell and it GATES: together-api-key funds every
# turn, the deploy mounts it unconditionally, and a missing version has to stop
# the deploy here rather than surface as a red /readyz two steps later.
ensure_supplied_secret() {
  local name="$1" example="$2" gates="$3"
  if ! gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    gcloud secrets create "$name" --replication-policy=automatic \
      --project="$PROJECT" >/dev/null
    ok "created secret $name"
  fi
  if has_version "$name"; then
    ok "$name already has a version (left alone — rotation is manual)"
  elif [ "$gates" = "gates" ]; then
    ok "!! $name has NO version. The deploy will fail until you add one:"
    echo "    printf '%s' '$example' | gcloud secrets versions add $name \\"
    echo "      --data-file=- --project=$PROJECT"
  else
    ok "$name has no version (not gating)"
  fi
}
ensure_supplied_secret together-api-key 'tgp_v1_...' gates

# The FAST-RESPONSE credential, and it GATES now. The stage lost its off switch:
# it runs on every eligible turn of every deployment, the deploy mounts this
# secret unconditionally, and readiness (`Settings.missing_secrets`) refuses a
# revision without it. That refusal is the point — a missing key would otherwise
# fail the stage open on every single turn, which is indistinguishable from a
# model that simply chose not to say anything.
ensure_supplied_secret cerebras-api-key 'csk-...' gates

# The private-staging subject gate's membership list — A SHELL ONLY, and this
# script must never put a version in it.
#
# It is different in kind from every secret above. The two generated ones are
# random bytes nobody needs to know; the two supplied ones are vendor credentials
# an operator pastes. This one is a list of SHA-256 digests of the Supabase
# subject ids of the people admitted to the private beta — so a generator here
# could only invent members, and a helper that took a subject and hashed it would
# put a real user id into a shell history and a terminal scrollback on the way to
# the only place it is supposed to exist. Publishing the list is an operator act
# performed from the runbook, and this script's whole contribution is the
# container and the grant.
#
# Nothing here reads it back either. `gcloud secrets versions access` returns the
# payload and appears nowhere in this repo; `has_version` asks only whether one
# exists.
if ! gcloud secrets describe staging-allowed-subject-digests \
     --project="$PROJECT" >/dev/null 2>&1; then
  gcloud secrets create staging-allowed-subject-digests \
    --replication-policy=automatic --project="$PROJECT" >/dev/null
  ok "created secret staging-allowed-subject-digests (empty)"
fi
if has_version staging-allowed-subject-digests; then
  ok "staging-allowed-subject-digests already has a version (left alone)"
else
  ok "staging-allowed-subject-digests has no version (not gating unless you deploy"
  ok "  ENVIRONMENT=staging with AUTH_MODE=enforce — see infra/gcp/README.md)"
fi

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
# together-api-key appears here because the deploy mounts it: without the grant,
# the runtime identity cannot read the key that funds every turn, and the
# revision fails at startup.
# cerebras-api-key is granted whether or not it has a version yet: the binding is
# on the secret, so granting it now means enabling the fast-response stage later
# needs no IAM change — and a missing grant would present as a revision that
# starts, passes readiness with the stage off, and then fails the moment somebody
# turns it on.
# staging-allowed-subject-digests is granted whether or not it has a version, for
# the same reason cerebras-api-key is: the binding is on the SECRET, so publishing
# a membership list later needs no IAM change — and a missing grant would present
# as a private staging revision that fails at startup on a mount it cannot read,
# which reads as a broken image rather than a missing permission.
RUNTIME_SECRETS="state-signing-key-current state-salt together-api-key cerebras-api-key staging-allowed-subject-digests"
for s in $RUNTIME_SECRETS; do
  retry 12 gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role=roles/secretmanager.secretAccessor \
    --project="$PROJECT" --quiet >/dev/null
done
ok "granted secretAccessor on $RUNTIME_SECRETS"

# The Developer Connect connection's GitHub OAuth token. The fetch-lfs build
# step reads it to pull LFS content (the model artifact) from the PRIVATE repo
# — the v2 checkout leaves no git credential behind. The secret's name carries
# a random suffix minted by Developer Connect, so it is discovered, not
# hardcoded; skipped with a warning if the connection is not set up yet.
GH_TOKEN_SECRET=$(gcloud secrets list --project="$PROJECT"   --filter="name~daintree-github-github-oauthtoken"   --format="value(name)" --limit=1 || true)
if [ -n "$GH_TOKEN_SECRET" ]; then
  retry 12 gcloud secrets add-iam-policy-binding "$GH_TOKEN_SECRET"     --member="serviceAccount:${BUILD_SA_EMAIL}"     --role=roles/secretmanager.secretAccessor     --project="$PROJECT" --quiet >/dev/null
  ok "granted deploy SA secretAccessor on $GH_TOKEN_SECRET (LFS fetch)"
else
  ok "!! no daintree-github oauthtoken secret found — LFS fetch will fail until granted"
fi

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
# THIS SCRIPT NO LONGER GRANTS PUBLIC ACCESS, and the removal is the point of the
# section rather than a tidy-up inside it.
#
# `allUsers roles/run.invoker` is what makes the service reachable by anybody at
# all, and it used to be granted here on every run — ungated, unprompted, on a
# script whose header tells you that re-running it is how you finish the setup.
# The binding is per-SERVICE, so it also publishes every no-traffic release
# candidate's `rc-<build>` tag URL. And the app cannot compensate: the server
# holds the upstream credential and funds every turn from it, so an unenforced
# public service is an open, Daintree-funded model endpoint that a re-run of a
# setup script created.
#
# Attaching public access is now a CUTOVER — a deliberate, separate operation
# performed once the deployed revision has been seen to enforce identity, enforce
# entitlement, and hold a non-empty subject gate. It is done by re-running this
# script with the service NAMED:
#
#     PUBLIC_CUTOVER=daintree-assistant ./infra/gcp/bootstrap.sh
#
# Naming the service is the INTENT half of the confirmation, and it is worth
# being honest about how much that is worth: an exported `PUBLIC_CUTOVER` left
# over in a shell would satisfy it if it happened to hold this exact name. That
# is why it is only half. The other half is below and does the real work — the
# deployed revision's posture is READ BACK before the binding is created, so a
# cutover aimed at yesterday's revision, or at staging while thinking of
# production, is refused by what is actually deployed rather than by what was
# typed.
#
# What this section will NOT do in either direction: it never removes an existing
# public binding. Production carries one today, it predates this rule, and taking
# it away is an outage — so revoking is as deliberate an act as granting, and it
# is a command an operator runs rather than a side effect of a setup script.
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
  VERSION_FILE="$(mktemp)"
  trap 'rm -f "$POLICY_FILE" "$VERSION_FILE"' EXIT
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
# allAuthenticatedUsers is not "our users" — it is every Google account there is,
# which for a service that funds its own model calls is the same exposure with a
# sign-in page in front of it.
print("yes" if members & {"allUsers", "allAuthenticatedUsers"} else "no")
PYEOF
)"

  if [ "$ALREADY_PUBLIC" = "yes" ]; then
    ok "already publicly invokable — left exactly as it is"
    ok "  revoking is a deliberate act too:"
    echo "    gcloud run services remove-iam-policy-binding $SERVICE \\"
    echo "      --region=$REGION --project=$PROJECT --member=allUsers --role=roles/run.invoker"
  elif [ "$PUBLIC_CUTOVER" != "$SERVICE" ]; then
    ok "private (no allUsers invoker) — and this script will not change that"
    ok "  to attach public access, prove the posture first and then:"
    echo "    PUBLIC_CUTOVER=$SERVICE ./infra/gcp/bootstrap.sh"
    ok "  see infra/gcp/README.md for what has to be true before you do"
  else
    # THE POSTURE IS PROVED, NOT ASSERTED BY THE OPERATOR. Typing the service
    # name says "I mean to do this"; it does not say the revision in front of us
    # is the one that was supposed to receive it, and the two are exactly the
    # thing that comes apart during a rollout — a cutover run against yesterday's
    # revision, or against staging while thinking of production.
    #
    # /version is read with an IDENTITY TOKEN because the service is still
    # private at this moment, which is the whole reason the check is possible: it
    # can be answered before anybody outside can ask anything.
    ok "cutover requested — checking what the deployed revision actually enforces"
    curl -sS --max-time 30 \
      -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
      "$URL/version" > "$VERSION_FILE" || true
    POSTURE_OK="$(VERSION_FILE="$VERSION_FILE" python3 - <<'PYEOF'
import json, os

try:
    with open(os.environ["VERSION_FILE"]) as handle:
        body = json.load(handle)
except Exception:
    print("unreadable")
    raise SystemExit(0)

posture = body.get("posture") or {}
gate = posture.get("subject_gate") or {}
problems = []
if posture.get("auth") != "enforce":
    problems.append("AUTH_MODE is %r, not 'enforce'" % posture.get("auth"))
if posture.get("entitlement") != "enforce":
    problems.append("ENTITLEMENT_MODE is %r, not 'enforce'" % posture.get("entitlement"))
if gate.get("enabled") is not True:
    problems.append("the subject gate is not enabled")
elif not isinstance(gate.get("entries"), int) or gate.get("entries") < 1:
    problems.append("the subject gate admits nobody")
# The LABEL is printed and the membership never is. A count and a name are what
# an operator needs to recognise the list they published; neither narrows who is
# on it.
print("ok" if not problems else "; ".join(problems))
PYEOF
)"
    if [ "$POSTURE_OK" != "ok" ]; then
      ok "!! refusing the cutover: $POSTURE_OK"
      ok "   A public service in this posture funds anonymous or ungated work."
      ok "   Release the enforced, gated revision first, then re-run the cutover."
      exit 1
    fi
    gcloud run services add-iam-policy-binding "$SERVICE" \
      --region="$REGION" --project="$PROJECT" \
      --member=allUsers --role=roles/run.invoker --quiet >/dev/null
    ok "granted allUsers roles/run.invoker after proving enforce/enforce/gated"
  fi
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
