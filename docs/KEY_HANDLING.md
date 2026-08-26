# Key handling

**Authoritative** for: what a Daintree API key *is*, where it lives, how it moves,
and everywhere it must never appear.

---

## 1. Every key is the SERVER's, and callers hold none

**This inverted when callers stopped supplying keys**, and it inverted in the direction
that costs money if a reader remembers the old rule. It used to be that a
Daintree credential *was* the caller's own OpenRouter key, playing two roles at
once: authenticating the request, and funding that turn's model calls. The caller
paid.

Now every credential belongs to the SERVER and **Daintree pays**. Three of them
concern providers and tracing, and the distinction between them is which failure
they cause (the state-signing keys in §6 are a separate matter — they sign, they
never spend):

| Env var | Role | Missing means |
|---|---|---|
| `TOGETHER_API_KEY` (`config.EXECUTOR_KEY_ENV`) | funds every turn | `/readyz` fails — nothing can be answered |
| `CEREBRAS_API_KEY` (`config.FAST_RESPONSE_KEY_ENV`) | funds the preamble stage, which runs on every eligible turn | `/readyz` fails — the stage has no off switch, so a missing key fails it open on every turn |
| `TRACE_IDENTITY_KEY` | pseudonymises trace identity — spends nothing | nothing, while trace capture is off; `/readyz` fails if it is on |

`TRACE_IDENTITY_KEY` is deliberately NOT the state-signing key and not the state
salt. Sharing would make a compromise of either a compromise of both, and rotating
one would silently re-pseudonymise every stored trace — splitting one subject into
two identities in the middle of a corpus.

Everything in sections 2-4 below applies to all three: none is ever logged,
echoed, stored in a trace, or returned in an error body. Consequences that follow directly, and that the
code enforces:

- **The serving backend holds the model credential.** `main._build_provider`
  passes `settings.serving_api_key`, and `missing_secrets()` gates `/readyz` on
  its presence — without it nothing can be answered at all, and discovering that
  on the first turn surfaces as an upstream 401 that reads like the caller's
  fault. Unit tests pin both halves.
- **Under the shipped posture the `Authorization` header is never read for
  authentication.** Not parsed, not verified, not rejected. `auth.authenticate`
  dispatches on `AUTH_MODE`, whose default is `open`, and returns the same
  anonymous principal whatever arrives. `Principal` carries no credential field
  in any mode.

  The header IS read for redaction, in every mode and before authentication runs
  at all — see §2a. That is not a contradiction of the above: one reads it to
  decide who you are, the other reads it to make sure it never appears in a log,
  and the second job cannot be done by refusing to look.

  That is stronger than "a key is optional", and the difference is why it is
  written this way. A CLI mid-migration may still be sending the OpenRouter key
  it used to authenticate with. A door that *read* that header would carry the
  key on the principal, thread it to every provider call, and forward it to
  Together — which rejects a credential it never issued. The caller would see
  `provider_invalid_api_key`: an error blaming their key for a header they were
  right to still be sending. Refusing to look at it is what makes an old client
  work rather than fail confusingly.

  The same reasoning retires the structural check that used to live here.
  Rejecting a malformed bearer only made sense while a bearer *meant* something
  — it warned a caller that the credential they meant to use was not going to be.
  Now no credential is ever used, so a 401 on a stray header would refuse a
  request the server can serve, over a value it does not read.

  `tests/unit/test_docs_drift.py` asserts both halves: the door ignores every
  header shape, and no route under `api/` threads `principal.api_key` to a
  provider call.
- **A per-request credential is still possible, and still tested.**
  `ProviderChatRequest.api_key` overrides the server's default in
  `OpenRouterProvider._resolve_key`. Nothing on the serving path sets it —
  benchmarks and live tests are its only callers — and it is kept live precisely
  so per-account credentials become a *value* flowing through existing plumbing
  rather than new plumbing.
