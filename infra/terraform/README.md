# Cloud Run infrastructure

Two deployment modes. The defaults are **staging-first** so you can smoke-test
the real service before building the production edge.

## Prerequisites (both modes)

Create the Secret Manager secrets referenced by `var.secret_ids` (values managed
out-of-band; never in Terraform state):

```bash
for s in daintree-api-key deepseek-api-key state-signing-key-current state-salt; do
  gcloud secrets create "$s" --replication-policy=automatic 2>/dev/null || true
done
# add a version to each, e.g.:
printf '%s' "$(openssl rand -hex 32)" | gcloud secrets versions add state-signing-key-current --data-file=-
printf '%s' "$(openssl rand -hex 16)" | gcloud secrets versions add state-salt --data-file=-
```

`STATE_SALT` must be a real (non-default) value — production readiness fails
otherwise (see `Settings.weak_secrets`).

Build and push the image to Artifact Registry, then pass it as `var.image` (by
immutable digest).

## Staging (default)

`ingress = "INGRESS_TRAFFIC_ALL"` and `allow_unauthenticated = true`: the direct
`https://<service>-<hash>-<region>.run.app` URL is reachable, and the app's
bearer key is the gate.

```bash
terraform apply \
  -var project_id=PROJECT \
  -var image=REGION-docker.pkg.dev/PROJECT/REPO/daintree-assistant@sha256:...
```

Smoke test against the `service_uri` output:

```bash
URL=$(terraform output -raw service_uri)
curl -s "$URL/healthz"                                   # {"status":"ok"}
curl -s "$URL/readyz"                                    # {"status":"ready", ...}
curl -s -o /dev/null -w '%{http_code}' "$URL/v1/daintree/capabilities"   # 401 (no auth)
curl -s -H "Authorization: Bearer $DAINTREE_API_KEY" "$URL/v1/daintree/capabilities"
curl -s -H "Authorization: Bearer $DAINTREE_API_KEY" -X POST "$URL/v1/daintree/respond" \
  -H 'content-type: application/json' \
  -d '{"protocol_version":1,"session":{"id":"s","turn_id":"t"},
       "input":{"messages":[{"role":"user","content":"hello"}]}}'
# streaming: add "generation":{"stream":true} and watch for `event: meta` then `event: delta`/`event: done`.
```

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
