# Daintree Assistant API v1

The Daintree Assistant Server exposes a **Daintree-native** HTTP API — it is *not*
OpenAI-compatible. There is exactly one client (the Daintree Assistant CLI), which
the server fully controls, so the wire contract is purpose-built rather than
shaped around a third-party schema.

The upstream provider is Together AI — its OpenAI-compatible wire shape is spoken
*internally*, behind a provider abstraction; the main model is
`deepseek-ai/DeepSeek-V4-Flash-0731`, named once in `config.EXECUTOR_MODEL`. Together
is a model HOST, not a router: one endpoint serves each model, so there is no
endpoint-selection policy, no fallback pool and no caller-facing routing preference.
All of that is invisible to `/v1/daintree/respond`, which never reports a real model
id (only the public `daintree-assistant` family). `/v1/daintree/tasks` deliberately
reports the real upstream model a task profile is configured to use — every profile
today runs on Together's `EXECUTOR_MODEL` — and the operator-facing `/v1/daintree/doctor`
reports it too.

## Principles

- **Runbooks are the core.** Runbook selection runs LOCALLY — an in-process two-stage ONNX
  classifier, no upstream call — and runbook bodies are injected server-side; the result is
  surfaced as a first-class `runbooks` block (active set, the newly-loaded prelude, and
  selector telemetry). A small model with the right runbook loaded is the source of the
  system's intelligence.
- **The server owns all system text.** Clients cannot send `system`/`developer`
  messages or a free-form system prompt; the request schema is strict (`extra="forbid"`).
  Typed startup/project-instruction fields are bounded, server-framed untrusted data.
