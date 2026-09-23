# DEPRECATED: this is not how the Daintree Assistant Server is deployed, and an
# apply from here fails at plan time unless it is explicitly acknowledged.
#
# `cloudbuild.yaml` is the deployment — pushing to `main` builds, deploys a
# no-traffic candidate, smokes it and promotes it. This module cannot express the
# posture that pipeline carries: AUTH_MODE, ENTITLEMENT_MODE,
# DAINTREE_ENTITLEMENT_URL and the Supabase issuer/JWKS/OAuth client are all
# absent here, as is the private-staging subject gate — which the pipeline does
# not mount yet either, so that one is a gap on both sides rather than a thing
# this file is merely missing. It sets exactly one literal env var (ENVIRONMENT) and
# mounts four secrets, so a service built from this file comes up calling itself
# a real deployment while the application falls back to AUTH_MODE=open — an
# anonymous, Daintree-funded model endpoint. That is the defect the gate below
# exists to stop; the file is kept as a TOPOLOGY SKETCH (Cloud Run v2 + external
# Application Load Balancer + Secret Manager, with secret VALUES never in
# Terraform state), not as a second way to ship.
#
# See README.md, and infra/gcp/ for the scripts that support the real path.

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

    # Sized against the app's own budget so an upstream stall usually surfaces as the
    # server's error envelope rather than a bare platform 504. Per attempt the phases
    # are connect 10s + pool 10s + write 30s + read MAIN_TIMEOUT_SECONDS (840s), and
    # MAX_UPSTREAM_RETRIES=1 means two attempts:
    #     2 x (10 + 10 + 30 + 840) = 1780s, plus selector time and retry backoff.
    # 1780s already leaves almost no headroom under 1800s, and httpx's read timeout is
    # per-INACTIVITY-GAP rather than total wall time, so a slowly trickling upstream
    # still has no finite application-level bound — this raises the ceiling, it does not
    # guarantee the envelope. A real guarantee needs an app-level total deadline set
    # below this value. Keep the two coupled; Cloud Run's own ceiling is 3600s.
    timeout = "1800s"
  }

  # The gate. `precondition` is evaluated during PLAN, so an unacknowledged apply
  # is refused before Terraform proposes a single resource — a variable default,
  # not a runtime check the operator discovers from a 503 afterwards. There is no
  # terraform binary in CI and no gate exercises this module's safety properties,
  # which is precisely why the refusal has to live in the file an operator runs
  # rather than in prose beside it.
  lifecycle {
    precondition {
      condition     = var.acknowledge_unsupported_terraform_path
      error_message = <<-EOT
        This Terraform path is DEPRECATED and unsupported. Deploy via cloudbuild.yaml
        (push to main); infra/gcp/ holds the supporting scripts.

        This module cannot express the posture a served deployment requires: it sets
        no AUTH_MODE, no ENTITLEMENT_MODE, no DAINTREE_ENTITLEMENT_URL, no Supabase
        issuer/JWKS/OAuth client and no subject gate, so the application falls back
        to AUTH_MODE=open and serves anonymous requests funded by Daintree's own
        OpenAI key.

        If you understand that and want the private skeleton anyway, set
        acknowledge_unsupported_terraform_path = true. The remaining preconditions
        still refuse a public or production apply -- but note what they do NOT
        change: whatever you build mounts the server's own OPENAI_API_KEY and
        runs AUTH_MODE=open, so every request that reaches it is anonymous and
        billed to Daintree. The gates restrict reachability, not the bill.
      EOT
    }

    # Acknowledgement buys a PRIVATE skeleton, never a published one. Both levers
    # are checked together because either one alone is enough to expose the
    # service: allUsers on run.invoker removes the IAM gate, public ingress
    # removes the network one, and this module has no application gate to fall
    # back on.
    #
    # Ingress is an ALLOWLIST rather than a `!= "INGRESS_TRAFFIC_ALL"`. The
    # provider accepts an empty ingress string and omits the field from the create
    # request, so the API's own default — all ingress — is what the service gets:
    # a public service produced by a value a denylist reads as harmless.
    precondition {
      # Deliberately one line, long as it is: this expression IS the safety
      # property, and tests/unit/test_infra_drift.py compares it verbatim.
      condition     = !var.allow_unauthenticated && contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"], var.ingress)
      error_message = <<-EOT
        Refusing to publish a provider-funded service from the deprecated Terraform
        path. allow_unauthenticated must stay false and ingress must be
        INGRESS_TRAFFIC_INTERNAL_ONLY or INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER.

        What a publicly invokable service needs depends on what it is for, and this
        module can express none of it: AUTH_MODE, ENTITLEMENT_MODE and
        DAINTREE_ENTITLEMENT_URL are all absent here, so whatever you build answers
        anonymously. (Those two modes are staged on INDEPENDENT axes and the public
        production service deliberately runs open/off today; private staging is the
        posture that needs both enforced. Either way the choice is made in
        cloudbuild.yaml, not here.) Deploy via cloudbuild.yaml instead.
      EOT
    }

    # ENVIRONMENT is refused here rather than in a `validation` block on the
    # variable so that `terraform destroy` still works for whoever already applied
    # one: variable validation is an error on every operation, while a resource
    # precondition is downgraded to a warning during destroy planning.
    precondition {
      condition     = contains(["development", "test", "staging"], var.environment)
      error_message = <<-EOT
        This path may not build the production service — cloudbuild.yaml does, and it
        is the only place the production posture (AUTH_MODE, ENTITLEMENT_MODE,
        DAINTREE_ENTITLEMENT_URL, the Supabase client) is expressed at all.

        ENVIRONMENT must be development, test or staging. Note that "staging" is
        hardened by the application exactly as "production" is, so it costs nothing
        in strictness.
      EOT
    }
  }
}

# Kept only so the refusal above has a resource to name. It is unreachable in
# practice: `allow_unauthenticated` defaults false, and the precondition on the
# service refuses true, so this binding never has an instance. Granting allUsers
# roles/run.invoker would remove the only gate this module can place in front of
# the service — the application's own posture is AUTH_MODE, which lives in
# cloudbuild.yaml — leaving an anonymous request served and billed to us.
#
# Note what `count = 0` does NOT say: it declines to create THIS binding, and
# asserts nothing about an allUsers grant made elsewhere. infra/gcp/bootstrap.sh
# makes exactly such a grant. Terraform cannot revoke a binding it does not
# manage, so a service that was public before is still public after.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.service.name
  location = google_cloud_run_v2_service.service.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# PRODUCTION EDGE: NOT from here. assistant.daintree.org is served by the
# cloudbuild.yaml pipeline, and the `environment` variable in this module refuses
# "production" outright. The topology a production edge needs, recorded because
# the sketch is the only thing this file is still good for: a global external
# Application Load Balancer — global IP, a serverless NEG targeting this service,
# backend service, URL map, HTTPS proxy, a Google-managed certificate for
# var.domain, a forwarding rule, a DNS A record, and optionally Cloud Armor. Do
# NOT enable CDN caching on authenticated routes. See infra/terraform/README.md.
