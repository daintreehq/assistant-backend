# Key handling

**Authoritative** for: what a Daintree API key *is*, where it lives, how it moves,
and everywhere it must never appear.

---

## 1. There is one key, and the SERVER owns it

**This inverted when callers stopped supplying keys**, and it inverted in the direction
that costs money if a reader remembers the old rule. It used to be that a
Daintree credential *was* the caller's own OpenRouter key, playing two roles at
once: authenticating the request, and funding that turn's model calls. The caller
paid.

Now there is exactly one credential — `TOGETHER_API_KEY`, named by
`config.SERVING_KEY_ENV` — it belongs to the server, and **Daintree pays**. Consequences that follow directly, and that the
code enforces:

- **The serving backend holds the model credential.** `main._build_provider`
  passes `settings.serving_api_key`, and `missing_secrets()` gates `/readyz` on
  its presence — without it nothing can be answered at all, and discovering that
  on the first turn surfaces as an upstream 401 that reads like the caller's
  fault. Unit tests pin both halves.
- **The `Authorization` header is ignored entirely.** Not parsed, not validated,
  not rejected. `auth.authenticate` returns the same anonymous principal whatever
  arrives, and `Principal` carries no credential field at all.

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
- **This is a stage, not a destination.** Account authentication and sign-in are
  being built next, and `auth.py` is the seam they land in: `authenticate` starts
  reading the header again, validating it against an account store rather than
  against its own shape, and the rest of the server does not move.

## 2. Server-side handling

| Stage | Behaviour | Code |
|---|---|---|
| Extraction | `Authorization: Bearer <token>`; raw header length-bounded *before* any scanning | `auth._extract_bearer` |
| Structural validation | printable non-space ASCII (0x21–0x7E) only, ≤ 4096 bytes. **No prefix check** — the issuer is not our business, only that the value can ride an HTTP header safely | `auth._structurally_valid` |
| In-process form | `pydantic.SecretStr` on a frozen `Principal`, `repr=False` | `auth.Principal` |
| Propagation | threaded explicitly as `ProviderChatRequest.api_key` to every provider call | `providers/base.py` |
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

(There used to be a bounded exception here: skill learning ran fire-and-forget
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

`POST /v1/daintree/auth/verify` is the only honest answer to "is my key usable?".
Daintree's own auth is **structural** — a well-formed string that no provider ever
issued passes it — so without this probe a bad key first surfaces at the first
real turn, long after the user thought they had signed in.

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
| `label` | when the provider reports one | a safe echo so the user can confirm they pasted the key they meant to. Dropped entirely if it contains the bearer — some providers default a key's label to the key itself |
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

**There is none, and that is the current design.** The CLI dropped the API-key
requirement outright: `internal/credentials/`, `internal/cli/login.go` and
`internal/app/signin.go` are gone from the vendored source, along with the
`login`/`logout` commands and the per-user credential file they maintained under
the CLI's state root. Nothing is persisted, so there is nothing to chmod, nothing
for `doctor` to inspect, and nothing to delete to sign out.

What remains is a single escape hatch. `cfg.APIKey` is read only from
`DAINTREE_API_KEY` and is normally empty; an empty key sends **no**
`Authorization` header at all (`internal/backend/client.go`, `setHeaders`). When
it is set, `redact.RegisterSecret` registers it during app construction before
anything can log — unconditionally, so the rare install that exports it is
covered without a second code path.

The server ignores the header regardless (§1, §2), so the env var changes what
goes over the wire and nothing about what happens to the request. It is a
transitional affordance, not a credential mechanism.

The lifecycle table that used to live here described the interactive sign-in
flow: prompt, verify before persist, show the endpoint, offer logout, redact the
key. Every row of it is moot now that no key is collected. It was deleted rather
than marked historical, because a status table nobody can act on is exactly the
inventory-of-the-past this document was rewritten to stop being. Git history has
it. When account authentication lands (`TODO.md`), the requirements get written
fresh against whatever that flow actually is.

## 6. Rotation

**Historical — the caller's OpenRouter key.** Rotate at OpenRouter, clear the stored
credential, sign in again. Nothing server-side needs to change: the backend keeps
no copy and no cache.

**The server's state-signing key** (`STATE_SIGNING_KEY_CURRENT`) is unrelated
material — it signs the opaque skill-state token, not any caller credential.

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