- **Cache-optimized prompt layout.** The two static system layers and the tool schemas
  lead, followed by the injected data rows ordered widest-shared first (runbook bodies,
  then integrations and a required session-stable startup block), the append-only
  visible conversation, and one small volatile runtime/display/turn tail. See
  [Prompt assembly](#prompt-assembly--caching).
- **Honest streaming.** Metadata is always the first public SSE event. The stream
  never fabricates a successful finish after an upstream failure.

## Authentication

**The credential model is inverted from what an early tester will remember.** The
SERVER holds the upstream credential (`TOGETHER_API_KEY`) and funds every turn from it.
Callers supply no key of their own.

There is no inbound API key to configure. Account authentication is OAuth, and under
the shipped posture the `Authorization` header is **never read for authentication** — not
parsed, not verified, not rejected. That is stronger than "a credential is optional", and the
difference is the point: a client still sending the key it used to authenticate with
would otherwise have that key threaded upstream and refused as *theirs*, so the caller
would see their own credential blamed for a header they were right to still be sending.
Refusing to look is what makes an old client work.

An account is an **entitlement to spend Daintree's key**, never a mapping to one of
yours. A verified caller changes who the turn is attributed to; it does not change which
credential funds it. So the `provider_invalid_api_key` /
`provider_insufficient_credits` / `provider_key_forbidden` family describes the SERVER's
credential in every posture: they mean the deployment is misconfigured or unfunded, not
that the caller did anything wrong. See [Errors](#errors).

`/healthz`, `/readyz`, and `/version` are unauthenticated (Cloud Run probes them; they
carry no user data), and so is `GET /v1/daintree/auth/config` — a client has to be able
to read how to sign in before it holds anything to sign in with.

The posture is named by one server setting, `AUTH_MODE`, so a client never has to infer
it:

- **`open`** — the default, and what every deployment runs today. The header is not read
  for authentication. (It is still read for redaction, in every mode: an inbound
  `X-Request-Id` that overlaps your credential is discarded rather than logged back to you.)
- **`observe`** — a credential that IS present is verified and the outcome recorded, and
  **nothing a caller can observe changes**. Same status, same body, same permissions as
  `open`, for a good token and a bad one alike, and a failure inside the verifier is
  swallowed rather than surfaced. It exists to produce evidence against real traffic
  before anybody's request depends on the answer. It is not free: verifying costs time,
  and a cold key cache costs a fetch.
- **`enforce`** — a verified credential is required, and failures are the
  [account error family](#your-daintree-account).

`/v1/daintree/auth/config` reports what a client needs from that as `required`, alongside
`configured` — whether signing in is possible here at all. The two are separate on
purpose: during a rollout a deployment publishes a complete manifest while still serving
anonymous requests.

**The posture is not the environment.** `ENVIRONMENT` (`development`, `test`, `staging`,
`production`) says where a deployment is and which hardening rules apply to the URLs it
publishes; `AUTH_MODE` says what it does about credentials. They are independent axes and
there is deliberately no fourth `staging` auth mode — a staging deployment sits in
whichever posture its rollout has reached, and the posture moves by a config-only
revision rather than a redeploy. The staged rollout, with no secret in it:

```text
assistant.daintree.org:        ENVIRONMENT=staging
browser account/payment links: staging.daintree.org
initial posture:               open, or observe
later posture:                 enforce, after an end-to-end sign-in is validated
```

## Account entitlement

Identity says the token is genuine. **Entitlement says the account behind it has
paid**, and it is staged on its own axis so a billing outage can never present as
a signed-out session.

`ENTITLEMENT_MODE` is `off` (the shipped default), `observe` or `enforce`, and the
two non-default modes both require `AUTH_MODE=enforce` — under any other mode
every request resolves to the anonymous principal, which has no account to ask
about. A configuration that does not satisfy that fails readiness rather than
quietly reverting to `off`.

- **`off`** — paid routes never ask. Nothing is looked up and a turn's first byte
  pays nothing for the question.
- **`observe`** — paid routes resolve the verdict, record it, and **never refuse on
  it**. The mirror of `AUTH_MODE=observe`, and with the same precise promise: the
  status, body and headers a caller receives are exactly what `off` would have
  returned, including on every failure path. It moves no verdict. It does move
  LATENCY — an uncached lookup is a bounded 5s network call on the turn's
  first-byte path — so this is a mode to watch timings under, not one that is
  free.
- **`enforce`** — `access: granted` is required.

Only `POST /v1/daintree/respond` and `POST /v1/daintree/tasks` are gated: they are
the two routes that can issue **billable inference**. (`/v1/daintree/auth/verify`
and the doctor route do reach the provider, but only through its tokenless key
probe, which spends nothing.) Health, version, auth discovery, account
status, capabilities and the runbook catalog are not, and `POST
/v1/daintree/runbooks/resolve` is not either — runbook selection is a local
classifier, so that route spends nothing and there is nothing for a gate to
protect.

The verdict is reached **before** the upstream call is opened and before any byte
is streamed, so a refusal is an ordinary HTTP status rather than a stream that
opens and then apologises. Under `enforce`:

| verdict | response |
|---|---|
| granted | the turn proceeds |
| no plan | `402 subscription_required` — open the `subscribe_url` from `/v1/daintree/auth/config` |
| plan not currently granting | `402 subscription_inactive` — open the `account_url`; **not** an invitation to buy a second plan |
| the billing authority refused the token | `401 auth_session_revoked` |
| the billing authority refused the OAuth client | `403 auth_client_not_allowed` or `auth_permission_denied` |
| too many requests | `429 account_rate_limited` |
| we could not decide | `503 entitlement_unavailable`, retryable |

**Uncertainty is never a verdict.** An outage, a timeout, a malformed answer and a
contradictory one all resolve to `entitlement_unavailable`, and none of them is
ever reported as "you have not paid". A paying customer told to buy a second plan
during a billing outage is the failure this whole taxonomy exists to prevent.

Verdicts are cached per instance for at most 60 seconds once granted and 15 once
refused, and nothing uncertain is cached at all — so a paid request costs at most
one lookup and often none. Any refusal purges the entry, so a revoked grant stops
being honoured at the first refusal rather than at expiry; and because every
cached entry is at most that old, the bound holds however many instances are warm.

A deployment in a binding mode that cannot build a client for the configured URL
**fails readiness** rather than taking traffic and refusing every turn.

The Supabase issuer, JWKS, authorize and token URLs and the public OAuth client id come
from that environment's own Supabase project. They are published to every caller by this
endpoint, so they are ordinary deployment configuration rather than secrets — but they
are per-deployment, and a value written into source is one that follows every copy of
it.

Under `enforce` a token is verified locally against the configured Supabase project's
JWKS: an asymmetric signature (RS256 or ES256, never chosen by the token), the exact
issuer, the `authenticated` audience, expiry within 60 seconds of clock skew, and an
OAuth `client_id` on this deployment's allowlist. **A verified token establishes identity
only.** It does not establish that the session still exists — nothing reaches back into a
minted JWT, so a logout leaves the signature valid until it expires — and it does not
establish entitlement, which is a separate question with its own staged mode (see
[Account entitlement](#account-entitlement) above).

### GET /v1/daintree/auth/config

Public, unauthenticated OAuth discovery: how to obtain a credential for **this**
deployment. It exists so a client never hardcodes a Supabase project, OAuth client id,
account URL or callback that can then drift from the backend.

It is unauthenticated by necessity, not by omission — requiring a token to learn how to
get a token is a bootstrap no client can satisfy. It is also **not gated on readiness**:
it is built from configuration alone, and the moment someone most needs to know how to
sign in is the moment the rest of the service is unhappy.

These fields are always present, so a client reads them unconditionally and only then
decides whether the endpoints exist:

```json
{ "version": 1, "environment": "production", "configured": false, "required": false }
```

`version` is the manifest's shape, and a client checks it before anything else. It is
present even in the unconfigured shape, because "a manifest I am too old to understand"
and "a deployment with no accounts" are different answers with different advice.

This is one of the few real versions in this service — wire metadata negotiated with a
client, in the same category as `X-Daintree-Protocol`, and not the kind of artifact
version [Versioning](#versioning) rules out.

`configured` and `required` answer **different questions**, and collapsing them breaks a
rollout. A deployment can publish a complete manifest while still serving anonymous
requests (`AUTH_MODE=observe`): a client that read only one of them would either refuse
to sign in when it could, or demand a sign-in nothing is enforcing yet.

- `configured` — this deployment has a complete OAuth manifest, so signing in is possible.
- `required` — a verified token is *required*; requests without one are refused.

Today production answers `configured: false, required: false`, which is the accurate
description of the open door below, **not** an error. A deployment with no account
authentication is a working deployment, so it is a 200 rather than a 404 or a 503 —
otherwise "there are no accounts here" would be indistinguishable from "this is broken".

When `configured` is true the manifest is also present:

```json
{
  "version": 1,
  "environment": "staging",
  "configured": true,
  "required": false,
  "issuer": "https://<project-ref>.supabase.co/auth/v1",
  "authorization_endpoint": "https://<project-ref>.supabase.co/auth/v1/oauth/authorize",
  "token_endpoint": "https://<project-ref>.supabase.co/auth/v1/oauth/token",
  "jwks_uri": "https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json",
  "client_id": "<public-oauth-client-id>",
  "redirect_uri": "http://127.0.0.1:42813/oauth/callback",
  "scopes": ["openid", "email"],
  "account_url": "https://staging.daintree.org/account",
  "subscribe_url": "https://staging.daintree.org/subscribe",
  "session_policy": { "access_token_seconds": 3600, "session_max_age_seconds": 2592000 }
}
```

Nothing here is a secret: every value is operator-supplied deployment configuration, and
no credential this server holds is reachable through it.

The manifest appears **only when it is complete and valid**, which is a stronger
condition than readiness and deliberately independent of it. A configuration that fails
any rule reports `configured: false` rather than publishing something a client would have
to reject — this endpoint answers while the service is unready, so readiness cannot be
what filters it.

The rules mirror the client's own, deliberately. The CLI validates this manifest before
using any of it, so anything it would refuse must never be published in the first place:
that turns a login failing confusingly on a user's machine into a deploy failing at
readiness. A published value must

- be HTTPS, with no credentials, fragment, or control characters (loopback `http://` is
  permitted outside production, so a local Supabase is developable);
- name `supabase.co` or `daintree.org` for the `issuer` — the trust anchor is pinned, so
  discovery can supply the project, the client, and the endpoints, but not which company
  issues a user's tokens;
- for `authorization_endpoint`, `token_endpoint` and `jwks_uri`, share the issuer's origin
  **and** sit under its path, on a segment boundary and with no dot segments;
- for `account_url` and `subscribe_url`, point at `daintree.org` or a subdomain — these
  open in the user's browser under the assistant's own recommendation, so a free choice
  here is a phishing page with the product vouching for it;
`redirect_uri` is **not configurable**. The client compiles the callback in — it is
registered with the identity provider and cannot vary at runtime — and compares this
field against it for equality, so a value this server could vary could only ever be
wrong. It is published from a constant, pinned against the client's own definition.

`account_url` and `subscribe_url` are **optional**: a deployment with working sign-in and
no subscription page still publishes a usable manifest. Withholding the whole manifest
over a missing link would suppress sign-in to enforce a nicety.
- for `client_id`, be at most 200 characters of `A-Za-z0-9-_.~`, with no surrounding
  whitespace — the client trims what it receives and validates the trimmed copy, so a
  value with spaces would validate on both sides while this server published the raw one.

The response carries a strong `ETag` over the exact bytes served, honours
`If-None-Match` with a 304, and is `Cache-Control: private, max-age=60`. `private`
because the response also carries a per-request `X-Request-Id` that a shared cache would
replay to unrelated callers, and 60 seconds so a cached `configured: false` cannot keep
clients from signing in for long after a cutover.

`scopes` is fixed. Supabase supports the standard OAuth scopes and no application-defined
ones, so backend permissions will be derived from the allowlisted `client_id` — never
from a scope the caller could ask for.

`session_policy` is *reported*, not enforced here, and not acted on either: it mirrors the
Supabase project's own session settings so a client can tell someone how long a login
lasts. A client schedules its refresh from the access token's own expiry, which is the
only value that can be right for the token in its hand.

### POST /v1/daintree/auth/verify

A readiness probe for the SERVER's own upstream credential, not the caller's — no caller
key exists to check. It asks the provider directly whether the credential this deployment
holds is usable, which is otherwise only discovered on the first real turn. It is a
tokenless introspection call (not a model call) that spends no tokens, and takes **no
request body**. It is subject to the deployment's `AUTH_MODE` like any other
non-discovery route — under `open` the header is not read, under `enforce` a verified
credential is required — but it carries no entitlement gate, because the probe costs
nothing to run.

`POST /v1/daintree/auth/verify` with no body →

```json
{ "valid": true, "usable": true, "reason": "ok",
  "detail": "The provider accepted this API key.",
  "label": "my-key", "limit": 20, "usage": 4.5, "limit_remaining": 15.5,
  "is_free_tier": false }
```

`valid` and `usable` answer **different questions**, and collapsing them is the bug
this split exists to fix:

- `valid` — the provider recognises this credential.
- `usable` — it recognises it *and* nothing we can see says the account is spent.

A recognised key with a spent balance is `valid: true, usable: false,
reason: "credits_exhausted"`. A client reading only `valid` would show a successful
sign-in, then fail on the first real request with what looks like an unrelated error.

`usable: true` is deliberately the weaker claim — **not** a promise that a turn can
be funded. Usability is judged conservatively: only a *positively reported*
zero-or-negative `limit_remaining` counts as exhausted, so an unlimited or
pay-as-you-go key, which reports no limit at all, is `usable: true` on no evidence
either way. Rendering it as a funding guarantee would make it one the field cannot
keep.

`reason` is the stable machine-readable outcome — branch on it, not on `detail`:
`ok`, `provider_rejected`, `credits_exhausted`.

`label`, `limit`, `usage`, `limit_remaining` and `is_free_tier` are optional: each is
echoed only when the provider supplies it with the expected type. `label` is
upstream-controlled text — it is stripped of control characters, bounded to 120
characters, and dropped entirely if it contains (or is contained by) the bearer, since
some providers default a key's label to the key itself.

A rejected key is a **200 with `valid: false`** and `reason: "provider_rejected"`,
deliberately *not* the `401 provider_invalid_api_key` the serving path returns. A rejection is a
successful answer to the question this endpoint asks, and our own `401` would be wrong
twice over: the request authenticated fine at our door, and `401` conventionally tells a
client to re-send the same credential — the useless retry loop this endpoint exists to
prevent.

Not knowing is never reported as a verdict. Anything other than a provider verdict
propagates as the usual error envelope: `429 upstream_rate_limited` (with `Retry-After`),
`504 upstream_timeout`, `503 upstream_unavailable` (unreachable), `502
upstream_protocol_error` (a 200 whose body the adapter could not read as a credential
answer), and `503` before readiness. The probe carries a short 10s read budget rather than
the main-model timeout. Being a sign-in bootstrap call, it does not require or check
`X-Daintree-Protocol`.

### GET /v1/daintree/account

Who is calling, and what their plan currently permits. Identity-protected,
protocol-3, and **never a paid route** — it invokes no model, spends nothing
upstream, and sits deliberately outside the entitlement guard. Someone with no
subscription has to be able to read their own account and discover that; gating
the endpoint that explains a refusal behind the refusal itself is a loop with no
exit.

```json
{
  "version": 1,
  "email": "person@example.com",
  "subject_hash": "16-lowercase-hex",
  "access": "granted",
  "plan_id": "standard",
  "subscription_status": "active",
  "entitlement_source": "polar",
  "entitlement_stale": false,
  "checked_at": "2026-08-25T12:00:00Z"
}
```

**It is a status READ.** A known missing or inactive subscription is a **200**
whose `access` says so — `subscription_required` or `subscription_inactive`.
Modelling it as an error here would force a client to handle four exception paths
to render an ordinary account page. The same facts are what the paid-work routes
refuse on, as the typed **402** family; see the account error table below.

`access` has one value the website's own contract does not:

- `granted` — a plan includes the Assistant.
- `subscription_required` — authenticated, and no product grants access.
- `subscription_inactive` — a subscription exists and is not currently granting.
- `unverified` — **identity is established and entitlement was not looked up.**
  This deployment is running the identity-only rollout, or has no billing
  authority configured. A client must not read it as "no plan": nothing was
  asked, so nothing was refused.

`email` is display-only and is **omitted** when the token carries no email claim —
never defaulted, never invented. `subject_hash` is a truncated SHA-256 correlation
id computed exactly as the CLI's `auth.SubjectHash` computes it, so an id a person
reads off their own terminal matches the one in this server's logs; the raw
subject is never returned. When `access` is `unverified` the entitlement-only
fields are **absent** rather than null — a `plan_id: null` beside
`entitlement_stale: false` would read as "we checked, you have nothing, and that
answer is fresh", which is three claims none of which were made.

Failures are reported as themselves rather than flattened into `unverified`:
`401 auth_session_revoked` when the billing authority refuses a token that
verified locally (it performs a fresh session check this server cannot),
`403 auth_client_not_allowed` or `auth_permission_denied` when it refuses the
OAuth client, `429 account_rate_limited`, and `503 entitlement_unavailable` for
any outage, timeout or unreadable answer — including a deployment that has a URL
configured but could not build a client for it. **An outage is never reported as
"no subscription."**

Every response — success and failure alike — is `Cache-Control: private, no-store`.
It describes one person's account, and a cached copy is a stale claim about their
money or their session. Note this is a statement about intermediaries, not a
promise of a fresh lookup on every call: the server holds a verdict for at most
60 seconds once granted, and 15 once refused, so a change of plan or a revoked
grant can take that long to show up here.

It is **not gated on readiness**. This route needs the token verifier and the
entitlement client, and neither is part of the model-serving container — so a
missing upstream key or a malformed runbook does not turn "what does my account
look like?" into a 503 about something else entirely.

**It requires an authenticated principal, and the bar is not lowered to make it
callable sooner.** Under `AUTH_MODE=open` the `Authorization` header is not read
at all, and under `observe` a verified caller is deliberately resolved as
anonymous — so both answer `401 auth_required` here. A client should use this
endpoint once `/v1/daintree/auth/config` reports `required: true`, which is the
field that exists for exactly this.

Entitlement is resolved whenever `DAINTREE_ENTITLEMENT_URL` is configured and a
client for it was built, **including while `ENTITLEMENT_MODE` is `off`**. The mode
decides whether a verdict may refuse a turn, not whether one can be obtained — so
the whole contract is exercisable against the real website before it can deny
anybody.

## Protocol negotiation

A generation or runbook-resolve request carries `protocol_version` in its body. Any
request may additionally declare it as a header — `X-Daintree-Protocol: 3`. The
supported range is advertised by `/version` and `/v1/daintree/capabilities`. An
unsupported version yields `426` from either channel.

**3 is the runbook protocol, and it is a hard break from 2.** The response block is
`runbooks` rather than `skills`, selection pins are `pinned_runbook_ids`, the catalog
routes live under `/v1/daintree/runbooks`, and the warning codes say `runbook`. Nothing
answers to the 2 spelling — there are no aliases, no duplicated fields and no
compatibility window, because the only client is the CLI in this repo's submodule and
the two move together. A protocol-2 client is therefore turned away at the door with
`426 unsupported_daintree_protocol` rather than handed a body it would misparse. This is
also why the negotiated version is separate from the `/v1/` URL prefix: the prefix is a
URL-shape escape hatch and has not moved.

The header is checked on every route that runs the caller's content through the model —
`/respond`, `/runbooks/resolve` and `/tasks` alike. (`/tasks` has no `protocol_version`
body field; the header is its whole declaration.) The health, version, capabilities and
`auth/verify` routes are exempt: they exist to *negotiate* or diagnose compatibility, so
failing them on a version mismatch would deny a client the very answer it needs to fix
itself. Omitting the header stays permitted everywhere — only a present, out-of-range
value fails.

## Request correlation

Every response carries `X-Request-Id`, and it is the **only** correlation id: the same
value goes into every structured log line for the request and into the SSE `meta` and
`error` events. A caller may propose one by sending `X-Request-Id`; it is adopted when
it is short, conservatively charactered, and not a fragment of the bearer token (a
pasted API key is *structurally* a valid request id, and would otherwise be logged and
echoed). Otherwise the server mints one silently — a caller's id has no authority here,
so refusing it is never worth failing the request over.

There is no body-borne request id on any endpoint. Quote the response header when
reporting a problem; anything else identifies nothing in the logs.

---

## POST /v1/daintree/respond

The single generation endpoint. Returns a native JSON body, or — when
`generation.stream` is `true` — a named-event SSE stream.

### Request

```jsonc
{
  "protocol_version": 3,                       // required

  "session": {                                 // required (no anonymous mode)
    "id": "sess_…",                            // required, non-empty
    "turn_id": "turn_…",                       // required, non-empty
    "instruction_revision": 0,                 // default 0 — bump on a mid-turn interjection
    "round": 0                                 // default 0 — tool-continuation round counter
  },

  "state": "dst1.<payload>.<sig>",             // nullable signed runbook-state token (see Runbooks)

  "profile": "assistant",                      // default "assistant" | "subagent" (see Profiles)

  "input": {
    "messages": [                              // required, ≥1; roles: user | assistant | tool
      { "role": "user", "content": "…" },
      { "role": "assistant", "content": null,
        "reasoning_content": "…",                // thinking-mode CoT; replay verbatim (see Notes)
        "tool_calls": [ { "id": "call_1", "type": "function",
                          "function": { "name": "git__status", "arguments": "{}" } } ] },
      { "role": "tool", "tool_call_id": "call_1", "content": "…" }
    ],
    "tools": [                                 // the client's current tool inventory
      { "type": "function",
        "function": { "name": "git__status", "description": "…", "parameters": { /* JSON Schema */ } } }
    ],
    "tool_choice": "auto"                       // "auto" | "none" | "required" | { "name": "git__status" }
  },

  "startup": {                                  // required, may be {}; stable across rounds
    "project": {                                // curated project.getCurrent fields only
      "id": "proj_…", "name": "Example", "path": "/path", "status": "active",
      "daintree_config_present": true, "in_repo_settings": true
    },
    "agent_roster": {                           // curated agent.listAvailable result
      "agents": [
        { "id": "codex", "display_name": "Codex", "source": "built-in",
          "availability": "ready", "installed": true, "launchable": true,
          "pinned": true, "toolbar_visible": true }
      ],
      "complete": true, "availability_complete": true, "total_count": 1
    },
    "project_instructions": "…"                // DAINTREE.md; max 16 KiB UTF-8
  },

  "runtime": {                                 // CLI-reported environment (optional, rendered as inert data)
    "permission_tier": "operator",
    "mcp_servers": [                           // the CLI's actual MCP surface (server-rendered)
      { "name": "daintree", "transport": "http", "status": "connected",
        "tool_count": 12, "description": "…", "instructions": "…" }
    ],
    "scheduler_active": true,
    "worktree": {                              // omitted when the live read was unavailable
      "current": {                             // null means Daintree definitively reported none
        "id": "wt_…", "path": "/path/.worktrees/x", "branch": "feature/x",
        "is_main": false,
        "issue_number": 12, "issue_title": "…",
        "pr_number": 34, "pr_title": "…", "pr_url": "https://…",
        "status": "dirty", "last_commit": "abc1234 Add context"
      }
    },
    "open_terminals": [                        // fresh metadata only; no terminal output
      { "id": "terminal-…", "kind": "agent", "worktree_id": "wt_…",
        "title": "Fix context", "agent_id": "codex", "agent_state": "working" }
    ],
    "display": {                               // omitted when there is no terminal to measure
      "columns": 180,                          // the terminal window, in cells
      "content_width": 100                     // the width the reply is actually wrapped at
    }
  },

  "turn": {                                    // per-turn context (optional)
    "goal": "…", "is_wake": false,
    "workflow_runs": ["…"],
    "async_operations": ["…"],                 // live runtime-owned async futures (one line each)
    "memories": { "pinned": ["…"], "relevant": ["…"] },
    "resumed_watchers": ["…"],               // adopted durable watchers, sent on resume
    "workflow_state": [                        // workflow-intelligence digests (client-owned graphs)
      { "id": "wf_…", "goal": "…", "status": "active",
        "progress": "2/5 nodes done; current: verify tests",
        "active_nodes": ["…"], "resources": ["…"], "blockers": ["…"],
        "next_action": "…", "last_event": "…" }
    ]
  },

  "selection": { "policy": "new_instruction", "force": false,     // "new_instruction" | "always"
                 "pinned_runbook_ids": [] },                        // gated: runbooks.pinned_runbook_ids

  "generation": {                              // only supported params exist; unknown keys are rejected
    "temperature": 0.7, "max_tokens": 1024,
    "stop": ["…"], "response_format": "text",  // "text" | "json_object"
    "stream": true
  },

  "client": { "name": "daintree-cli", "version": "…", "platform": "…" }
}
```

Notes:
- The whole body is strict: unknown top-level keys, unknown `generation` params, and
  `system`/`developer` roles are all rejected.
- `startup` is required even when empty. It contains only curated, session-stable project
  identity, agent-registry metadata, and project instructions bounded to 16 KiB of UTF-8.
  It is deliberately
  outside `input.messages`: only the main model receives its framed user-role data block.
  The selector never sees it — its evidence is the sanitized `input.messages`
  transcript plus bounded turn/workflow hints and the available tool names.
- Agent ids are launch identifiers. They are preserved exactly and JSON-quoted in the
  prompt; aggregate limits omit whole agent rows rather than truncating an id. No launch
  commands, environment variables, presets, authentication paths, raw settings, or secrets
  belong in `startup`.
- `runtime.worktree` is a typed, fresh per-request snapshot. An omitted wrapper means the
  live read was unavailable; `{ "current": null }` means Daintree successfully reported
  that there is no current worktree.
- `runtime.display` is the client's live render geometry, both values in terminal cells and
  bounded to 10000. `content_width` is the load-bearing one — the measure the reply is
  actually wrapped at, after the cockpit's inset, gutter and prose cap — and both the
  `# Display context` block handed to the model and the server-side output lint are written
  against it. An omitted block means UNKNOWN (a piped one-shot, the stdio host, the headless
  daemon), not narrow, so `MAIN_TERMINAL_WIDTH` stands. The client republishes on resize.
  Gated on `capabilities.respond.display_context`: `runtime` is `extra="forbid"`, so a
  client must not send the block to a backend that does not advertise it.
- `tools`/`tool_calls` use the standard `{id, type, function:{name, arguments}}` shape.
  `tool_choice` is flattened to `{ "name": … }` for the named case.
- `reasoning_content` (assistant turns only) carries thinking-mode chain-of-thought.
  It appears in the response only when thinking is enabled; on a turn that performed tool calls it
  SHOULD be echoed back verbatim on later requests so the model can continue the reasoning that
  produced those calls (it is ignored on turns that did not). Omit it entirely when thinking is
  off. The server renames it to the upstream's `reasoning` at the provider boundary; the Daintree
  wire name never changes. See Prompt assembly & caching.
- Tool definitions are bounded (total bytes, schema depth, property count, string sizes);
  `function.parameters` must be a JSON object whose root `type`, when present, is `object`.
- `generation.response_format` `"text"` is the default output mode — sending it is equivalent
  to omitting it. `"json_object"` cannot be combined with a non-empty `input.tools` (the
  upstream rejects JSON mode together with function calling); that combination is rejected
  with `invalid_request` / `response_format_with_tools`.
- `turn.workflow_state` carries bounded digests of the CLIENT-owned durable workflow graphs
  (the workflow-intelligence layer). The server renders them into the turn footer and never
  stores them; caps are hard (max digests, one 512-byte line per string, 16 KiB total block —
  advertised by `capabilities.workflow_intelligence`). The full graphs live only in the CLI's
  local store; the server contributes the stateless `workflow_plan` /
  `workflow_reconcile` / `workflow_resume_digest` tasks (see `/v1/daintree/tasks`),
  whose plans/patches are recommendations the client validates and applies itself — they
  carry no execution authority.

### Response — non-streaming

```jsonc
{
  "protocol_version": 3,
  "request_id": "req_…",
  "model": "daintree-assistant",
  "message": {
    "role": "assistant",
    "content": "…",
    "reasoning_content": "…",                   // present only when thinking is enabled
    "tool_calls": [ { "id": "call_…", "type": "function",
                      "function": { "name": "git__status", "arguments": "{…}" } } ]
  },
  "finish_reason": "stop",                      // "stop" | "tool_calls" | "length" | …
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0 },

  "runbooks": {                                   // first-class runbook outcome
    "active":       [ { "id": "…", "title": "…" } ],
    "newly_loaded": [ { "id": "…", "title": "…" } ],
    "prelude": {                                // OPTIONAL: render before the reply
      "tool_executions": [
        { "call":   { "id": "call_…", "type": "function",
                      "function": { "name": "runbook__load", "arguments": "{\"runbookId\":\"…\"}" } },
          "result": { "role": "tool", "tool_call_id": "call_…", "content": "{…}" },
          "display_name": "runbook.load" }
      ]
    },
    "selector": { "ran": true, "degraded": false, "task_type": "…", "confidence": 0.9,
                  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0 } }
  },

  "state": "dst1.…",                            // refreshed token — send back on the next request
  "catalog_revision": "sha256:…",
  "prompt_version": "main",
  "warnings": []
}
```

### Response — streaming (named SSE events)

`Content-Type: text/event-stream`. Events:

```
event: meta
data: { "protocol_version":3, "request_id":"req_…", "model":"daintree-assistant",
        "runbooks": { …same runbooks block… }, "state":"dst1.…",
        "catalog_revision":"sha256:…", "prompt_version":"…", "warnings":[] }

event: status
data: { "phase": "fast_response" }               // a preamble is about to follow (eligible turns only)
                                                 // "executor" once it hands over; "thinking" when CoT
                                                 // begins. IGNORE AN UNKNOWN PHASE — the set grows.

event: preamble                                  // at most once, before any executor output
data: { "id":"pre_…", "content":"Tracing the failing build back to …",
        "provisional": true, "commit_on": "done" }

event: delta
data: { "content": "Hel" }                      // repeated; may carry "reasoning_content" or "tool_calls" fragments

event: compaction                                // optional, at most once, immediately before `done`
data: { "turn_id":"turn-7",
        "replaces": { "start_index":0, "end_index":12 },
        "block": { "role":"user", "name":"daintree_compaction", "content":"…" } }

event: done
data: { "finish_reason": "stop",
        "usage": { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, "cached_tokens":0 } }

event: error
data: { "error": { "type":"…","code":"…","message":"…","param":"…" },
        "request_id":"req_…",                  // same id as the response header
        "retry_after":"5" }                    // optional raw Retry-After on rate limits
```

#### The `preamble` event, and what a client owes it

One short line about the work that is starting, written by a second model running
**concurrently** with the main one and shown while the main one is still opening.
It exists to put honest text on the screen sooner; it is **not** a result, and the
model that wrote it has inspected nothing.

`meta` is still first, and `done` is still terminal. The rest is a contract:

| Rule | Why it is a rule and not a preference |
|---|---|
| Render it immediately, in the assistant bubble | That is the whole point of it |
| **Never commit it to conversation history** | The main model never saw it. A committed line becomes, on the next turn, an assistant turn the model has no memory of writing |
| Drop it when the turn ends — on `done` **and** on `error` | It is display state, not content. Commit only the executor's own output, exactly as you would with no preamble at all |
| A preamble does **not** end the replay-safe window | It is server-generated and nothing of the answer has been shown. Only a visible executor delta makes a retry unsafe |
| Ignore it if you do not implement it | It is additive; an unknown event name is safe to skip |

`provisional` and `commit_on` are still on the payload for clients that already
read them. A client following the rule above never reaches a commit.

**At most one per turn, and never after the first `delta`.** The server races the
two calls and DROPS the line if the answer starts first — so a client never has to
decide what to do with one that arrives late. It simply will not.

Nothing about the preamble reaches the main model: its request is byte-identical
whether the stage ran, was withheld, or is switched off entirely.

`/v1/daintree/capabilities` reports `respond.fast_response.enabled` for the
instance you are talking to. It is always `true` on a current backend — the stage
has no off switch — but read the field rather than assuming it, since that is what
lets an older or differently-configured backend tell you otherwise.

`request_id` is on the **error** event as well as `meta` because a stream can fail
*before* `meta` is ever sent — a preparation failure does exactly that — and `meta` is
otherwise the only event carrying it. The HTTP status is already committed to 200 by
then, so this is the only channel a failed stream has to identify itself.

Guarantees:
- `meta` is always first, before any token — so the client renders the runbook prelude and
  active set up front, as if the runbook load happened before the reply. It is emitted as soon
  as authoritative selection and prompt assembly finish — with local selection that is
  ~10ms in, effectively immediate. `meta` marks runbook selection complete; clients can
  show the runbook-load cue immediately.
- `status` (optional) marks a generation phase. Today the only phase is `thinking`, emitted
  once the instant the model's chain-of-thought begins; the `reasoning_content` deltas that
  follow are the CoT to display. It never appears when thinking is off. Unknown future phases
  should be ignored by clients.
- After `meta`, the server opens the (single) upstream stream. Transient open/prefetch
  failure is retried internally before any model delta; exhaustion emits a terminal
  `error` event in the already-open SSE stream.
- A later mid-stream upstream failure likewise emits a terminal `error` event and stops.
  The stream **never** emits `done` it did not earn (a truncated upstream stream is an error).
- `done` replaces any `[DONE]` sentinel; there is no OpenAI framing.
- `done` is **terminal**: nothing is ever emitted after it. `compaction`, when present,
  arrives immediately before it. That ordering is deliberate — a reader modelled on the
  `data: [DONE]` convention breaks its loop on the terminal event, so a block placed after
  `done` would be silently dropped by exactly the kind of parser a client hand-writes.

---

### Context compaction

Long orchestration sessions resend their whole history every round at full input price.
Most of that history is a stale snapshot of a mutable world, so the server may **propose**
replacing a settled prefix of the conversation with one block of reconciled **current
state**. The client owns its history and performs the replacement; the compaction
subsystem keeps no state of its own. (That is a statement about COMPACTION, not
about the service: a deployment running full-content trace capture stores the
turn like any other. See `docs/INTERNAL_BETA.md` §9.)

When compaction is live, the model declares on the first line of its reply whether that
reply ends the turn. **It is not live in any current deployment**: the rule that asks for
the control line ships only when a compaction producer is configured
(`compactor.available`), and every deployment holds `NullCompactor`, so no reply is asked
for one and `/v1/daintree/capabilities` reports the feature unavailable. Everything in this
section describes the mechanism once a producer exists. The
server strips that line before any byte reaches the client — it never appears in a `delta` —
and on a final reply starts the compaction call while the rest of the message is still
streaming, so the wait is usually hidden behind generation that was happening anyway. It is
overlapped, not free: the call still costs, and a reply that then makes a tool call after all
has its block discarded unsent.

Scope, precisely: `profile: "assistant"`, **streaming** replies, and not a
`response_format: "json_object"` turn (a bare JSON object carries no control line, since one
would invalidate it). Non-streaming turns strip the declaration and never compact — a single
awaited completion has no generation left to overlap with.

`compaction` is **optional on every turn**, and at most one per turn. Most turns emit none,
and a client that receives none simply keeps the history it already holds. Handle it or
ignore it; never wait for it.

Applying a block, exactly:

```js
messages = messages.slice(0, replaces.start_index)
          .concat([block])
          .concat(messages.slice(replaces.end_index))
```

- The indices address the `input.messages` array **of the request that opened this stream**.
  The assistant reply now streaming is not in that array and is appended afterwards as usual.
- Apply only when `turn_id` matches the turn you asked about.
- Send `block` back **verbatim**, `name` included. That reserved name is how the next turn
  recognises already-frozen history: compaction is append-only, and a block fed back through
  the compactor loses a little more each time with nothing in the output to say so. The name
  is stripped before the conversation reaches the model, so it costs nothing upstream.
- The compaction subsystem persists nothing about a block, and none of it rides
  the state token — the span start is re-derived each turn from the reserved
  message name the client splices back in. (Again: scoped to compaction. The
  restricted trace plane, when enabled, captures the turn like any other.)

`/v1/daintree/capabilities` carries a `context_compaction` block: `enabled` is whether THIS
deployment can actually produce one, and `max_block_content_bytes` bounds `block.content`
specifically — the content string in UTF-8 bytes, not the block and not the event. The
`compaction` event is listed in `stream_events` regardless, because a parser that handles it
is correct against every deployment.

---

## Phase timings

Both response shapes carry a `timings` object: **wall-clock milliseconds**,
measured server-side with `time.perf_counter()` around real awaits, so every
figure includes queueing and network exactly as the user experienced it. This is
deliberately not the provider's own latency accounting, which cannot see the
round trip it sits inside.

```json
{
  "timings": {
    "selection_ms": 863,
    "docs_ms": 210,
    "preparation_ms": 1104,
    "upstream_open_ms": 1290,
    "first_output_ms": 1655,
    "generation_ms": 4120,
    "total_ms": 5775
  }
}
```

| field | meaning |
|---|---|
| `selection_ms` | the LOCAL two-stage classifier, end to end. No network, so `calls` is 0 |
| `docs_ms` | the documentation lookup, when the selector asked for one |
| `preparation_ms` | request in → upstream request built. Selection, docs, state, assembly |
| `upstream_open_ms` | request in → the model's first event. Mostly prefill |
| `thinking_ms` | first chain-of-thought fragment → first visible token |
| `first_output_ms` | request in → first visible token. The headline |
| `generation_ms` | first visible token → complete |
| `total_ms` | the whole wait |

Three rules a client must encode:

1. **Absent means the phase did not happen.** Both paths serialize with
   `exclude_none`, so a field that does not apply is *missing*, never `0`. A
   selector that did not run and a selector that answered instantly are different
   facts; reading a missing key as zero merges them. Only `preparation_ms` and
   `total_ms` are always present — every turn prepares, and every turn ends.
2. **They do not sum to `total_ms`.** Read each as "how long did this phase
   take", never as a partition — rounding, retries and work between phases mean
   the arithmetic will not close exactly.
3. **Streaming carries them on `done`, not `meta`.** `meta` is emitted before the
   model is opened — which is what makes it useful — so it cannot know generation
   or total. A client logging a turn reads the terminal event.

One reading that is normal rather than a bug: a non-streaming turn reports **no**
`upstream_open_ms` — one await covers opening and generating, so the whole
upstream wait lands in `first_output_ms` rather than being split on a guess.
`selection_ms` is the LOCAL classifier (~10ms); tens of milliseconds there is
normal, seconds is not.

`thinking_ms` is absent on every normal turn: the whole interactive surface is
non-thinking ([`PROVIDER_POLICY.md`](PROVIDER_POLICY.md) §2). Seeing it means that
posture changed.

---

## Runbooks

Runbook selection is removed from the main thread:

1. **Selector cadence.** A local in-process classifier — no upstream call (historically a small-model call returning a JSON object via
   `response_format` json_object) runs when: there is no valid
   `state`; `selection.force` is set or
   `selection.policy == "always"`; the catalog revision changed; the `turn_id` changed; or
   `instruction_revision` changed. It does **not** re-run on ordinary tool-continuation
   rounds (same turn). Selector failure **fails open** — the prior active set is retained.
2. **Active set.** A **successful** selector run is *authoritative*: its returned ids are the
   complete active set for the instruction (a topic-shift **replacement** — an empty result
   clears the active set), capped (default 3). Each selected specific is additionally paired
   with its **domain foundation** (the `[foundation]` runbook sharing its id namespace) before
   the cap is applied, so `runbooks.active`/`newly_loaded` may include a foundation id the
   selector did not itself return. A tool-continuation round (no run) or a
   degraded selector reuses the prior set unchanged (absent caller pins — see 2b), so a
   classifier hiccup never evicts an in-progress runbook. The selector is instructed to re-list any active runbook it means to
   keep. (The explicit `resolve` `load` action, by contrast, is additive over the prior set.)
2b. **Caller pins.** `selection.pinned_runbook_ids` names runbooks the caller requires this
   turn whatever the classifier decides. (`force` means "re-run the selector", never
   "select this runbook".) It exists for runbook **development** — exercising a runbook you are
   rewriting against a real turn, instead of reverse-engineering a prompt that talks the
   selector into picking it. The rules:
   - **Alongside, not instead of.** Selection still runs on its usual cadence; pins do not
     force it, suppress it, or alter `_should_run_selector`.
   - **Admitted first, and paired.** Pins take their specific+domain-foundation units out of
     the cap before the selection is budgeted, the mirror of the server's own documentation
     pin (which takes the *leftover* slot, unpaired, because it is the server's inference
     rather than the caller's instruction). A pin whose whole unit does not fit the cap is
     skipped entirely rather than admitted half. The one case a pin loads bare is
     `profile: "subagent"`, where the menu is narrowed to runbooks the caller can execute and
     a foundation requiring tools it does not hold is filtered out of that menu — the same
     capability-first exception the profile already makes for selected runbooks.
   - **A pin displaces inherited runbooks.** On a continuation round the prior active set is
     budgeted *after* the pins, so a pin sent mid-turn can truncate it; the remainder is
     truncated verbatim, which can leave an inherited specific without the foundation it
     arrived with. Sending no pins leaves reuse exactly as it was.
   - **Counted against the cap**, not exempt from it — an exemption would make the
     advertised `max_active_runbooks` false and let a pinned turn grow the prompt without
     bound.
   - **Applied every round**, so a pin sent on a tool-continuation round is honoured
     without re-running the selector. Once admitted it rides the state token like any other
     active runbook; resend it to keep it through a fresh authoritative round. One exception:
     under `profile: "subagent"` a prior set is re-filtered to the selectable menu each
     round, so a pinned runbook that is hidden from selection (today only the documentation
     runbook, which that profile does not want anyway) does not persist there — resend it
     every round, or simply do not pin it under that profile.
   - **Refusals are reported, never silent** — a mistyped id that produces a normal-looking
     turn is exactly the failure this would be debugged into. `warnings` carries
     `unknown_runbook_id_ignored` (no such runbook), `pinned_runbook_not_executable` (a real runbook
     outside this `profile`'s narrowed menu — a pin overrides the classifier, not the
     capability boundary), and `pinned_runbook_over_cap` (the pins alone did not fit). None of
     these is an error: whether a pin fits depends on the live catalog, the foundation
     topology and the configured cap, none of which a request-shape validator can see.
   Gated by `runbooks.pinned_runbook_ids` in `/v1/daintree/capabilities` — `selection` is
   `extra="forbid"`, so sending the field to a deployment that predates it 422s the turn.
3. **One upstream call on an ordinary turn; two whenever the fast-response stage
   RUNS.**
   Selection is local (an in-process classifier, ~10ms), so the main path is
   strictly sequential: select → assemble → one main completion. There is no
   speculative or private pre-selection stream. When the stage is enabled and the
   turn is eligible, a second internal call runs **at the same time** as the main
   one and nothing waits on it. Note that it runs — and is billed — whether or not
   you ever see a `preamble`: in `shadow` mode it is generated and withheld, a
   reply the validator refuses is discarded, and a reply that arrives after the
   answer has started is dropped. The main call's input never contains the
   preamble, which is what makes the two concurrent rather than sequential.

   "Same connection" refers to the SSE response you are reading — one request in,
   one stream out, with both internal calls behind it. Upstream, the two hosts use
   separate HTTP clients and separate connection pools on purpose.
4. **Injection.** Only the active runbook *bodies* are injected into the main prompt — never
   the full catalog — and they reach the model as **plain context** (the "Loaded runbooks"
   user-role block), not a tool call. The model is **not** shown a synthetic `runbook__load`
   self-call; a `runbooks.prelude` is still returned for the client to render a visible
   "Runbook loaded" card.
5. **State token.** `state` is an opaque, signed (`HMAC-SHA256`, key-ring/`kid` rotation),
   session-bound, expiring token carrying the active runbooks + catalog revision + cadence
   keys. It is verified fail-closed; an invalid/expired/wrong-session token is ignored
   (selection simply re-runs) and a `warnings` entry is added.

`runbook.find` / `runbook.load` (and their wire-safe `runbook__find` / `runbook__load` forms) are
reserved and rejected if a client offers them as tools or replays them in history.

`/v1/daintree/capabilities` carries `runbooks.catalog`: every catalog runbook as a minimal
`{id, title}` object, sorted by id, so a client naming a runbook can validate the id and
suggest near misses before spending a turn. It is the **canonical full catalog** —
capabilities has neither a `profile` nor a tool inventory, so it is not narrowed to a
profile's executable menu, and server-pinned runbooks are included because manual `load`
accepts them. Cache it against the `catalog_revision` alongside it: both come from one
catalog snapshot. That keying is **conservative, not exact** — the revision hashes every
runbook's metadata *and body*, so it moves on a body-only edit that leaves the id/title list
byte-identical; a client will occasionally re-fetch a list that did not change. For the
full metadata (summary, when-to-use, risk, required tools) use `/v1/daintree/runbooks`,
which serves it with an ETag; `runbooks.catalog` deliberately does not repeat it.

---

## Profiles

`profile` selects which persona answers a request. Omitted means `"assistant"` — the
orchestrator the human is talking to, and the shape every ordinary turn takes.

`"subagent"` is a bounded, **read-only** worker the CLI runs in its own isolated
conversation so the main thread can learn one answer without paying the context cost
of finding it. It is not a hint: it selects a different assembly path.

|                                | `assistant`                   | `subagent`                                |
|--------------------------------|-------------------------------|-------------------------------------------|
| System prompt                  | orchestrator persona          | sub-agent persona                         |
| Runbook selection                | full catalog                  | menu narrowed to runbooks the caller's tools can execute |
| Active-set cap                 | `max_active_runbooks`           | `min(that, 2)`                            |
| Documentation lookup           | on                            | off — it researches the caller's PROJECT  |
| Integrations / runtime / turn footer / display | rendered      | dropped                                   |
| Startup block                  | yes                           | yes — it must know which project it is in |

The narrowed menu is filtered on each runbook's declared `requiredTools`: a runbook whose
steps call tools the caller does not hold is worse than no runbook, because it spends
context and then steers the model toward calls that can only fail.

The signed `state` token records the profile that resolved its active set. A token issued
under one profile forces a fresh selection under the other rather than being reused — the
two profiles choose from different menus, so a set that is legal for one can be illegal
for the other.

## Documentation lookup

Questions about **Daintree itself** are answered by the server, not by handing the client a
tool call to run. The public documentation MCP (`https://daintree.org/api/mcp`) is no-auth,
stateless and identical for every caller, so nothing about it needs to run on the user's
machine — unlike the Daintree control plane, which drives local terminals and worktrees
behind a per-session token.

1. **Classification.** The local selector already on the turn's first-byte path returns a
   second answer alongside `runbook_ids`: `docs_query`, a short search string, or `null`. It is
   independent of runbook selection — a turn can need both, either, or neither — and `null` is
   the common case. A turn that skips selection (continuation round, no-task fast path) or
   whose selector degraded never looks anything up.
2. **Retrieval.** One stateless JSON-RPC `tools/call` to the docs server's `search` tool,
   before the main upstream stream opens. It gets its own slice of the turn's `preparation`
   budget — *minus a reserve* for the assembly and teardown that still follow — enforced as a
   wall-clock bound and skipped entirely when too little remains. A slow docs host therefore
   costs first-byte latency and never the turn. Redirects are refused rather than followed,
   and the response is read under a size cap.
3. **Injection.** The top few ranked sections are sanitized, byte-capped, framed as an inert
   `<daintree_docs>` data block and appended to the **volatile user-role tail** described
   below — never a system message, which would re-encode the tool schemas for content that lives
   for exactly one turn. Each section carries a complete `URL:` built server-side from a
   validated same-site path; the model cites what it is given rather than assembling a URL.
3b. **Continuation.** The query is carried in the signed state token, scoped to the
   `turn_id`, and re-run on the turn's tool-continuation rounds — those skip selection, and
   the tail carrying the pages is rebuilt every round, so without this a usage question that
   takes a tool call to answer would lose its sources between asking and answering. A new
   instruction is classified afresh and never inherits the query.
4. **Visibility.** The search appears in `runbooks.prelude.tool_executions` as a `docs__search`
   execution (display name `docs.search`), the same channel a runbook load rides, so the user
   sees which query produced the cited pages. Its result carries the matched **paths only** —
   the page text is already going to the model through the prompt.
**Best-effort by construction.** Every failure — no client, no budget, a docs server that
errors, times out or matches nothing — leaves the turn to answer without documentation. It
never fails the request. Advertised as `capabilities.docs_lookup`; a client gating on it
should stop offering its own docs tools, since two paths to the same answer means the model
uses both.

**The tradeoff, stated:** docs answers now depend on the backend being reachable. They
previously survived a backend outage and a Daintree outage independently, and usage
questions get asked precisely when something is broken.

The `respond` log carries selection outcome and timing;
`respond_upstream_open` records the open-to-first-event wait. Dedicated cleanup-timeout events
cover degraded lifecycle paths without logging request content.

## Prompt assembly & caching

The serving model's chat template serializes the prompt as **[all system messages, in
array order] → [tools] → [non-system conversation]** — a system message anywhere in the array
lands in the leading system region, before the ~21.5k-token tool schemas
(measured 2026-07-08: volatile system-role runtime context after the
conversation cut byte-identical turns to a 36% cache hit; 99% after this
layout). The rule: **only content that is stable for every caller may be
system-role**; everything else rides a framed user-role data row below the
schemas, and per-request variable context rides ONE trailing user-role
"[Injected context …]" block, where it costs exactly its own tokens.

The segments below are ordered by **how widely each can be shared**, widest
first, so a change to one never re-encodes anything above it:

| # | Serialized segment | Placement | Stability |
|---|---------|------|-----------|
| 1 | Base system prompt | system | static — cached across all sessions |
| 2 | Response contract | system | static — output-shape rules; the last system block |
| 3 | Tool schemas | top-level `tools` | 77 schemas, 77,258 compact-JSON bytes, ~21.5k tokens (measured 2026-08-16) |
| 4 | Active runbook bodies | user | shared by TASK — any two sessions running the same runbook share the prefix through here. Replaced wholesale on a selector topic shift; omitted when empty |
| 5 | Integrations (MCP servers) | user | per session and instance — carries the client's MCP endpoint URL. Client tools are NOT listed here; their schemas already ride the function-calling interface |
| 6 | Injected startup context | user | session-stable project/agent/instruction data. The `startup` request field is required; the row is emitted only when it renders non-empty |
| 7 | Visible `input.messages` conversation | user/assistant/tool | append-only. The learner reads a bounded transcript of it; the selector reads only the newest message plus a small labelled slice of history |
| 8 | Injected context (runtime + display + turn footer + retrieved docs) | user | per-request tail — the volatile block, and normally the only row that moves between rounds. Always present: the display fragment (terminal width, pipe-table cap) renders even when the client sends no runtime |

Rows 4 and 5 were system-role until issue #39. That put a per-machine MCP
endpoint URL and every selector topic shift ahead of all 21.5k tokens of
schemas; moving them below makes a runbook load cost its own bytes instead of the
tool region. Runbook bodies stay ahead of the conversation rather than joining the
volatile tail — a large runbook in the tail is re-paid uncached on every round of
every turn, far worse than one conversation re-encode when the set changes.

All client-provided context (ids, names, MCP details, project instructions, memories,
worktrees) is treated as untrusted data. Prose fields collapse whitespace; multiline fields
(project instructions, MCP instructions) are framed in delimited, escape-neutralized data
blocks. Agent ids are the exception to prose normalization: the assembler preserves their
exact value, JSON-quotes it, and admits or omits each complete row under the roster budget.

No `prompt_cache_key` is sent, and no `provider` block: a model host serves one endpoint per
model, so there is nothing to select between and no cache-affinity hint to place. Prefix
caching is implicit at the endpoint that serves the request.

**There is no endpoint-selection policy at all any more.** The privacy filter, throughput
sort, fallback pool, output-quality denylist, sticky-affinity key and caller-facing `routing`
block all existed to steer a router across many interchangeable endpoints serving one model.
A host has no such pool, so every one of them was deleted rather than neutered, along with
the `upstream_no_compliant_provider` branch of the error taxonomy. Git history has the
previous revision if that shape is ever needed again.

Reasoning is controlled by the unified `reasoning` object, sent explicitly on every call:
`{"enabled": false}` turns thinking off, `{"effort": "<tier>"}` turns it on at a tier.
DeepSeek V4 Flash thinks when the field is ABSENT, so the explicit directive is what keeps a
non-thinking call actually non-thinking — measured, `{"enabled": false}` drops
`reasoning_tokens` out of the usage block entirely. Runbook selection sends nothing at all: it
is local and makes no upstream call.

The thinking posture is **hardcoded — a product decision like the model ids, not
configuration**. **The whole interactive surface is non-thinking**: the main `/respond` call,
every utility task, and the selector all send `{"enabled": false}`. This workload does not
need a chain-of-thought — the main model routes a request onto a runbook and emits tool
calls, and the utility tasks are bounded extractions and classifications over text the client
already gathered — while reasoning cost seconds of first-byte latency (a measured greeting
turn spent 366 reasoning tokens and ~3.5s of an 11.5s wall-clock).

There is **no middle tier to retreat to**: measured over 5 samples each on a fixed prompt,
`low` and `high` produced statistically identical chain-of-thought (mean 662 vs 656 reasoning
tokens), so the only real step below `high` is off — which is the posture taken. Runbook
selection is not part of this decision at all any more: it runs locally and makes no upstream
call, so it has no thinking posture to set. (Historically it did, and it reached "off" first
on its own first-byte argument.)

**Nothing thinks, and on Together that is a flag genuinely held down.** DeepSeek V4 Flash
reasons whenever the field is absent, so every call sends `reasoning: {"enabled": false}` —
measured, that drops `reasoning_tokens` out of the usage block entirely — a flag genuinely
held down, not a property of the model.

`/respond` advertises the posture as `respond.thinking_enabled` on `/v1/daintree/capabilities`
(currently `false`), which is what a client should gate any thinking UI on. Because
`max_tokens` is shared by reasoning + visible output, a small-output **thinking** call adds
`THINKING_HEADROOM_TOKENS` to its tight cap so the chain-of-thought can't starve the answer
into a length truncation. Under the current posture there is no such call: a non-thinking
utility task deliberately does *not* get that slack, since it would raise the visible-output
ceiling above the cap its profile set. The main `/respond` call instead runs on its own flat
budget (`MAIN_DEFAULT_MAX_TOKENS` == `MAIN_MAX_TOKENS_CEILING`).

Thinking is independently forced off for any request that pins a `tool_choice` (thinking + a
forced tool choice is rejected by the model) — that guard is enforced separately from the posture, so it holds if the posture ever
flips back. When thinking is enabled, the model returns its chain-of-thought as
`reasoning_content`; the client stores it on the assistant turn and replays it verbatim on any
turn that performed tool calls, so the model can continue that reasoning. That round-trip stays
in the contract. Thinking mode ignores `temperature`/`top_p`/penalties.

## Errors

Stable envelope (request/auth/validation failures before SSE as JSON; generation failures
after streaming `meta` as an `error` event):

```jsonc
{ "error": { "type": "invalid_request_error" | "authentication_error" | "rate_limit_error" | "api_error",
             "code": "…", "message": "…", "param": "…" } }
```

Codes are the contract; **branch on `code`, never on `message` or on the HTTP
status alone** (502 covers more than one condition). Each row names who can fix
the problem, because that is the whole reason these are separate codes.

### Daintree's own failures

| HTTP | Code | Meaning / action |
|------|------|------------------|
| 400  | `system_messages_not_allowed`, `reserved_tool_name`, `invalid_tool_schema`, `tool_choice_not_offered`, `invalid_request_error` | The request is malformed. Client bug. |
| 400  | `missing_tool_call_id`, `dangling_tool_message`, `duplicate_tool_call_id`, `unresolved_tool_calls`, `malformed_tool_call_id`, `tool_calls_on_non_assistant_message` | The tool-call transcript is not a valid exchange. An assistant turn opens a BATCH of calls, every call is answered exactly once, and only then may a user/assistant message follow; order *within* a batch is free. Ids are compared verbatim — surrounding whitespace is rejected rather than trimmed, because they travel upstream raw. A trailing unresolved batch is legal. Client bug in every case: report it with the request id rather than retrying. |
| 400  | `invalid_image`, `remote_image_not_allowed` | An image part failed the data-URL policy: media type outside the allowlist (`image/png`, `image/jpeg`, `image/webp`, `image/gif`), not `;base64`, empty, or not valid base64. |
| 401  | `invalid_api_key` | About the SERVER's own upstream credential, never the caller's, and not returned as an HTTP error by anything today: `POST /v1/daintree/auth/verify` and `/v1/daintree/doctor` both catch it internally and report it as a 200 verdict field instead. **Not** what account authentication returns — that is `auth_required` / `auth_token_invalid` in the account family below, which say which of the two happened. This code predates them and is on its way out. |
| 413  | `request_too_large` | Body cap, a per-message / per-tool byte cap, or a request-SHAPE cap: too many messages, content parts, tool calls in one message, stop sequences, pinned runbook ids or images. `param` names the field. |
| 413  | `context_window_exceeded` | The assembled prompt does not fit the model's context window. Distinct from `request_too_large`, which is about the bytes the CLIENT sent: this one counts the whole prompt — base prompt, tool schemas, active runbook bodies, startup and the transcript — so a small request can trip it in a long session. Not retryable; start a new session or shorten the conversation. Raised before the upstream call when the server can see it, and mapped from the host's own rejection when it cannot, so the code is the same either way. |
| 426  | `unsupported_daintree_protocol` | Upgrade the client. |
| 404  | `unknown_task` | `/v1/daintree/tasks` was asked for a task profile that does not exist. Read the available ids from `/v1/daintree/capabilities`. |
| 503  | *(not ready)* | `/readyz` is failing; the instance has not finished starting or is misconfigured. |
| 500  | `internal_error` | Daintree bug. Report with the request id. |

### Your Daintree account

**Which of these a deployment can actually return depends on its posture, and the
codes themselves are stable regardless.** The identity codes are raised under
`AUTH_MODE=enforce`; the subscription and dependency codes under
`ENTITLEMENT_MODE=enforce`. Under the shipped defaults (`open` and `off`) none of
them is reachable, and that is a rollout state rather than a gap in the contract —
the CLI already recognises all twelve, so the taxonomy agrees on both sides before
either side switches its half on.

Two remain unreachable for a reason other than posture. `auth_permission_denied`
needs a second OAuth client with a narrower grant, and there is only one;
`usage_limit_reached` needs numeric plan allowances, which are a product decision
nobody has made — this server records what a turn cost and enforces no quota.

The "what a client should do" column describes the correct handling, not a survey of what
any client has built: the CLI carries a typed remedy for each of the five identity codes,
and classifies the rest for retryability without yet acting on them.

Three groups, and the grouping is what a client should switch on.

**Identity — who you are.** Each has a different next step, which is the reason they are
not one code.

| HTTP | Code | Meaning | What a client should do |
|------|------|---------|--------------------------|
| 401 | `auth_required` | No credential arrived. | Offer sign-in. Never retry automatically. |
| 401 | `auth_token_invalid` | Malformed, wrong issuer/audience, or a bad signature. | Refresh only if a stored session exists to refresh from; otherwise sign in. |
| 401 | `auth_token_expired` | The credential verified and is past its expiry. | Refresh once, then replay — but only while nothing has been shown to the user. |
| 401 | `auth_session_revoked` | The signature is fine; the session is gone (logout, grant revocation, security action). | Delete the local credential. Refreshing cannot succeed — the session that would authorize it no longer exists. |
| 403 | `auth_client_not_allowed` | A valid, current token from an OAuth client this deployment does not accept. | Report the mismatch. **Never refresh** — every refresh mints another token wrong in the same way. |
| 403 | `auth_permission_denied` | A credential this deployment DOES accept, for an operation it may not perform. | Report it. Never refresh, for the same reason — a new token carries the same authority. |

The two 403s differ in their message rather than their remedy, and the split is worth the
extra code: telling someone their OAuth client is not accepted, when it is, sends whoever
reads that log hunting a configuration error that does not exist. `auth_permission_denied`
is unreachable today — every allowlisted client holds the whole permission set — and is
defined now so the wrong code cannot be correct-by-accident until a narrower client
exists.

Which specific check failed is deliberately not on the wire: `auth_token_invalid` covers
all of them. Narrating the reason to an unauthenticated caller would make the endpoint an
oracle for what this deployment accepts. The detail is in the server log under the request
id.

Every 401 here carries `WWW-Authenticate` (bare `Bearer` for `auth_required`, since no
token was presented; `Bearer error="invalid_token"` for the rest). The 403 does not — RFC
7235 reserves the challenge for 401, and offering one here would invite exactly the
refresh loop that cannot terminate.

**Subscription — authenticated, not entitled.** The login is fine in both cases and must
be kept; signing the user out on the way to a checkout page is a bug.

| HTTP | Code | Meaning | What a client should do |
|------|------|---------|--------------------------|
| 402 | `subscription_required` | No product on this account includes the Assistant. | Open the `subscribe_url` from `/v1/daintree/auth/config`. |
| 402 | `subscription_inactive` | A subscription exists but is not currently granting access (payment failed, paused, ended). | Open the `account_url` from `/v1/daintree/auth/config` — **not** a second checkout. Offering a fresh purchase to someone whose card bounced is how a person pays twice. |

**Usage and dependency.**

| HTTP | Code | Meaning | What a client should do |
|------|------|---------|--------------------------|
| 429 | `usage_limit_reached` | The plan's hard usage allowance for the period is spent. | Send them to `account_url` to upgrade. Not retryable — it holds until the period rolls over or the plan changes. The error carries no period or reset timestamp, so a client cannot show when it lifts. |
| 429 | `account_rate_limited` | Too many requests from this account. Transient. | Honour `Retry-After` when present; otherwise back off. |
| 503 | `auth_dependency_unavailable` | Session validity could not be established. | Retry with backoff. **This is not a logout** — do not delete the credential. |
| 503 | `entitlement_unavailable` | The billing authority is unreachable and no acceptable recent result exists. | Retry. **Never present the user as unsubscribed** — telling a paying customer to buy again during an outage is the worst outcome this taxonomy exists to prevent. |
| 503 | `usage_accounting_unavailable` | A durable usage write failed before any paid work began, so nothing was started. | Retry. Nothing was spent. |

None of the 503s carries a fixed `Retry-After`. That is deliberate rather than an
omission: a client treats a server-supplied value as authoritative and drops its own
jittered backoff for it, so a hardcoded delay would pin every client to one cadence for
the length of an outage — synchronising the herd against the dependency that is already
struggling. One is sent only when something actually knows a recovery estimate.

**Branch on `code`, not on status.** The two 429s share a status and are opposites: one is
transient, the other holds for the rest of the period. The two 402s are both final and
differ only in where to send the user. The three 503s are alike — all "we could not
check", all to be retried with the credential preserved. A client branching on status
alone would replay an exhausted quota until its backoff budget was gone, or delete a good
credential because a database blinked.

### The server's provider account

No caller key rides the request, so these describe the DEPLOYMENT's own upstream
credential — each one is an operator fix, never something the caller can act on.
Never retried.

| HTTP | Code | Action |
|------|------|--------|
| 401  | `provider_invalid_api_key` | The upstream rejected the server's key. Replace or rotate it. Distinct from `invalid_api_key`: the key travelled fine, the provider refused it. |
| 402  | `provider_insufficient_credits` | The account cannot fund the call. Add credits or use another key. |
| 403  | `provider_key_forbidden` | Key model-permissions, spend limit or a provider guardrail. |

### Provider routing and availability

| HTTP | Code | Action |
|------|------|--------|
| 429  | `upstream_rate_limited` | The upstream is rate limiting. Honour `Retry-After`. Retryable. |
| 503  | `upstream_unavailable` | Provider outage or no capacity. Retryable. |
| 503  | `provider_not_configured` | The task requires an optional second provider whose credential this deployment never set. **Not** retryable — no amount of waiting configures a key. Every other task and every turn is unaffected; set the named provider's key to enable it. |
| 504  | `upstream_timeout` | Upstream inactivity/deadline. Retryable. |

### Faults on our side of the upstream call

| HTTP | Code | Action |
|------|------|--------|
| 502  | `upstream_request_rejected` | The upstream judged the body **Daintree generated** malformed. Almost always a server bug; report the request id. Carries the provider's own reason. |
| 502  | `upstream_protocol_error` | An unparseable response: bad JSON, a broken chunk, a stream that ended without `[DONE]`. Provider/compatibility problem. |
| 502  | `upstream_runaway_output` | The upstream model degenerated: one round streamed more visible prose than the runaway ceiling (`MAIN_STREAM_RUNAWAY_CHARS`, default 16000 characters; tool-call arguments do not count) and the server hung up on it mid-stream rather than pay out the rest of the token budget. **Streaming only** — a buffered response has already been generated and billed by the time the server sees it, so there is nothing left to interrupt. Terminal on purpose rather than a truncated `done`: the text delivered so far is unusable, and committing it to history is what makes the NEXT turn degenerate too, so discard the round's assistant content and tool calls. **Not auto-retried, by design** — visible tokens have necessarily already reached the screen, and the CLI's retry boundary is the first content fragment (replaying would duplicate on-screen text). Re-asking is a fresh turn, which may route to a different endpoint. |
| 502  | `upstream_error` | Residual/legacy catch-all. Still emitted for a missing or malformed upstream credential and for a non-verdict verification failure. |

Raw upstream bodies are never returned. For a non-auth 4xx a **bounded,
credential-scrubbed excerpt** of the provider's reason is appended to the message
— it is usually the only thing that says *why*. Account-class failures carry no
upstream prose at all.

For a streaming `/respond`, public generation begins only after the `200` and `meta` event;
an exhausted 429/502/504 open failure therefore uses the SSE `error` shape rather than
changing the already-committed HTTP status. When an exhausted rate limit carried
`Retry-After`, its raw string value is preserved as the optional top-level SSE
`retry_after` field; other terminal stream errors omit the field.

---

## Other endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/daintree/respond` | yes | Generation (native, + first-class runbooks) |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task ids, catalog revision + runbook `{id, title}` listing |
| `POST` | `/v1/daintree/auth/verify` | yes | Sign-in probe: does the upstream recognise the server credential? (no body; no inference). `valid` plus optional budget metadata; recognised is not the same as fundable |
| `GET`  | `/v1/daintree/runbooks` | yes | Runbook metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/runbooks/resolve` | yes | Manual `find` / `load` / `clear`; returns the same `runbooks` block + `state` |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks (server-owned prompts; no client task prompt) |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

## Versioning

`protocol_version` is explicit per request. `prompt_version` is a stable prompt-family
label (`"main"`) — pre-release, it is not bumped on prompt edits; everything on disk is
simply the current prompt. `catalog_revision` is the real change signal: it is a content
hash that moves whenever any runbook changes, so the client re-runs selection when the
catalog moves. Both are returned on every response.
