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

# Secret Manager secret IDs (values are managed out-of-band, never in TF state).
variable "secret_ids" {
  description = "Map of env var name -> Secret Manager secret id."
  type        = map(string)
  default = {
    DAINTREE_API_KEY          = "daintree-api-key"
    DEEPSEEK_API_KEY          = "deepseek-api-key"
    STATE_SIGNING_KEY_CURRENT = "state-signing-key-current"
  }
}
