# Daintree Assistant — internal beta contract

**Authoritative** for what the internal beta *is*. Where code, client copy or
another document disagrees with this file, this file is right and the other one is
a bug. Companion authorities: [`PROVIDER_POLICY.md`](PROVIDER_POLICY.md) (model,
routing, privacy, retries, errors), [`KEY_HANDLING.md`](KEY_HANDLING.md)
(credential lifecycle), [`DAINTREE_API.md`](DAINTREE_API.md) (the wire contract).

---

## 1. The contract in one paragraph

A tester needs no key of their own — the server holds the upstream credential and
funds every turn (this replaced the paste-your-own-OpenRouter-key contract; see the
superseded rows below). The backend
responds quickly and predictably, common Daintree workflows are covered by
server-owned runbooks, failures say whose problem they are, and a bad revision can
be rolled back.

## 2. What is in the beta

This is the **release contract** — what must be true when testers are invited. It
is not a description of today's build, so every line carries a status:

- ✅ **holds now**, enforced in code
- ⏳ **required, not yet implemented** — a release gate, and *not* something to
  describe in the present tense anywhere else

> **Rows 1-4 were SUPERSEDED and are kept as history.** They describe the
> caller-funded credential model this service no longer has: the server now holds
> the upstream key and funds every turn, and callers send none (`auth.py` ignores
> the `Authorization` header entirely). Re-decide what the beta contract should say
> about credentials before inviting testers; do not read these four rows as current.

