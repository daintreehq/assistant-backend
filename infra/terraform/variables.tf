# DEPRECATED PATH. See README.md and the header of main.tf: nothing deploys this
# service from Terraform, and an apply fails at plan time unless
# `acknowledge_unsupported_terraform_path` is set. The defaults below are
# nonetheless the SAFE ones, because a gate somebody can switch off must not have
# an unsafe thing waiting behind it.

# The deprecation gate. It defaults to false so that `terraform plan` on this
# module fails before it describes a single resource.
#
# The reason is not tidiness. `cloudbuild.yaml` is the deployment (CLAUDE.md:
# "Pushing to `main` is the deploy"), and it enforces a posture this module has
# no way to express: AUTH_MODE, ENTITLEMENT_MODE, DAINTREE_ENTITLEMENT_URL, the
# Supabase issuer/JWKS/OAuth client, and the subject gate. This module sets
# exactly one literal env var — ENVIRONMENT — and mounts four secrets. That
# combination is what makes it dangerous rather than merely incomplete: a service
# built from here comes up believing it is a real deployment while the
# application falls back to AUTH_MODE=open, which is an anonymous, Daintree-funded
# model endpoint. Bringing this to parity would mean a second copy of the posture
# rules in cloudbuild.yaml that nothing runs and no CI gate exercises — a copy
# that drifts silently, which is how the stale credential prose in this very file
# got here.
#
# Setting it true buys a PRIVATE, non-production skeleton and nothing else; the
# preconditions in main.tf still refuse a public or production apply.
variable "acknowledge_unsupported_terraform_path" {
  description = "Acknowledge that this Terraform path is deprecated and unsupported."
  type        = bool
  nullable    = false
  default     = false
}

variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "Cloud Run region (selected from measured end-to-end latency)."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "daintree-assistant"
}

variable "image" {
  description = "Fully-qualified container image (Artifact Registry, by immutable SHA)."
  type        = string
}

variable "domain" {
  description = "Custom domain served by the external load balancer."
  type        = string
  default     = "assistant.daintree.org"
}

variable "min_instances" {
  description = "Minimum Cloud Run instances (avoid scale-from-zero latency)."
  type        = number
  default     = 1
}

variable "max_instances" {
  description = "Maximum Cloud Run instances (cost ceiling)."
  type        = number
  default     = 10
}

variable "concurrency" {
  description = "Per-instance request concurrency (conservative for long streams)."
  type        = number
  default     = 8
}

# Ingress controls who can reach the service, and from THIS module it is one of
# only two things that do — the other being the IAM binding below. The
# application's own posture is `AUTH_MODE`, which this module cannot set at all:
# `auth.py` dispatches on that mode, and the shipped default is `open`, under
# which the Authorization header is never read for authentication and every
# request is the same anonymous principal funded by the server's own Baseten
# key. So a service built from here is exactly as private as its ingress, and no
# more.
#
# The default is therefore the internal one, and main.tf ALLOWLISTS the accepted
# values rather than refusing "INGRESS_TRAFFIC_ALL" by name. A denylist looked
# equivalent and was not: the provider accepts an EMPTY string here and simply
# omits the field from the create request, so the API applies its own default —
# which is all ingress. `""` would have walked straight past a `!=` check.
#   - "INGRESS_TRAFFIC_INTERNAL_ONLY": the default, and the only value that is
#     private on its own terms.
#   - "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER": also accepted, but it is private
#     only for as long as nobody has built the external Application Load Balancer
#     in front of it — and this module does not build one.
#   - Anything else, "INGRESS_TRAFFIC_ALL" and "" included, is refused by the
#     precondition in main.tf even with the deprecation acknowledged, because this
#     module cannot express the enforce/enforce/gated posture a publicly reachable
#     service requires.
# `nullable = false` closes the same hole from the other side: an explicit null
# becomes the default here rather than reaching the provider as an omitted field.
variable "ingress" {
  description = "Cloud Run ingress setting (only the private values are accepted; see main.tf)."
  type        = string
  nullable    = false
  default     = "INGRESS_TRAFFIC_INTERNAL_ONLY"
}

# Granting allUsers roles/run.invoker publishes a Daintree-funded model endpoint.
# Google IAM is the only gate this module can actually place in front of the
# service — the application's is AUTH_MODE, which lives in cloudbuild.yaml — so
# removing the IAM one removes all of them, and an anonymous request is served
# and billed to us.
#
# This defaults false and main.tf refuses true outright. It is kept as a variable
# rather than deleted so the refusal has something to name: an operator who sets
# it gets the reason, where a silently absent variable would just be an unknown
# argument.
variable "allow_unauthenticated" {
  description = "Grant allUsers roles/run.invoker. Refused by main.tf; kept so the refusal can name it."
  type        = bool
  nullable    = false
  default     = false
}

# ENVIRONMENT gates the app's own hardening (`Settings.is_hardened` covers
# production AND staging: weak or default secrets rejected, the serving origin
# pinned, local-only features refused). "staging" is therefore the safe default
# here — it keeps every one of those checks on while making no claim to be the
# production service, which this module is not allowed to build.
#
# It used to default to "production", which is how a Terraform-built service came
# up announcing itself as production while the application, given no AUTH_MODE,
# fell back to `open`.
# The refusal of "production" is a PRECONDITION in main.tf rather than a
# `validation` block here, and the difference is destroy. Input-variable
# validation is an error on every operation including `terraform destroy`, so a
# tfvars file or TF_VAR_environment left at "production" would leave an operator
# unable to tear down what they had already applied — a gate that traps someone
# on the way out is a worse gate. Resource preconditions are downgraded to
# warnings during destroy planning, so the refusal binds exactly where it should:
# on creating or updating a service.
variable "environment" {
  description = "ENVIRONMENT value passed to the app (gates the app's hardened checks)."
  type        = string
  nullable    = false
  default     = "staging"
}

# Secret Manager secret IDs (values are managed out-of-band, never in TF state).
#
# THE SERVER HOLDS THE UPSTREAM CREDENTIAL. BASETEN_API_KEY funds every turn and
# is in `Settings.missing_secrets()`, so a deployment without it answers /readyz
# with 503 and can never serve a request. It is listed here for that reason: this
# map is the complete set of secrets the service mounts, and omitting the one
# that gates readiness deploys a revision that cannot start.
#
# It is NOT the complete set of ENV a real deployment needs — that is
# cloudbuild.yaml's list, and the gap is the whole reason this path is
# deprecated. Nothing here supplies AUTH_MODE, ENTITLEMENT_MODE,
# DAINTREE_ENTITLEMENT_URL, the Supabase issuer/JWKS/OAuth client, or the
# operational dials.
#
# (This block used to say that the deployment held no API key and that each
# caller's bearer both authenticated and funded their turn. That contract is
# gone: an account is an entitlement to spend Daintree's key, never a mapping to
# one of the caller's. See docs/KEY_HANDLING.md.)
#
# STATE_SALT must be a real (non-default) value or readiness fails in any
# hardened environment.
variable "secret_ids" {
  description = "Map of env var name -> Secret Manager secret id."
  type        = map(string)
  default = {
    STATE_SIGNING_KEY_CURRENT = "state-signing-key-current"
    STATE_SALT                = "state-salt"
    BASETEN_API_KEY          = "baseten-api-key"
    # Required since the fast-response stage lost its off switch: it runs on
    # every eligible turn, so readiness refuses a deployment without it.
    CEREBRAS_API_KEY          = "cerebras-api-key"
  }
}
