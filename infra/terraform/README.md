# Cloud Run infrastructure

Two deployment modes. The defaults are **staging-first** so you can smoke-test
the real service before building the production edge.

## Prerequisites (both modes)

Create the Secret Manager secrets referenced by `var.secret_ids` (values managed
out-of-band; never in Terraform state):

```bash
for s in state-signing-key-current state-salt together-api-key; do
  gcloud secrets create "$s" --replication-policy=automatic 2>/dev/null || true
done
# state-signing material can be GENERATED:
printf '%s' "$(openssl rand -hex 32)" | gcloud secrets versions add state-signing-key-current --data-file=-
printf '%s' "$(openssl rand -hex 16)" | gcloud secrets versions add state-salt --data-file=-
# the upstream credential cannot be — supply the real Together key:
printf '%s' 'tgp_v1_...' | gcloud secrets versions add together-api-key --data-file=-
```

**The server holds the upstream credential.** `TOGETHER_API_KEY` funds every turn
and is in `Settings.missing_secrets()`, so a deployment without it answers
`/readyz` with 503 and never serves a request. Callers supply no key of their own
and `auth.py` ignores the `Authorization` header entirely — **the door is open**
(see `docs/KEY_HANDLING.md`); an inbound bearer is not what protects this service.

`STATE_SALT` must be a real (non-default) value — production readiness fails
otherwise (see `Settings.weak_secrets`), as does a `TOGETHER_BASE_URL` that is not
the `https://api.together.xyz` origin (the server's key is forwarded to it).

Build and push the image to Artifact Registry, then pass it as `var.image` (by
immutable digest).

## Staging (default)

`ingress = "INGRESS_TRAFFIC_ALL"` and `allow_unauthenticated = true`: the direct
`https://<service>-<hash>-<region>.run.app` URL is reachable.

**There is no gate.** `auth.py` ignores the `Authorization` header, so every
request becomes the same anonymous principal and is funded by the server's own
Together key. That combination is acceptable only for a URL that is not handed
around: there is no per-caller rate limit or spend control in the application, so
the only thing bounding spend is Cloud Run's max-instance count and whatever
budget alerts the provider account carries. Set those, and prefer restricted
ingress or IAM the moment this URL leaves a small internal group.

```bash
terraform apply \
  -var project_id=PROJECT \
  -var image=REGION-docker.pkg.dev/PROJECT/REPO/daintree-assistant@sha256:...
```

Smoke test against the `service_uri` output. **No bearer is needed** — none is
read — and the turn is billed to the SERVER's Together key, so the last command
below costs real money:

```bash
URL=$(terraform output -raw service_uri)
# Not /healthz: Google's frontend reserves that path and answers it itself, so
# the request never reaches the container. /readyz is the stronger check anyway.
curl -s "$URL/readyz"                                    # {"status":"ready", ...}
curl -s -o /dev/null -w '%{http_code}' "$URL/v1/daintree/capabilities"   # 200 — the door is open
curl -s -X POST "$URL/v1/daintree/respond" \
  -H 'content-type: application/json' \
  -d '{"protocol_version":2,"session":{"id":"s","turn_id":"t"},"startup":{},
       "input":{"messages":[{"role":"user","content":"hello"}]}}'
# streaming: add "generation":{"stream":true} and watch for `event: meta` then `event: delta`/`event: done`.
```

`infra/gcp/smoke.sh` runs all of the above against a live deployment.

This is **not** the production-domain deployment — call it staging.

## Production (assistant.daintree.org) — deferred edge

Set `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"` and
`allow_unauthenticated = false`, then add the external HTTPS edge (not yet in
this config):

1. Global external Application Load Balancer (reserved global IP).
2. Serverless NEG targeting the Cloud Run service + backend service + URL map +
   target HTTPS proxy + global forwarding rule.
3. Google-managed certificate for `var.domain`.
4. DNS `A` record → the forwarding-rule IP.
5. **No** CDN caching on authenticated routes.
6. Optional Cloud Armor (rate limiting / IP policy).
7. Keep Cloud Run ingress restricted to load-balancer traffic.

Smoke-test the same endpoints through `https://assistant.daintree.org` once DNS
and the certificate are active.