- **The seam has been filled; what is still staged is the posture.** Account
  authentication is built and tested. `authenticate` reads the header again under
  `observe` and `enforce`, and validates it by verifying a real signature against
  a cached JWKS (`services/supabase_auth.py`) rather than against its own shape —
  and the rest of the server did not move, which is what the seam was for. What
  remains a rollout decision is which mode a deployment runs; the shipped default
  is `open`. What identity landing did NOT do is the thing the old sentence left
  room for: an account does not map to a credential of the caller's. It is an
  ENTITLEMENT to spend Daintree's key, checked on a second and independently
  staged axis (`ENTITLEMENT_MODE`, shipped `off`). The present tense is
  deliberate — `ProviderChatRequest.api_key` is kept alive and tested precisely
  as the field a per-account credential would occupy, so "never" would
  contradict a seam this codebase maintains on purpose. What is true today is
  that nothing on the serving path fills it.

## 2a. The ACCOUNT credential

A different secret with different rules, and the separation is the point: the
provider key below is the deployment's and is spent upstream; this one is a
user's and is never spent anywhere — it is only ever checked.

| Property | Rule | Code |
|---|---|---|
| Read | every raw `Authorization` header, never a single-value accessor. `Headers.get` returns the first of two and `dict(scope["headers"])` keeps the last, so either silently validates a credential the upstream may not have seen | `services/bearer.py` |
| Shape | exactly one header, one credential, RFC 6750 `b64token` grammar, 8 KiB. Refused rather than trimmed, so the value checked and the value used are one string | `services/bearer.py` |
| Verified | locally, against a cached JWKS. Algorithms, audience, skew and required claims are hardcoded; only the project and the client id are configured | `services/supabase_auth.py` |
| In-process form | not stored. It is verified and discarded; `Principal` carries the resulting identity | `auth.Principal` |
| Propagation | **none.** It is never forwarded, never logged, never placed on a request object, and never reaches a provider | — |
| Redaction | every raw header, split into its fields, is compared against an inbound `X-Request-Id` before that id is adopted — including a comma-folded one, where the credential is only visible once the value is split | `main._safe_request_id` |
| In errors | never. A refusal names the rule that failed, not the value; the four identity 401s take no message parameter at all, so a caller cannot be handed `str(exception)` by a future edit | `errors.py`, `services/bearer.py` |

**What is not yet true.** A verified token proves who is calling and that the
token has not expired. It does not, on its own, prove the session still exists:
nothing reaches back into a minted JWT, so a logout leaves the signature valid
until `exp`.

The entitlement lookup narrows that where it runs, as a side effect rather than as
a revocation check. The website's endpoint is specified to perform a fresh
Supabase user check, and `services/entitlement.py` maps a 401 from it to
`auth_session_revoked` — so on `/respond` and `/tasks` under a non-`off`
`ENTITLEMENT_MODE`, and on `/v1/daintree/account` wherever a URL is configured,
the window becomes the verdict cache's TTL rather than `exp`. Identity-only
deployments keep the full window, and a direct session lookup still needs
infrastructure this service does not have (`TODO.md`).

The bearer forwarded to that endpoint is the CALLER's own and is transient: it is
never stored, never a cache key, never logged, and never chained into an
exception.


## 2. Server-side handling

> **The first three rows below described a CALLER-SUPPLIED PROVIDER KEY and are
> retired for good.** No bearer has ever been a model credential since the key
> moved to the server, and none ever will be again: an account is an entitlement
> to spend Daintree's key, not a mapping to one of the caller's.
>
> They are NOT retired because nothing reads the header. Something does now —
> `services/bearer.py` parses it and `services/supabase_auth.py` verifies it —
> but what it reads is an IDENTITY, and identity is handled by §2a rather than
> here. Under the shipped `AUTH_MODE=open` even that does not run. What remains
> live below is everything from **Propagation** down, which describes the
> SERVER's keys.

