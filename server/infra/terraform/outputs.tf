output "service_uri" {
  description = "The Cloud Run service URI (not the public edge)."
  value       = google_cloud_run_v2_service.service.uri
}

output "runtime_service_account" {
  description = "Email of the runtime service account."
  value       = google_service_account.runtime.email
}
