# Daintree production audit — 5 September 2026

## Assessment

The architecture is substantially more mature than a typical model wrapper: the CLI owns credentials and durable execution, the backend owns stateless reasoning, and the desktop owns local authority. Keep that separation. Assuming the serving model is capable, the highest returns come from reliable intent routing, bounded dependencies, explicit authorization, durable progress, and evidence that an action succeeded.

The patches accompanying this audit close reproducible failures in all three projects. They do **not** establish that a public paid launch is ready. Session revocation, globally enforced usage allowances, and live provider/product acceptance still need evidence and, in the first two cases, infrastructure or commercial decisions. Do not interpret a green unit suite as proof of those properties.

## Scope and evidence

Audited source bases: `daintree` at `07b356b4d`, `assistant` at `17f85c6`, and `assistant-backend` at `b5288f8`. Work targets `develop`. Other existing worktrees, running applications, personal credentials, and deployed infrastructure were not changed.

This was a risk-focused review across all three components, with full automated suites and targeted failure injection, rather than a claim that every source line or every live deployment was exhaustively reviewed. Areas examined:

| Boundary | Source and checks |
| --- | --- |
| Desktop isolation and authorization | `electron/setup/security.ts`, window factories, `HelpSessionService`, MCP HTTP lifecycle, session store, tier authorization, grants; desktop unit suite and type/codegen/lint checks |
| CLI identity and transport | `internal/auth` discovery, PKCE callback, token exchange, credential storage, revision/generation handling and refresh locking; backend TLS/redirect policy, retries and SSE bounds; full Go tests, vet and race detector |
| Durable execution | SQLite ownership and busy-timeout handling, timer/watcher/async delivery contracts, actor-specific grants, tool dispatch and subagent restrictions; existing Go failure/race tests |
| Backend identity and availability | Auth/entitlement dependencies, JWKS cache and verifier, readiness configuration, request buffering, provider retry/failover boundaries, trace lifecycle |
| Intelligence | Local selector benchmark, catalog/tool projection, runbook prompt budgets, signed continuation state, utility validation, context-budget and compaction paths |
| Release and supply chain | Cloud Build posture/traffic gates, CI definitions, pinned dependency audits, isolated installation of updated dependencies |

Baselines before fixes: Go tests passed; Python had **4,399 passed, 1 failed, 1 skipped**, with the failure caused by an oversized runbook; desktop had **56,298 passed, 1 failed, 7 skipped, 1 todo**, with the failure in the real-filesystem `ProjectPluginWatcher` test. The desktop result is recorded as a baseline failure, not attributed to the MCP patch.