| Stage | Behaviour | Code |
|---|---|---|
| ~~Extraction~~ *(retired)* | ~~`Authorization: Bearer <token>` as a PROVIDER key~~ — a bearer is an account credential now, never a model one | `auth.py` |
| ~~Structural validation~~ *(retired)* | ~~printable non-space ASCII, ≤ 4096 bytes~~ — the shape a bearer must have now lives in `services/bearer.py`, and it is stricter | `auth.py` |
| ~~In-process form~~ *(retired)* | ~~`SecretStr` on a frozen `Principal`~~ — `Principal` holds an identity and no credential of any kind | `auth.Principal` |
| Propagation | the SERVER's key, installed ONCE as the provider's default when the container is built — the serving path sends no per-call key, so `/respond` passes none. `ProviderChatRequest.api_key` stays live and tested as the field a per-account credential would occupy, and `_resolve_key` prefers it over the default when one IS present; benchmarks and live tests are its only callers today | `providers/base.py`, `main._build_provider` |
| Header construction | a fresh per-call header dict; the shared `httpx` client's own headers are never mutated, so concurrent calls carrying different keys cannot bleed into one another | `openrouter._headers` |
| Malformed value | rejected in `_resolve_key` **before** httpx sees it — h11's `LocalProtocolError` embeds the offending header value verbatim and httpcore logs it at DEBUG, so the permissive path would leak the key into logs | `openrouter._resolve_key` |
| Upstream error bodies | the credential is scrubbed out; if it is still recoverable after scrubbing (a doubly-encoded echo) the whole diagnostic is dropped | `openrouter._extract_upstream_error`, `_still_reveals` |

**The server never persists a key.** Not to disk, not to a cache, not to a
metrics label, not to a request id. It lives in memory for the lifetime of the
request that carried it, and is then dropped with the `Principal`.

The server's own key is different in kind: it is held for the process lifetime
by design, as a `SecretStr` on the immutable `Settings`. Everything below about
where a key must never appear applies to it identically — it is *more* sensitive
than a caller's was, since one leak exposes every turn Daintree runs rather than
one tester's account.

(There used to be a bounded exception here: runbook learning ran fire-and-forget
after the response, so the orchestrator captured the caller's `SecretStr` by
value into a detached task and it outlived the request. That feature has been
removed.)

## 3. Where a key must never appear

Enforced by review, by the redaction paths above, and by unit and integration
tests over those paths. (The live injection benchmark's canary is *not* a
credential canary — it measures instruction obedience, not key leakage. Do not
cite it as coverage for this list.)

- log lines (structured or otherwise) — `observability.py` carries no bearer keys
- exception messages and tracebacks
- request ids — a caller-supplied `X-Request-Id` that is, contains, or is a long
  fragment of the bearer is discarded and replaced with a server-minted id
- error envelopes returned to the client
- `/version`, `/readyz`, `/v1/daintree/capabilities` responses
- any diagnostic bundle
- benchmark result files

## 4. Verification

`POST /v1/daintree/auth/verify` is the only honest answer to "can this deployment
actually serve me?", and the key it asks about is **the SERVER's** —
`settings.serving_api_key`, the credential that funds every turn. There is no
caller key for it to be about: the structural check that used to stand behind this
section is retired (§2), because rejecting a malformed bearer only ever warned a
caller that the credential they meant to spend was not going to be spent.

The probe still earns its place for the same reason it always did, one owner
further along: without it, a serving credential that is expired or revoked first
surfaces on a tester's first real turn — as an upstream 401 that reads like their
fault rather than the deployment's. Note the boundary, because the two failures
are not the same: a credential never SET does not reach that state at all —
`missing_secrets()` reports it, the instance stays unready, and `require_ready`
answers 503 before any provider call is opened. Readiness checks PRESENCE; only
the probe checks that the value still works. The CLI calls it from
`doctor` (`cli/internal/backend/verify.go`), which is where an operator asks the
question before a user does.

