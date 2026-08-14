# Key handling

**Authoritative** for: what a Daintree API key *is*, where it lives, how it moves,
and everywhere it must never appear.

---

## 1. There is one key, and the tester owns it

For the internal beta a Daintree credential **is an OpenRouter key**. It plays two
roles at once and there is no second credential anywhere in the system:

1. it authenticates the request to the Daintree backend (HTTP bearer);
2. it is the upstream credential that funds that turn's model calls.

Consequences that follow directly, and that the code enforces:

- **The serving backend holds no model credential.** `main._build_provider` passes
  `api_key=None` deliberately, so a key-propagation bug fails loudly in
  `_resolve_key` instead of quietly billing a shared account. A unit test pins
  this.
- **`OPENROUTER_API_KEY` is benchmark and live-test tooling only.** The serving
  path never reads it. It is not a fallback and must never become one.
- **There is no unauthenticated mode in any environment.** Without a caller key
  there is no upstream credential to run the turn with, so the old development
  shortcut was removed rather than gated.
- **The tester pays.** Every token — main completion, selector, utility tasks —
  is billed to the key's own OpenRouter account.

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

One bounded exception, in local development only: when **skill learning** is
enabled, the post-turn learner runs fire-and-forget *after* the response has been
sent, so the request is already gone. The orchestrator therefore captures the
`SecretStr` by value into the detached task, and it survives until that task
finishes. Learning is off by default and fails `/readyz` in staging and
production, so this never applies to a deployed service — but it is a real
in-memory extension of the key's lifetime on a developer's machine.

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

The probe is `GET /key` against OpenRouter: the provider's own key-introspection
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
| `detail` | always | human-readable verdict |
| `label` | when the provider reports one | a safe echo so the user can confirm they pasted the key they meant to. Dropped entirely if it contains the bearer — some providers default a key's label to the key itself |
| `limit`, `usage`, `limit_remaining` | when numeric | lets the client warn about an exhausted key, which otherwise reads as a broken sign-in |
| `is_free_tier` | when boolean | |

Failure to *reach* the provider propagates as the usual upstream envelope (502
`upstream_error`, or 504 `upstream_timeout`) — then we genuinely do not know, and
must not imply a verdict.

**Not yet implemented:** a `usable` field distinct from `valid`. A valid key with
an exhausted balance is a different problem with a different fix, and today the
client has to infer that from `limit_remaining`. Tracked as part of the error
taxonomy work; until it lands, do not describe `usable` as available.

## 5. Client-side storage (the CLI)

Owned by the CLI repository; recorded here because it is part of the same
lifecycle.

- Global `~/.daintree/credentials.json`, written atomically as mode `0600`. The
  parent directory is created as `0700` **when it is absent**; an existing
  directory's mode is not currently corrected, so a pre-existing world-readable
  `~/.daintree` stays that way.
- **Endpoint pairing is fail-closed**: a key persisted against one backend
  endpoint is never sent to an environment-overridden URL. A custom endpoint gets
  no credential rather than the production-paired one.
- For this internal beta, a `0600` file is acceptable **provided it is
  disclosed**. Native OS keychain storage is a worthwhile later enhancement,
  especially because this key carries spend authority.

### Required lifecycle — ⏳ a release gate, not current behaviour

Today the CLI accepts a pasted token and persists it immediately: it has no caller
of `/v1/daintree/auth/verify`, no post-verification smoke, and no credential-clear
path. Everything below is what must be true before testers are invited, tracked in
the CLI repository. Do not describe it in the present tense anywhere.

1. On first launch, state plainly that the key is an **OpenRouter** key and that
   OpenRouter bills it.
2. Normalise and display the target backend endpoint *before* asking for the key.
3. **Verify the key before persisting it.** Login must not silently persist a
   rejected key.
4. Show the safe label and remaining limit when the provider reports them.
5. Run a tiny streaming smoke after verification.
6. Persist only after both checks pass — unless the tester explicitly chooses an
   offline/custom-endpoint setup.
7. Provide a clear/logout path that removes the key completely.
8. Provide rotation instructions.
9. No debug, audit, crash or doctor output may contain the raw key.

## 6. Rotation

**The caller's OpenRouter key.** Rotate at OpenRouter, clear the stored
credential, sign in again. Nothing server-side needs to change: the backend keeps
no copy and no cache.

**The server's state-signing key** (`STATE_SIGNING_KEY_CURRENT`) is unrelated
material — it signs the opaque skill-state token, not any caller credential.

> ⏳ **The application supports rotation; the deployment pipeline does not yet.**
> `StateCodec` accepts a current/previous key pair, but Cloud Build pins the KID
> and mounts only the current key and salt — there is no previous-key secret for
> it to mount. Executing the procedure below against the managed deployment
> therefore requires a corresponding pipeline change first.

It rotates through a current/previous pair:

1. generate a new key and KID;
2. move the outgoing key into `STATE_SIGNING_KEY_PREVIOUS` /
   `STATE_SIGNING_KID_PREVIOUS` and set the new one as current;
3. deploy, and verify that tokens signed by the old key are still accepted and
   that newly issued tokens carry the new KID;
4. wait longer than `STATE_TOKEN_TTL_SECONDS` (default 7 days);
5. remove the previous key and KID;
6. verify a stale token is now rejected.

`infra/gcp/bootstrap.sh` deliberately **never rotates an existing secret
version** — doing so would invalidate every outstanding state token at once.

A well-sized but invalid, expired, unknown-key or wrong-session state token is
ignored, not an error: the turn simply re-runs selection. Rotation therefore
degrades to one extra selector call, never to a failed request. (An *oversized*
token is rejected as invalid input rather than ignored — but rotation never
produces one.)