The [desktop PR](https://github.com/daintreehq/daintree/pull/12254) passed **56,305 tests**, with 7 skipped and 1 todo, against an isolated installation of the updated lockfile; static checks and a production build also passed. The [CLI PR](https://github.com/daintreehq/assistant/pull/377) passed the full race-enabled Go suite, vet, and the vulnerability scan; its GitHub checks passed on macOS and Ubuntu. The backend's six deterministic dashboard checks, catalog routing-coverage gate, Ruff, and strict Mypy passed. Final backend suite and CI results are recorded in the accompanying backend PR.

## Fixed in the accompanying changes

| Finding | Change and evidence |
| --- | --- |
| A failed MCP authentication attempt could revoke another session by claiming its id | Unauthenticated failures now affect global telemetry only. Session ids are routing handles, not ownership credentials. Regression tests exercise both HTTP and legacy SSE message paths and prove no victim revocation occurs. |
| MCP security headers were interpreted after Node had discarded or combined duplicates | Duplicate authorization, host, origin, session and workspace headers now fail before bearer/session resolution. Tests cover mixed-case duplicates. |
| An OAuth error body could turn HTTP 429/500/503 into local credential deletion | Only an HTTP 400 `invalid_grant` is treated as an authoritative grant rejection. Outages remain retryable failures and preserve credentials. Tests reproduced the previous destructive classification. |
| Token lifetime multiplication could overflow and bypass the lifetime ceiling | Validate `expires_in` in seconds before converting to Go nanoseconds. A regression reproduces a centuries-long input wrapping into a one-hour duration. |
| JWKS size checks happened after the entire response was downloaded | Stream and count decoded bytes, reject oversized declarations before reading, close rejected responses, and cap the complete fetch at five seconds. Failure tests prove early termination and refresh-lock release. Existing warm-cache/rotation tests still apply. |
| Opening optional trace storage could indefinitely block preparation | Apply the configured trace flush grace to opening storage as well. Timeout returns an untraced scope; caller cancellation continues to cancel the turn. Both outcomes are tested. |
| Production readiness could accept anonymous access outside the deployment pipeline | `ENVIRONMENT=production` now requires `AUTH_MODE=enforce` in application readiness. Staging observation and local development stay available. Tests assert refusal, preserved liveness and usable discovery. This is an intentional deployment requirement. |
| Supervision instructions exceeded their prompt budget | Remove repeated instructions from the `watch-outcome` procedure while retaining message-versus-fixed-call delivery, wake-versus-timer grants, tier limits, retry semantics, and verification requirements. Embedded routing metadata is unchanged. No model retraining or relaxed budget was used. |
| Pinned dependencies had known advisories | Update Python `cryptography` and `h2`; refresh compatible desktop lockfile dependencies. Python advisory findings fall from two to zero; desktop affected-package findings fall from 17 (8 high) to 5 (1 high). These are scanner counts, not counts of demonstrated product exploits. |

## Remaining production gates, in order

### 1. Establish session revocation, independently of billing

`services/supabase_auth.py` verifies a signed, unexpired JWT. It does not query whether its `session_id` still exists. The entitlement service maps an upstream 401 to revocation, but that proves only error mapping. A successful user lookup is not proof that a particular session has not been revoked. The CLI's logout also explicitly reports that it does not revoke remotely.

Implement an authoritative, narrowly privileged session-status lookup, bound to both the verified subject and session id. Cache positive results for a documented short interval, distinguish dependency failure from revocation, and preserve credentials on outages. Do not add a general service-role credential to the desktop or CLI. Decide and publish whether logout means local disconnect or server revocation; test the corresponding action.

Acceptance: mint a real session, revoke that exact session, replay its still-unexpired access token against every protected route, and prove refusal within the advertised window across two backend instances. Keep a second session for the same account alive to prove revocation is scoped correctly. [Supabase session semantics](https://supabase.com/docs/guides/auth/sessions) and [sign-out semantics](https://supabase.com/docs/guides/auth/signout) explain why signature verification alone cannot provide this guarantee.

### 2. Enforce spending and admission limits across instances

`api/paid_access.py` gates identity and subscription access. Neither it nor the current accounting path enforces a numeric account allowance. Cloud Run's eight concurrent requests per instance and ten-instance maximum are fleet controls, not per-customer quotas. A paid account can still monopolize capacity or incur unbounded cumulative usage.

Define Standard/Pro allowances explicitly, then implement atomic reservation and settlement in a shared ledger keyed by verified account. Track request id, known usage, incomplete usage, outcome and retry attempts; account for failed, cancelled and repaired generations. Reserve before opening the provider and release/settle with bounded crash recovery. Add per-account concurrent-turn and request-rate admission independent of commercial credits; return typed 429 responses with retry guidance. A process-local dictionary alone does not enforce a fleet-wide limit.

Acceptance: simultaneous requests through two instances cannot overspend the same allowance; cancellation and instance death cannot leak reservations; retries cannot silently double-charge a customer; unknown provider cost is never represented as zero. The allowance numbers are a product decision, not something this audit invented.

### 3. Turn routing measurements into a quality release gate

The shipped classifier's local benchmark passed its existing gate, but returned **167/181 recall-pass (92.3%)**, **149/181 strict matches (82.3%)**, and **19/21 correct docs decisions**. Fourteen cases missed required runbooks. Candidate misses were zero: increasing shortlist width is not the first fix supported by this corpus. Examples include plain-worktree creation versus recipe execution, PR discovery versus follow-up, topic shifts, and mixed intents.

Use the benchmark's per-case loss attribution to repair the ranking/count/admission failures. Add paraphrased training rows for both directions of each confused pair, keeping benchmark utterances out of training. Retrain as a separately reviewed batch and qualify against held-out users/intents, topic changes, tool continuations, unavailable tools, and adversarial terminal/repository text. The current audit changes only runbook body prose, so it makes no claim of improved classifier accuracy.

Before raising automation autonomy, measure end-to-end task success as well as selection: did the requested operation execute, did its result satisfy the request, and did the assistant report the observed outcome accurately? Require correct refusal/confirmation behavior independently of the aggregate success score. Set and ratchet product thresholds from that evidence; a selector pass alone cannot prove intelligent task completion.

### 4. Prove the provider and fallback against live contracts

The code already has valuable no-replay boundaries after streamed output, a circuit breaker, structured output validation, bounded repair, and fallback credential isolation. Preserve them. Mocked HTTP tests prove the request built by the adapter, not what either host currently accepts or bills.

Add a small opt-in live qualification suite for text, streaming completion, tool calls, forced tool selection, structured JSON, usage accounting, malformed-request errors, cancellation, and a forced primary outage. Exercise the fallback periodically even when the primary is healthy. Compare its armed state with deployment intent before promotion. Measure first-token and completion latency at normal and peak concurrency. No dedicated live-provider qualification or cloud configuration changes were performed by this audit.

### 5. Finish dependency remediation without breaking the application

Five desktop audit entries remain: Vite and the four-node dependency chain through Drizzle's old esbuild loader. The Vite advisory is high severity and concerns development-server behavior on Windows. The repository deliberately stays on Vite 8.0 because its 8.1 transition has a documented renderer initialization regression; compatible lockfile refresh did not supply a patched 8.0 release. The esbuild chain has moderate development-server exposure; npm's suggested force fix downgrades Drizzle incompatibly.

Treat both as outstanding work: qualify a patched Vite branch with a real packaged-renderer smoke test, and replace or upgrade the obsolete Drizzle loader with migration-generation parity tests. Do not force a downgrade merely to make audit output green. This audit preserves the Vite compatibility constraint and does not claim a clean desktop dependency audit. Reachability analysis should distinguish packaged runtime from developer servers. [Vite advisories](https://github.com/advisories/GHSA-fx2h-pf6j-xcff) and [esbuild advisory](https://github.com/advisories/GHSA-67mh-4wv8-2f99) describe the affected surfaces.

### 6. Validate the complete product under failure

Run a coordinated desktop/CLI/backend release candidate through login, refresh, logout, endpoint switching, sleep/resume, renderer replacement, backend restart, provider outage, and lost responses. Include timed messages and fixed-call timers: their delivery semantics differ, and replay after restart must not duplicate destructive work. Existing ownership, grants and race tests are strong evidence for the individual mechanisms; they cannot replace a cross-process acceptance run of the exact shipped binaries.

Bound request-body receipt before expensive parsing; the current byte cap does not itself bound a slow upload. Measure event-loop lag and queueing while ONNX selection is saturated. Publish operational alerts for auth-dependency failure, refresh failure, provider circuit state, turn cancellation/failure, incomplete accounting, queue age and stalled progress. Keep raw prompt/terminal content out of operational logs and do not enable production training-content capture without an explicit consent/retention design.

## Release recommendation

Merge the verified hardening fixes through the three project PRs. Preserve existing model/tool permission boundaries and the separate identity, billing and execution authorities. Close the revocation and spending gates before public paid availability; then qualify live end-to-end behavior and ratchet the routing/task-success thresholds. A bigger model would not repair these engineering gaps.