The probe is `GET /models` against Together: the cheapest authenticated call
endpoint, which answers for the credential rather than for a model and runs no
inference. It has a short, hardcoded 10s read budget, deliberately *not*
inheriting the main model's budget — a user is watching a sign-in spinner. It is
also the one upstream call with **no retry wrapper**: a sign-in probe should
answer or fail, not stall behind a backoff.

**What it returns today** (`api/daintree_auth.py`): always HTTP 200 when an answer
was obtained, because "this key is invalid" is a successful *result* for this
question, not a request failure.

| Field | When | Meaning |
|---|---|---|
| `valid` | always | the provider recognised the key |
| `usable` | always | the key is **not known to be exhausted** — see the caveat below |
| `reason` | always | the stable machine-readable outcome: `ok`, `provider_rejected`, `credits_exhausted` |
| `detail` | always | human-readable verdict — prose, never branch on it |
| `label` | when the provider reports one | the provider's own name for the credential, so an operator can tell WHICH server key answered — nobody pastes a key any more, so this identifies a deployment's credential rather than confirming someone's typing. Dropped entirely if it contains the key — some providers default a label to the key itself |
| `limit`, `usage`, `limit_remaining` | when numeric | lets the client warn about an exhausted key, which otherwise reads as a broken sign-in |
| `is_free_tier` | when boolean | |

`valid` and `usable` answer **different questions**, and collapsing them was the
bug the split exists to fix: a recognised key with a spent balance is `valid:
true, usable: false, reason: "credits_exhausted"`, and a client reading only
`valid` shows a successful sign-in and then fails on the first real request with
what looks like an unrelated error.

**`usable: true` is not a promise that a turn can be funded.** It is the weaker,
honest claim that nothing we can see says otherwise: usability is judged
conservatively, and only a *positively reported* zero-or-negative
`limit_remaining` counts as exhausted. A provider that reports no limit at all —
unlimited, pay-as-you-go, or simply silent — yields `usable: true` on no evidence.
Stating it the other way round would make the field a guarantee it cannot keep,
and a sign-in that promised funding and then failed is exactly the confusion the
split exists to prevent.

A client must decode `usable` as **optional**, not as a plain boolean: a backend
predating the field omits it entirely, and a plain `bool` decodes that absence as
`false` — declaring every key on every older deployment unusable. Absent means
"not reported", and the fallback is `limit_remaining`. The CLI does exactly this —
`Usable *bool`, so an omitted field stays distinguishable from a reported `false`.

Failure to *reach* the provider propagates as the usual upstream envelope — 503
`upstream_unavailable` for an unreachable provider, 504 `upstream_timeout` for a
probe that ran out of time, 502 `upstream_protocol_error` for a 200 whose body is
not a key record. Then we genuinely do not know, and must not imply a verdict.

## 5. Client-side storage (the CLI)

**Exactly one secret, and it is not an API key.** The CLI persists the OAuth
**refresh token** and nothing else. The access token stays in process memory and is
never written anywhere: it is short-lived and re-derivable, so persisting it would
add a second place to leak from in exchange for saving one network round trip an
hour.

This section said "there is none" until account authentication landed, and that was
true of the flow it described. `internal/credentials/`, `internal/cli/login.go` and
`internal/app/signin.go` are still gone, along with the `login`/`logout` commands
that prompted for a key and the per-user credential file they maintained. What came
back is a different thing with different terms, and reading the old sentence as
still current is the mistake this paragraph exists to prevent.

**Where it lives, and what happens when it cannot.** The refresh token goes to the
operating system's credential service (`internal/auth/keyring.go`, reached through
`internal/auth/store.go`). The rule there is **no silent plaintext**: when no
credential service is available the store degrades to `MemoryStore` — the session
lives in that process only and evaporates on exit — and the user is told the tier
changed. It never falls back to a file. A store that quietly wrote one would be
worse than a store that refused, because the user would be told they are signed in
and never told the terms had changed.

