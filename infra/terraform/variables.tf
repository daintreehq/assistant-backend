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

# Ingress controls who can reach the service.
#   - Staging smoke test: "INGRESS_TRAFFIC_ALL" makes the *.run.app URL directly
#     reachable; app-level bearer auth is the gate.
#   - Production: "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" once the external
#     Application Load Balancer + managed cert for var.domain are in place.
variable "ingress" {
  description = "Cloud Run ingress setting."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
}

# For a staging URL reachable with just the app bearer key, grant allUsers the
# run.invoker role (the app's auth, not Google IAM, is the gate). Set false once
# the service sits behind the load balancer / IAP.
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
# STATE_SALT must be a real (non-default) value or production readiness fails.
variable "secret_ids" {
  description = "Map of env var name -> Secret Manager secret id."
  type        = map(string)
  default = {
    DAINTREE_API_KEY          = "daintree-api-key"
    FIREWORKS_API_KEY         = "fireworks-api-key"
    STATE_SIGNING_KEY_CURRENT = "state-signing-key-current"
    STATE_SALT                = "state-salt"
  }
}
