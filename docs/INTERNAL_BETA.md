# Daintree Assistant — internal beta contract

**Authoritative** for what the internal beta *is*. Where code, client copy or
another document disagrees with this file, this file is right and the other one is
a bug. Companion authorities: [`PROVIDER_POLICY.md`](PROVIDER_POLICY.md) (model,
routing, privacy, retries, errors), [`KEY_HANDLING.md`](KEY_HANDLING.md)
(credential lifecycle), [`DAINTREE_API.md`](DAINTREE_API.md) (the wire contract).

---

## 1. The contract in one paragraph

A tester pastes their own OpenRouter key. Daintree verifies it before the first
turn. The key never leaves the request path except to OpenRouter. The backend
responds quickly and predictably, common Daintree workflows are covered by
server-owned skills, failures say whose problem they are, and a bad revision can
be rolled back.

## 2. What is in the beta

This is the **release contract** — what must be true when testers are invited. It
is not a description of today's build, so every line carries a status:

- ✅ **holds now**, enforced in code
- ⏳ **required, not yet implemented** — a release gate, and *not* something to
  describe in the present tense anywhere else

| | Requirement | Status |
|---|---|---|
| 1 | **OpenRouter is the sole provider.** | ✅ |
| 2 | **The tester supplies an OpenRouter key**, and it is theirs. | ✅ |
| 3 | **That key is the HTTP bearer** and funds every upstream call the turn makes. | ✅ |
| 4 | **The serving backend holds no shared model credential** — not as a fallback, not as a default, not for "just the selector". | ✅ `main._build_provider(api_key=None)` |
| 5 | **The primary model stays date-pinned.** | ✅ `config.OPENROUTER_MODEL` |
| 6 | **The backend owns** prompts, the skill catalog and bodies, automatic skill selection, prompt assembly, and the utility-task prompts. | ✅ |
| 7 | **The CLI owns** the visible conversation, local state, permissions and tool execution. | ✅ |
| 8 | **The backend is stateless across instances**, apart from process-local caches; skill state travels in a signed, opaque token. | ✅ `state/codec.py` |
| 9 | **Skill learning is off in staging and production** — an enabled mode fails `/readyz`. | ✅ `Settings.local_only_problems` |
| 10 | **Tester prompt and tool content is never logged.** | ✅ with one exception — see §5 |
| 11 | **The tester can verify their key before the first model call.** | ✅ `/auth/verify` reports `usable` and a stable `reason` separately from `valid`, and `App.SignIn` calls it against a throwaway client BEFORE persisting, so a key the provider REJECTS never reaches disk. An inconclusive probe (provider unreachable) persists with a warning — deliberately: that is not evidence about the key |
| 12 | **Failures identify their owner**: Daintree, OpenRouter, account credits, key permissions, rate limiting, provider routing, or malformed input. | ✅ distinct codes — see [`DAINTREE_API.md`](DAINTREE_API.md#errors) |
| 13 | **A release can be rolled back** to the immediately previous Cloud Run revision. | ◐ `infra/gcp/rollback.sh` exists (`--dry-run`, `--list`, `--to <revision>`), refuses every case where guessing would be worse than stopping, and fails closed unless it can confirm the target is serving. Its selection logic is tested offline against a fake `gcloud`; **moving real traffic has never been rehearsed**, so this is not ✅ until a drill has been run |

## 3. What is explicitly *not* in the beta

Not blockers. Do not delay the beta for any of them:

- Daintree-owned login accounts
- Daintree-issued API keys or subscription keys
- shared or brokered OpenRouter credentials
- billing metering, invoices, spend ledgers
- tenant databases, team administration, admin console
- model switching or provider switching in the client
- user-created server-side skills
- production skill learning
- long-term server-side conversation storage
- a public third-party developer API

These belong *after* the OpenRouter-key-only beta has proved the assistant
experience.

## 4. Deliberately not defended

The backend is a **pass-through for the caller's own OpenRouter key**. It holds no
credential worth stealing and funds no spend of its own, so it deliberately has
**no admission control**: no per-key rate limits, no per-key in-flight caps, no
per-IP throttling, no verified-key gate in front of prompt assembly. (Cloud Run
still caps each instance at its configured concurrency and the service at its
maximum instance count — that is platform capacity, not Daintree policy, and it
is not per-caller.)

That is a decision, not an oversight. Model spend is caller-funded and therefore
self-limiting, and the URL is not advertised. Be accurate about what is still
consumable, though: not just CPU but memory (the outer middleware buffers a whole
body — up to 8 MiB — before authentication runs), concurrency slots, connections,
log volume, and therefore availability for real testers. Revisit it if — and only if — the beta produces evidence
of abuse or capacity starvation. Anything built then belongs at the edge (Cloud
Armor, an allowlist) before it belongs in application code.

## 5. Required tester disclosure

Onboarding must state all of this, plainly, before the tester hands over a key:

- The key is **theirs** and every token is billed to **their** OpenRouter account
  — main completion, skill selector and utility tasks alike.
- The exact privacy routing mode in force. Today: requests route only to endpoints
  that **do not collect or train on** request data.
  **Zero data retention is not enforced** <!-- privacy-copy-ok -->  — see
  [`PROVIDER_POLICY.md` §4](PROVIDER_POLICY.md#4-privacy--what-is-and-is-not-promised).
  Never say "does not store". <!-- privacy-copy-ok -->
- Where the CLI stores the key — by default
  `~/.daintree/assistant-cli/credentials.json`, per-user so one login serves every
  project, moved by an explicit `DAINTREE_ASSISTANT_STATE_DIR`. The file is `0600`;
  the directory is `0700` **when the CLI creates it**, and a pre-existing root
  keeps whatever mode it had (`doctor` reports the file's mode, not that one). And
  how to remove or rotate it.
- That Daintree logs **metadata but not prompt or tool content**, with one
  exception: when OpenRouter rejects a request, a bounded excerpt of *its* error
  body is logged and may appear in the error message, and a provider is free to
  quote part of the offending request back at us. The caller's credential is
  scrubbed from that excerpt (and the excerpt dropped entirely if the credential
  survives scrubbing), but request content is not.
- That this is an internal beta, and that failures should be reported with the
  **request ID** and **build SHA**.

## 6. Invariants that must survive every change

These are load-bearing. Breaking one is a release blocker, not a regression.

- Client `system` / `developer` messages are **rejected**. The server owns all
  system text.
- `skill.find` / `skill.load` are never *offered* to the main model. Selection is
  automatic and server-side; the reserved-tool-names check enforces it at the
  request boundary.
- Only **active skill bodies** are injected — never the full catalog.
- **Selector failure fails open** — a provider error or unparseable output
  degrades to the prior active set rather than blocking the user. This covers the
  failure modes that actually happen in production; an unexpected *programming*
  exception still fails the request, deliberately, because silently degrading a
  bug is how a bug survives.
- Skill state is **portable** via the signed token. A well-sized but invalid,
  expired, unknown-key or wrong-session token is **ignored** and selection simply
  re-runs — it is not an error. (An *oversized* token is different: it is rejected
  as invalid input, by contract validation or the codec's 16 KiB bound.)
- Required structured `startup` context reaches the **main model only** — neither
  the selector nor the learner ever sees it. What they *do* see:
  - **selector**: sanitized `input.messages`, plus bounded turn/workflow hints and
    the available tool names;
  - **learner**: `input.messages`, the just-generated final answer (it learns from
    the workflow it actually completed), and the available tool names.
- The serving provider is built with **no default credential**.
- Prompt order is cache-critical: stable systems and tools → stable startup user
  block → visible conversation → one volatile runtime/display/turn user tail. Never
  add a volatile system message — that includes the terminal's own width, which is
  why the geometry rides the tail and the response contract states no number (#38).

## 7. Environments

Four, and only these four (`config.Settings.environment` is a closed `Literal`, so
a typo like `prod` cannot fail open past the hardening gates):

| Environment | Skill learning | Weak-secret gate | Purpose |
|---|---|---|---|
| `development` | allowed | off | local work; optional request dumping |
| `test` | allowed | off | offline fakes, deterministic tests |
| `staging` | **fails readiness** | on | real OpenRouter smoke, no external testers |
| `production` | **fails readiness** | on | the tester endpoint |

Authentication does **not** vary by environment: every protected
`/v1/daintree/*` route requires a caller key in all four. The operational
endpoints — `/`, `/health`, `/healthz`, `/readyz`, `/version` — are deliberately
unauthenticated everywhere, because a probe that needs a credential is not a
health check.

## 8. Known limitations to publish with the beta

State these to testers up front rather than letting them discover them:

- Zero data retention is not enforced (§5). <!-- privacy-copy-ok -->
- The request prompt is large — tool schemas dominate it — so a turn costs more of
  the tester's OpenRouter balance than the visible conversation suggests.
- There is no rate limiting; a runaway client burns the tester's own balance.
- Skill learning never runs against the deployed service, so the catalog does not
  improve from beta usage.
- Images: remote image URLs are disabled by default and in the current deployment
  (`ALLOW_REMOTE_IMAGE_URLS=false`, reported in `/v1/daintree/capabilities`).
  Nothing prevents a hardened environment from setting it, so read capabilities
  rather than assuming.
- The CLI still replays a deterministic `upstream_error` when it arrives as a
  terminal SSE event rather than an HTTP one, so a doomed turn can retry a few
  times before giving up. Tracked in the CLI repository.

## 9. Reporting a problem

Every response carries `X-Request-Id`, and `/version` reports the build SHA. A
useful report is: what you asked, what happened, the request ID, and the build
SHA. Prompt and tool content is not logged server-side, so without the request ID
there is very little to correlate against.

Two details about that id:

- A client may supply its own `X-Request-Id` and it will be adopted and echoed —
  but only if it is short, in a conservative charset, and not the caller's own
  bearer token. Anything else is silently replaced with a server-minted id, so a
  key pasted into the wrong variable never reaches a log line.
- No endpoint takes a request id in its BODY. `/tasks` used to, and it won the
  response header while the logs kept the middleware's — so quote the
  **response header**, which is the only correlation id the server knows about.
  It is also on the SSE `error` event, because a stream can fail before `meta`.
