# Skeleton Terraform for the Daintree Assistant Server on Cloud Run.
#
# This is an illustrative skeleton, not an applied configuration. It shows the
# recommended topology (Cloud Run v2 + external Application Load Balancer +
# Secret Manager). Secret VALUES are never stored in Terraform state — only
# references to Secret Manager versions.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Runtime service account with least privilege (secret access only).
resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-run"
  display_name = "Daintree Assistant Cloud Run runtime"
}

resource "google_secret_manager_secret_iam_member" "secret_access" {
  for_each  = var.secret_ids
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "service" {
  name     = var.service_name
  location = var.region
  ingress  = var.ingress

  template {
    service_account = google_service_account.runtime.email
    max_instance_request_concurrency = var.concurrency

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        startup_cpu_boost = true
      }

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }

      # Secret-backed environment variables.
      dynamic "env" {
        for_each = var.secret_ids
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/readyz"
        }
        initial_delay_seconds = 5
        timeout_seconds       = 5
        period_seconds        = 5
        failure_threshold     = 12
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }
    }

    timeout = "900s"
  }
}

# Staging: make the direct *.run.app URL reachable with just the app bearer key.
# Set allow_unauthenticated = false once the service sits behind the load
# balancer / IAP and Google IAM gates invocation.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.service.name
  location = google_cloud_run_v2_service.service.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# PRODUCTION EDGE (deferred): for assistant.daintree.org, set
# ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER", allow_unauthenticated =
# false, and add a global external Application Load Balancer — global IP, a
# serverless NEG targeting this service, backend service, URL map, HTTPS proxy,
# a Google-managed certificate for var.domain, a forwarding rule, a DNS A
# record, and optionally Cloud Armor. Do NOT enable CDN caching on authenticated
# routes. See infra/terraform/README.md.
