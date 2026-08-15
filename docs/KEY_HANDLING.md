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

Owned by the CLI repository; recorded here because it is part of the same
lifecycle.

- `~/.daintree/assistant-cli/credentials.json`, written atomically as mode `0600`.
  The sign-in is **per-user, not per-project**: it lives at the CLI's state root
  rather than in the per-project subdirectory, so one login serves every project.
  (An explicit `DAINTREE_ASSISTANT_STATE_DIR` override moves it, which is how
  tests and benchmarks avoid reading or clobbering the real sign-in.)
- The parent directory is created `0700` — but `MkdirAll`'s mode applies only to
  directories it **creates**, so a pre-existing state root (an older build, an
  installer, a restored backup, a permissive umask) keeps whatever mode it already
  had. The `0700` is a promise about new installs.
- `Save` therefore repairs it, narrowly: `tightenDirMode` clears the group and
  world bits before writing — but only on a directory the CLI owns (an
  `assistant-cli` state root). A custom directory supplied through
  `DAINTREE_ASSISTANT_STATE_DIR` is left as the user configured it, since chmod-ing
  an arbitrary path the user chose is not ours to do. Either way, a directory that
  is group- or world-WRITABLE refuses the save outright: that is a different
  problem, and no chmod makes it safe.
- Detection covers the file: `doctor` re-checks the credential file's own mode and
  **fails** — not warns — on anything wider than `0600`, because the CLI writes it
  `0600` by construction, so a wider mode means something else set it and the file
  spends the user's money. Doctor's *directory* check runs against the resolved
  state dir, which under the normal project-scoped configuration is the per-project
  subdirectory rather than the parent root the sign-in lives in — so the repair
  above, not that check, is what covers the credential's own directory.
- **The stored key is NOT bound to the stored endpoint.** `DAINTREE_BACKEND_URL`
  resolves ahead of the persisted URL while the persisted key still applies, so a
  process-level override sends the caller's own provider credential to whatever
  host it names. That is deliberate — the key is valid against a local backend as
  well as the deployed one, and binding it would break the sign-in-once dev loop —
  but it is a fail-OPEN default and must be described as one. What the pairing
  does protect against is a *bound project's* `.env`: neither the key nor the URL
  is read from untrusted project configuration.
- For this internal beta, a `0600` file is acceptable **provided it is
  disclosed**. Native OS keychain storage is a worthwhile later enhancement,
  especially because this key carries spend authority.

### Required lifecycle

What must be true before testers are invited. Implemented in the CLI repository.

Statuses are `✅` done, `◐` partly, `⏳` not yet. They describe the **`cli/`
submodule at its current pin**, not the CLI's `develop` tip: the pin does not
float, so it trails, and a row that has landed upstream but not here stays ⏳ until
the pin moves.

`tests/unit/test_docs_drift.py` pins the rows whose evidence is a symbol in the
vendored source. Two limits worth knowing: symbol presence cannot establish
semantics (so the one ordering that matters — verify before persist — is checked
positionally instead), and CI does not fetch the submodule, so those checks are a
LOCAL gate that skips in CI rather than a green-build guarantee.

| | Requirement | Status |
|---|---|---|
| 1 | On first launch, state plainly that the key is an **OpenRouter** key and that OpenRouter bills it. | ✅ `login.go` names the provider and prints `backend.KeyPurposeNotice` before the key prompt |
| 2 | Normalise and display the target backend endpoint *before* asking for the key. | ✅ the RESOLVED endpoint is restated ("Signing in to …") ahead of `promptAPIKey` — a custom URL is normalised on the way through, so what was typed is not necessarily the host the key is about to reach |
| 3 | **Verify the key before persisting it.** Login must not silently persist a rejected key. | ✅ `App.SignIn` runs `backend.CheckSignIn` against a THROWAWAY client, then persists, then swaps the live client |
| 4 | Show the safe label and remaining limit when the provider reports them. | ◐ the startup login path does, on the no-warning branch; the in-session `App.SignIn` discards the verification object and shows only a generic warning |
| 5 | Run a tiny streaming smoke after verification. | ⏳ verification is a tokenless key probe, so it proves the credential without proving a turn can stream |
| 6 | Persist only after the checks pass — unless the tester explicitly chooses an offline/custom-endpoint setup. | ◐ a DEFINITE rejection is not persisted; an INCONCLUSIVE verification (provider unreachable, or a backend not serving the route on loopback) persists with a warning, deliberately — neither is evidence about the key, and refusing would leave no way to configure the CLI |
| 7 | Provide a clear/logout path that removes the stored key. | ✅ `logout` deletes the file and names the path it removed. Note it cannot clear a key supplied through `DAINTREE_API_KEY`, which keeps applying |
| 8 | Provide rotation instructions. | ⏳ §6 below is the content; nothing surfaces it in the CLI |
| 9 | No debug, audit, crash or doctor output may contain the raw key. | ✅ `redact.RegisterSecret` is called on the new key before it is persisted. Note it is registered *after* the verification call that first uses it, so redaction covers storage and every later use, not the probe |

## 6. Rotation

**The caller's OpenRouter key.** Rotate at OpenRouter, clear the stored
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