Signing out deletes it (`Manager.Logout`), and a rotation replaces it. So "delete
this to sign out" now has a real answer again, and it is `daintree auth logout`
rather than a path.

**The legacy escape hatch is unchanged.** `cfg.APIKey` is read only from
`DAINTREE_API_KEY`, is empty on virtually every install, and is never persisted. When
it IS set it takes precedence over the account credential — `NewAccountTokenSource`
returns nothing rather than silently overriding a key the user explicitly exported —
and `redact.RegisterSecret` registers it during app construction before anything can
log, unconditionally, so the rare install that exports it is covered without a second
code path.

**One older claim here is now wrong and worth naming.** This section used to say an
empty `DAINTREE_API_KEY` means no `Authorization` header at all. That described a
client holding a frozen `apiKey string`; the client now resolves a `TokenSource` once
at construction, and an empty key is precisely the condition under which it builds the
live account source. On a signed-in install an unset variable therefore sends a bearer
token. What still holds is narrower: a source with nothing to give yields an empty
token and `applyHeaders` sends no header for one, and the paths a client must reach
before it holds any credential — the probes and `GET /v1/daintree/auth/config` — are on
a closed allowlist that bypasses credential lookup entirely
(`internal/backend/tokensource.go`, `publicPaths`).

Under `AUTH_MODE=open` the server ignores whatever arrives (§1, §2), so on today's
deployments none of this changes what happens to a request. Under `observe` it is
verified and recorded, and under `enforce` it is required.

## 6. Rotation

**Historical — the caller's OpenRouter key.** Rotate at OpenRouter, clear the stored
credential, sign in again. Nothing server-side needs to change: the backend keeps
no copy and no cache.

**The server's state-signing key** (`STATE_SIGNING_KEY_CURRENT`) is unrelated
material — it signs the opaque runbook-state token, not any caller credential.

It rotates through a current/previous pair, and `cloudbuild.yaml` supports the
overlap: it mounts `state-signing-key-previous` **when that secret exists**, and
both key ids are build substitutions (`_CURRENT_KID`, `_PREVIOUS_KID`) because
both change on a rotation. A hardcoded current kid would make the previous key
useless — the outgoing tokens name `state-prod`, the incoming key is also
`state-prod`, and `StateCodec` installs the current key first and `setdefault`s
the previous, so the old key is never selected. The deploy therefore **fails
closed** if the previous secret exists without a `_PREVIOUS_KID`, or if the two
ids are equal.

1. generate a new key and KID;
2. copy the outgoing key into a `state-signing-key-previous` secret, write the
   new one to `state-signing-key-current`, and re-run `infra/gcp/bootstrap.sh`
   so the runtime identity can read the new secret (it is idempotent);
3. deploy with
   `--substitutions=_CURRENT_KID=<new>,_PREVIOUS_KID=<old>`, then verify that
   tokens signed by the old key are still accepted and that newly issued tokens
   carry the new KID;
4. wait longer than `STATE_TOKEN_TTL_SECONDS` (default 7 days);
5. delete the `state-signing-key-previous` secret and deploy again — with the
   secret gone, the build drops both the mount and `_PREVIOUS_KID`;
6. verify a stale token is now rejected.

Untested against a real rotation. The pipeline logic is there and fails closed on
the mistakes that would silently break it, but nobody has run the procedure end
to end.

`infra/gcp/bootstrap.sh` deliberately **never rotates an existing secret
version** — doing so would invalidate every outstanding state token at once.

A well-sized but invalid, expired, unknown-key or wrong-session state token is
ignored, not an error: the turn simply re-runs selection. Rotation therefore
degrades to one extra selector call, never to a failed request. (An *oversized*
token is rejected as invalid input rather than ignored — but rotation never
produces one.)
