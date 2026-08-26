# Cloud Run infrastructure (Terraform) — DEPRECATED, UNSUPPORTED

**Do not deploy this service from Terraform.** Nothing here is applied, nothing
in CI exercises it, and an apply fails at plan time on purpose.

**The deployment is `cloudbuild.yaml`.** Pushing to `main` builds the image,
deploys a no-traffic revision tagged `candidate`, smokes `/readyz`, asserts
`/version` reports the released commit, and only then promotes it to 100%. A bad
revision fails the build and never takes traffic. `infra/gcp/` holds the
supporting scripts: `bootstrap.sh` for the one-time project setup, `smoke.sh` to
check a live deployment, `rollback.sh` to move traffic back.

## Why this path is refused rather than fixed

This module sets **one** literal environment variable — `ENVIRONMENT` — and
mounts four secrets. It sets no `AUTH_MODE`, no `ENTITLEMENT_MODE`, no
`DAINTREE_ENTITLEMENT_URL`, no Supabase issuer/JWKS/OAuth client, no subject
gate, and none of the operational dials. `cloudbuild.yaml` is the authoritative
list of what a real deployment passes.

That gap is not merely incomplete, it is actively unsafe. The application, given
no `AUTH_MODE`, falls back to its default of `open` — under which the
`Authorization` header is never read for authentication and every request is the
same anonymous principal, funded by the server's own Together key. Until this
change the module's defaults then published exactly that: `ingress =
"INGRESS_TRAFFIC_ALL"`, an `allUsers` grant of `roles/run.invoker`, and
`environment = "production"`. A `terraform apply` with no `-var` at all produced
an anonymous, provider-funded model endpoint that announced itself as production.

Bringing it to parity would mean maintaining a second full copy of the posture
rules `cloudbuild.yaml` already enforces — a copy nothing runs, no test drives,
and no deploy would ever notice going wrong. It would drift, silently. It already
had: the prose in these files described a caller-funded credential model that the
server stopped implementing.

## What is left here, and what it is for

`main.tf`, `variables.tf` and `outputs.tf` are a **topology sketch** — Cloud Run
v2 + external Application Load Balancer + Secret Manager, with secret values
never in Terraform state. Read them for the shape. They are safe to leave on disk
because three `precondition` blocks on the Cloud Run service refuse the plan: one
unless the deprecation is explicitly acknowledged, one unless both reachability
levers (`allow_unauthenticated` and `ingress`) are private, and one unless
`environment` is something other than `production`. Every default is already the
safe value, so the refusals are what stops someone overriding them, not what
supplies them.

**Acknowledging the first gate does not make this a supported path.** It reduces
the blast radius of an apply; it changes nothing about the credential model.
Whatever it builds still mounts the server's own `TOGETHER_API_KEY` and still
runs `AUTH_MODE=open`, so every request that reaches it is anonymous and billed
to Daintree. The gates restrict who can reach the service. Nothing here can make
a request to it cost the caller anything.

There is no `terraform apply` recipe in this file any more, and no smoke-test
`curl` — a `POST /v1/daintree/respond` is billed to the server's own key, and a
document that hands an operator both a way to publish an anonymous endpoint and a
way to spend against it is the thing that had to go.

## Facts a real deployment still depends on

Kept here because they are the reasoning behind the secret list this module does
carry, and they remain true wherever the service is deployed from:

- **The server holds the upstream credential.** `TOGETHER_API_KEY` funds every
  turn and is in `Settings.missing_secrets()`, so a deployment without it answers
  `/readyz` with 503 and never serves a request. Callers supply no key of their
  own; an account is an entitlement to spend Daintree's key, never a mapping to
  one of the caller's (`docs/KEY_HANDLING.md`).
- **`auth.py` dispatches on `AUTH_MODE`**, and `open` is only the shipped
  default. `observe` verifies a credential that is present without changing any
  verdict; `enforce` requires one. An environment that sets none of them gets
  `open`, which is why a deployment tool that cannot set it has no business
  publishing a service.
- **`STATE_SALT` must be a real (non-default) value** — readiness fails otherwise
  in any hardened environment (`Settings.weak_secrets`), as does a
  `TOGETHER_BASE_URL` that is not the `https://api.together.xyz` origin, because
  the server's own key is forwarded there.
- **Do not probe `/healthz` externally.** Google's frontend reserves that path
  and answers it itself, so the request never reaches the container. `/readyz` is
  the stronger check anyway.

## The production edge (assistant.daintree.org)

Not built from here, and this module refuses `environment = "production"`. The
shape, for reference: a global external Application Load Balancer with a reserved
global IP, a serverless NEG targeting the Cloud Run service, backend service, URL
map, target HTTPS proxy and global forwarding rule; a Google-managed certificate
for the domain; a DNS `A` record to the forwarding-rule IP; Cloud Run ingress
restricted to load-balancer traffic; optionally Cloud Armor. No CDN caching on
authenticated routes.
