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

# Ingress controls who can reach the service, and right now it is the ONLY thing
# that does. There is no app-level gate: `auth.py` ignores the Authorization
# header, so every request is the same anonymous principal funded by the server's
# own Together key.
#   - Staging smoke test: "INGRESS_TRAFFIC_ALL" makes the *.run.app URL directly
#     reachable BY ANYONE who learns it. Acceptable only for a URL that is not
#     circulated, and only with a spend cap set on the provider account.
#   - Production: "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" once the external
#     Application Load Balancer + managed cert for var.domain are in place.
variable "ingress" {
  description = "Cloud Run ingress setting."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

# Grant allUsers the run.invoker role to make the staging URL directly reachable.
# NOTE WHAT THAT MEANS NOW: Google IAM was never the gate here, and neither is the
# application — an anonymous request is served and billed to us. Setting this true
# publishes a Daintree-funded model endpoint. Set false once the service sits
# behind the load balancer / IAP.
variable "allow_unauthenticated" {
  description = "Grant allUsers roles/run.invoker (staging direct-URL access)."
  type        = bool
  default     = true
}

variable "environment" {
  description = "ENVIRONMENT value passed to the app (gates production secret checks)."
  type        = string
  default     = "production"
}

# Secret Manager secret IDs (values are managed out-of-band, never in TF state).
#
# THE SERVER HOLDS THE UPSTREAM CREDENTIAL. TOGETHER_API_KEY funds every turn and
# is in `Settings.missing_secrets()`, so a deployment without it answers /readyz
# with 503 and can never serve a request. It is listed here for that reason: this
# map is the complete set of secrets the service mounts, and omitting the one
# that gates readiness deploys a revision that cannot start.
#
# (This block used to say the opposite — that the deployment held no API key and
# each caller's bearer both authenticated and funded their turn. That contract is
# gone: `auth.py` ignores the Authorization header entirely. See docs/KEY_HANDLING.md.)
#
# STATE_SALT must be a real (non-default) value or production readiness fails.
variable "secret_ids" {
  description = "Map of env var name -> Secret Manager secret id."
  type        = map(string)
  default = {
    STATE_SIGNING_KEY_CURRENT = "state-signing-key-current"
    STATE_SALT                = "state-salt"
    TOGETHER_API_KEY          = "together-api-key"
  }
}
