# Outputs of the DEPRECATED skeleton (see README.md). `service_uri` names a
# private, non-production service if this module is ever applied at all; it is
# not the deployed service, which cloudbuild.yaml builds.

output "service_uri" {
  description = "The Cloud Run service URI (not the public edge)."
  value       = google_cloud_run_v2_service.service.uri
}

output "runtime_service_account" {
  description = "Email of the runtime service account."
  value       = google_service_account.runtime.email
}