| | Requirement | Status |
|---|---|---|
| 1 | ~~**OpenRouter is the sole provider.**~~ *(superseded — the serving host is `config.SERVING_HOST`)* | — |
| 2 | ~~**The tester supplies an OpenRouter key**, and it is theirs.~~ *(superseded — no caller key exists)* | — |
| 3 | ~~**That key is the HTTP bearer** and funds every upstream call the turn makes.~~ *(superseded — the server's key funds every call)* | — |
| 4 | **The serving backend holds no shared model credential** — not as a fallback, not as a default, not for "just the selector". | ✅ `main._build_provider(api_key=None)` |
| 5 | **The primary model stays pinned**, and is not a floating alias. | ✅ `config.SERVING_MODEL` |
| 6 | **The backend owns** prompts, the runbook catalog and bodies, automatic runbook selection, prompt assembly, and the utility-task prompts. | ✅ |
| 7 | **The CLI owns** the visible conversation, local state, permissions and tool execution. | ✅ |
| 8 | **The backend is stateless across instances**, apart from process-local caches; runbook state travels in a signed, opaque token. | ✅ `state/codec.py` |
| 9 | **Prompt dumping is off in staging and production** — it writes full prompts to disk, and an enabled dir fails `/readyz`. (Runbook learning was the other entry here; it has been removed entirely.) | ✅ `Settings.local_only_problems` |
| 10 | **Tester prompt and tool content is never logged.** | ✅ with one exception — see §5 |
| 11 | **A caller can confirm the backend can actually serve before the first model call.** | ✅ `/auth/verify` probes the credential this request would spend — the caller's if they sent one, the SERVER's otherwise, which is every request today — with a tokenless model listing, and reports `usable` and a stable `reason` separately from `valid`. An inconclusive probe (provider unreachable) is reported as unknown, never as invalid |
| 12 | **Failures identify their owner**: Daintree, the upstream provider, account credits, key permissions, rate limiting, or malformed input. | ✅ distinct codes — see [`DAINTREE_API.md`](DAINTREE_API.md#errors) |
| 13 | **A release can be rolled back** to the immediately previous Cloud Run revision. | ◐ `infra/gcp/rollback.sh` exists (`--dry-run`, `--list`, `--to <revision>`), refuses every case where guessing would be worse than stopping, and fails closed unless it can confirm the target is serving. Its selection logic is tested offline against a fake `gcloud`; **moving real traffic has never been rehearsed**, so this is not ✅ until a drill has been run |

## 3. What is explicitly *not* in the beta

Not blockers. Do not delay the beta for any of them:

- Daintree-owned login accounts
- Daintree-issued API keys or subscription keys
- billing metering, invoices, spend ledgers
- tenant databases, team administration, admin console
- model switching or provider switching in the client
- user-created server-side runbooks
- long-term server-side conversation storage
- a public third-party developer API

These belong *after* the OpenRouter-key-only beta has proved the assistant
experience.

## 4. Deliberately not defended

This section was written when the backend was a **pass-through for the caller's own
key** — holding no credential worth stealing and funding no spend of its own, which
is what justified having **no admission control**. That premise is GONE: the server
now holds the credential and pays for every turn, so an open door is an open door
onto Daintree's own spend. Re-decide this before testers are invited. What follows
is the original reasoning, retained for that decision: no per-key rate limits, no per-key in-flight caps, no
per-IP throttling, no verified-key gate in front of prompt assembly. (Cloud Run
still caps each instance at its configured concurrency and the service at its
maximum instance count — that is platform capacity, not Daintree policy, and it
is not per-caller.)

That was a decision when it was made, and **its premise has since inverted.** It
rested on model spend being caller-funded and therefore self-limiting: each
tester's key paid for each tester's turns, so the worst an abusive caller could
do was exhaust their own balance. The credential moved to the server. Every turn
is now billed to Daintree, the `Authorization` header is ignored entirely, and
the application carries no per-caller, per-IP or global rate limit — so an
unadvertised URL is the only thing between a discovered endpoint and unbounded
spend on our account.

Be accurate about what is consumable: model spend first, and then CPU, memory
(the outer middleware buffers a whole body — up to 8 MiB — before any route
dependency runs), concurrency slots, connections, log volume, and therefore
availability for real testers.

What follows is a **sequencing** point rather than a reversal. Application-level
rate limiting is still the wrong layer, and account authentication is still the
real answer. But "self-limiting" is no longer a reason to wait for it, so the
interim control belongs at the edge and at the provider: restricted ingress or
IAM on the Cloud Run service, and a hard spend cap plus alerting on the Together
account. A billing ceiling is not something application correctness can
substitute for.

## 5. Required tester disclosure

Onboarding must state all of this, plainly, before the tester hands over a key:

- Tokens are billed to **Daintree**, not to the tester — main completion, runbook
  selection and utility tasks alike. There is no key to hand over and none to
  protect. (This bullet used to say the opposite, and the reversal is the single
  most important thing on this page to get right: onboarding copy written for the
  old model tells a tester they are paying for their own usage.)
- **What is promised about their data, and what is not.** Requests go to Together
  AI. Nobody has yet checked Together's published terms for the account tier we
  actually run on, so make no data-handling promise at all — not retention, not
  training, not deletion — until
  [`PROVIDER_POLICY.md` §4](PROVIDER_POLICY.md#4-privacy--what-is-and-is-not-promised)
  stops declaring itself unverified. An unchecked assurance is worse than
  silence: silence a tester can weigh, and a promise they cannot. Never say
  "does not store". <!-- privacy-copy-ok -->
- **A second outbound flow.** On a documentation question the server sends a
  cleaned version of the tester's question to Daintree's own public docs MCP
  (`daintree.org`). It is first-party and the query is not logged, but it is a
  separate destination from Together and belongs in any honest data-flow
  statement.
- That the CLI stores **no key at all** — the sign-in flow and the credential
  file it wrote were removed, so there is nothing on disk to protect, rotate or
  delete. `DAINTREE_API_KEY` is the only way to set one, it is normally unset, and
  a value that IS set is ignored by the server rather than forwarded. See
  [`KEY_HANDLING.md` §5](KEY_HANDLING.md#5-client-side-storage-the-cli).
- That Daintree logs **metadata but not prompt or tool content**, with one
  exception: when the upstream rejects a request, a bounded excerpt of *its* error
  body is logged and may appear in the error message, and a host is free to quote
  part of the offending request back at us. The credential is scrubbed from that
  excerpt (and the excerpt dropped entirely if it survives scrubbing), but request
  content is not.
- That this is an internal beta, and that failures should be reported with the
  **request ID** and **build SHA**.

## 6. Invariants that must survive every change

These are load-bearing. Breaking one is a release blocker, not a regression.

- Client `system` / `developer` messages are **rejected**. The server owns all
  system text.
- `runbook.find` / `runbook.load` are never *offered* to the main model. Selection is
  automatic and server-side; the reserved-tool-names check enforces it at the
  request boundary.
- Only **active runbook bodies** are injected — never the full catalog.
- **Selector failure fails open** — a provider error or unparseable output
  degrades to the prior active set rather than blocking the user. This covers the
  failure modes that actually happen in production; an unexpected *programming*
  exception still fails the request, deliberately, because silently degrading a
  bug is how a bug survives.
- Runbook state is **portable** via the signed token. A well-sized but invalid,
  expired, unknown-key or wrong-session token is **ignored** and selection simply
  re-runs — it is not an error. (An *oversized* token is different: it is rejected
  as invalid input, by contract validation or the codec's 16 KiB bound.)
- Required structured `startup` context reaches the **main model only** — the
  selector never sees it. What it *does* see: sanitized `input.messages`, plus
  bounded turn/workflow hints and the available tool names.
- The serving provider is built **with the server's own credential**
  (the credential `config.SERVING_KEY_ENV` names), and `/readyz` gates on its presence.
- Prompt order is cache-critical. The base prompt and the response contract are the
  **only** system messages; everything else the server injects sits below the tool
  schemas as a framed **user-role** row, ordered by how widely it can be shared,
  widest first: runbook bodies → integrations → the stable startup block → visible
  conversation → one volatile runtime/display/turn tail. Keeping the integrations
  row and the runbook bodies out of the system region is what stops a per-machine MCP
  endpoint URL, or a selector topic shift, from re-encoding all 21.5k tokens of tool
  schemas (#39). Never add a volatile system message — that includes the terminal's
  own width, which is why the geometry rides the tail and the response contract
  states no number (#38).

## 7. Environments

Four, and only these four (`config.Settings.environment` is a closed `Literal`, so
a typo like `prod` cannot fail open past the hardening gates):

| Environment | Prompt dumping | Weak-secret gate | Purpose |
|---|---|---|---|
| `development` | allowed | off | local work |
| `test` | allowed | off | offline fakes, deterministic tests |
| `staging` | **fails readiness** | on | real upstream smoke, no external testers |
| `production` | **fails readiness** | on | the tester endpoint |

Authentication does **not** vary by environment, and currently that means the
door is open in all four: the server holds the upstream credential, so every
request has one to spend. The `Authorization` header is **ignored entirely** —
not parsed, not validated, not rejected — so a client still sending an old
OpenRouter key is served rather than having that key forwarded upstream and
rejected as theirs. Account authentication is being built next. The operational endpoints — `/`, `/health`, `/healthz`,
`/readyz`, `/version` — have always been unauthenticated, because a probe that
needs a credential is not a health check.

## 8. Known limitations to publish with the beta

State these to testers up front rather than letting them discover them:

- Zero data retention is not enforced (§5). <!-- privacy-copy-ok -->
- The request prompt is large — tool schemas dominate it — so a turn costs more
  than the visible conversation suggests. That is now **Daintree's** spend, not
  the tester's: the server holds the upstream credential.
- There is no rate limiting and no inbound authentication, so a runaway client
  burns Daintree's own balance. This is the most important consequence of the
  credential move and the reason account auth is next.
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
