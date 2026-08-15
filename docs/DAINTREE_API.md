# Daintree Assistant API v1

The Daintree Assistant Server exposes a **Daintree-native** HTTP API — it is *not*
OpenAI-compatible. There is exactly one client (the Daintree Assistant CLI), which
the server fully controls, so the wire contract is purpose-built rather than
shaped around a third-party schema.

The upstream provider is OpenRouter — its OpenAI-compatible wire shape is spoken
*internally*, behind a provider abstraction; the main model is
`deepseek/deepseek-v4-flash-0731`, and OpenRouter selects which of the endpoints
serving it answers (throughput-sorted, filtered to endpoints that do not collect
or train on data). That is invisible to clients.

## Principles

- **Skills are the core.** Skill selection runs on a separate small model and skill
  bodies are injected server-side; the result is surfaced as a first-class `skills`
  block (active set, the newly-loaded prelude, and selector telemetry). A small model
  with the right runbook loaded is the source of the system's intelligence.
- **The server owns all system text.** Clients cannot send `system`/`developer`
  messages or a free-form system prompt; the request schema is strict (`extra="forbid"`).
  Typed startup/project-instruction fields are bounded, server-framed untrusted data.
- **Cache-optimized prompt layout.** Stable system layers and tool schemas lead, followed
  by a required session-stable startup block, the append-only visible conversation, and
  one small volatile runtime/turn tail. See [Prompt assembly](#prompt-assembly--caching).
- **Honest streaming.** Metadata is always the first public SSE event and no private
  speculative model event can precede it. The stream never fabricates a successful
  finish after an upstream failure.

## Authentication

Every request carries the caller's own API key as the bearer token; that same key is
the upstream (OpenRouter) credential funding the turn's model calls. For early testers
the key IS their OpenRouter key (`sk-or-v1-…`); later it becomes a subscription key the
backend maps to a credential server-side, with no wire change.

```
Authorization: Bearer <caller API key>
```

The server holds no shared inbound secret and no upstream credential of its own, and
performs only structural validation (present, header-safe ASCII) — a missing or
malformed key is `401 invalid_api_key`, while a well-formed key the upstream then
rejects surfaces as `401 provider_invalid_api_key`. Same status, deliberately
different codes: ours means "fix your header", theirs means "the key travelled fine
and OpenRouter refused it". An underfunded or permission-limited key is different
again (`402 provider_insufficient_credits`, `403 provider_key_forbidden`) — see
[Errors](#errors). There is no unauthenticated mode in any environment.

`/healthz`, `/readyz`, and `/version` are unauthenticated (Cloud Run probes them; they
carry no user data).

### POST /v1/daintree/auth/verify

Because validation is structural, a well-formed key that no provider ever issued
authenticates fine and is only discovered on the caller's first real turn. This endpoint
closes that gap at sign-in by asking the provider directly. It is a tokenless
introspection call (not a model call), takes **no request body**, and requires the
bearer header like every other authed route.

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
- `usable` — it recognises it *and* the account can actually fund a turn.

A recognised key with a spent balance is `valid: true, usable: false,
reason: "credits_exhausted"`. A client reading only `valid` would show a successful
sign-in, then fail on the first real request with what looks like an unrelated error.

`reason` is the stable machine-readable outcome — branch on it, not on `detail`:
`ok`, `provider_rejected`, `credits_exhausted`. Usability is judged conservatively:
only a *positively reported* zero-or-negative `limit_remaining` counts as exhausted,
so an unlimited or pay-as-you-go key (which reports no limit at all) stays usable.

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
upstream_protocol_error` (a 200 whose body is not a key record), `401 invalid_api_key`
for a missing/malformed bearer at our own door, and `503` before readiness. The probe carries a short 10s read budget rather than
the main-model timeout. Being a sign-in bootstrap call, it does not require or check
`X-Daintree-Protocol`.

## Endpoint routing (optional)

`/v1/daintree/respond` accepts an optional top-level `routing` block. Omit it —
almost every request does — and the server default applies.

```jsonc
"routing": {
  "privacy": "no_training",        // "no_training" (default) | "zdr"
  "sort": "throughput",            // "throughput" (default) | "price" | "latency"
  "only":   ["deepinfra"],         // optional endpoint allowlist (≤24 slugs)
  "ignore": ["some-endpoint"]      // optional endpoint denylist  (≤24 slugs)
}
```

The caller owns the key and pays the bill, so how strict the privacy filter is
and whether speed or price ranks endpoints are legitimately theirs to choose.

**This is a closed set, not a pass-through.** An arbitrary `provider` block is
rejected: forwarding one would let a client drop the no-training floor, pin an
endpoint that cannot do `response_format` (breaking every JSON call), or send a
parameter this server has never tested — each failing as an inscrutable upstream
error rather than a validation one. Unknown fields and unknown values are `400`.
Slugs must be lowercase alphanumerics with `.`, `-` or `_`.

Two things a caller cannot do:

- **Weaken privacy.** `data_collection: "deny"` is sent unconditionally and is not
  derived from any field here. `privacy: "zdr"` is *additive* — it adds the
  stricter retention filter on top, it does not replace the no-training one.
- **Guarantee a route.** `zdr`, an `only` list, or an unusual parameter mix can
  empty the eligible pool. That fails closed with `503
  upstream_no_compliant_provider` rather than silently relaxing the policy.

`/v1/daintree/capabilities` reports the active mode, its accurate description, and
the values a client may select.

## Protocol negotiation

A generation or skill-resolve request carries `protocol_version` in its body. Any
request may additionally declare it as a header — `X-Daintree-Protocol: 2`. The
supported range is advertised by `/version` and `/v1/daintree/capabilities`. An
unsupported version yields `426` from either channel.

The header is checked on every route that runs the caller's content through the model —
`/respond`, `/skills/resolve` and `/tasks` alike. (`/tasks` has no `protocol_version`
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
  "protocol_version": 2,                       // required

  "session": {                                 // required (no anonymous mode)
    "id": "sess_…",                            // required, non-empty
    "turn_id": "turn_…",                       // required, non-empty
    "instruction_revision": 0,                 // default 0 — bump on a mid-turn interjection
    "round": 0                                 // default 0 — tool-continuation round counter
  },

  "state": "dst1.<payload>.<sig>",             // nullable signed skill-state token (see Skills)

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
    ]
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

  "selection": { "policy": "new_instruction", "force": false },   // "new_instruction" | "always"

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
  Neither the selector nor the skill learner ever sees it — the selector's evidence is the
  sanitized `input.messages` transcript plus bounded turn/workflow hints and the available
  tool names; the learner's is `input.messages` plus the just-generated final answer and
  the available tool names.
- Agent ids are launch identifiers. They are preserved exactly and JSON-quoted in the
  prompt; aggregate limits omit whole agent rows rather than truncating an id. No launch
  commands, environment variables, presets, authentication paths, raw settings, or secrets
  belong in `startup`.
- `runtime.worktree` is a typed, fresh per-request snapshot. An omitted wrapper means the
  live read was unavailable; `{ "current": null }` means Daintree successfully reported
  that there is no current worktree.
- `tools`/`tool_calls` use the standard `{id, type, function:{name, arguments}}` shape.
  `tool_choice` is flattened to `{ "name": … }` for the named case.
- `reasoning_content` (assistant turns only) carries thinking-mode chain-of-thought.
  It appears in the response only when thinking is enabled; on a turn that performed tool calls it
  SHOULD be echoed back verbatim on later requests so the model can continue the reasoning that
  produced those calls (it is ignored on turns that did not). Omit it entirely when thinking is
  off. The server renames it to OpenRouter's `reasoning` at the provider boundary; the Daintree
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
  "protocol_version": 2,
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

  "skills": {                                   // first-class skill outcome
    "active":       [ { "id": "…", "title": "…" } ],
    "newly_loaded": [ { "id": "…", "title": "…" } ],
    "prelude": {                                // render BEFORE the assistant reply
      "tool_executions": [
        { "call":   { "id": "call_…", "type": "function",
                      "function": { "name": "skill__load", "arguments": "{\"skillId\":\"…\"}" } },
          "result": { "role": "tool", "tool_call_id": "call_…", "content": "{…}" },
          "display_name": "skill.load" }
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
data: { "protocol_version":2, "request_id":"req_…", "model":"daintree-assistant",
        "skills": { …same skills block… }, "state":"dst1.…",
        "catalog_revision":"sha256:…", "prompt_version":"…", "warnings":[] }

event: status
data: { "phase": "thinking" }                    // once, when thinking-mode CoT begins (thinking on only)

event: delta
data: { "content": "Hel" }                      // repeated; may carry "reasoning_content" or "tool_calls" fragments

event: done
data: { "finish_reason": "stop",
        "usage": { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, "cached_tokens":0 } }

event: error
data: { "error": { "type":"…","code":"…","message":"…","param":"…" },
        "request_id":"req_…",                  // same id as the response header
        "retry_after":"5" }                    // optional raw Retry-After on rate limits
```

`request_id` is on the **error** event as well as `meta` because a stream can fail
*before* `meta` is ever sent — a preparation failure does exactly that — and `meta` is
otherwise the only event carrying it. The HTTP status is already committed to 200 by
then, so this is the only channel a failed stream has to identify itself.

Guarantees:
- `meta` is always first, before any token — so the client renders the skill prelude and
  active set up front, as if the skill load happened before the reply. It is emitted as soon
  as authoritative selection and prompt assembly finish. An eligible speculative main
  stream may already be open privately, but none of its status, reasoning, content,
  tool-call, usage, or error events can cross this boundary. `meta` marks skill selection
  complete; clients can show the skill-load cue immediately.
- `status` (optional) marks a generation phase. Today the only phase is `thinking`, emitted
  once the instant the model's chain-of-thought begins; the `reasoning_content` deltas that
  follow are the CoT to display. It never appears when thinking is off. Unknown future phases
  should be ignored by clients.
- After `meta`, the server either consumes an exact-match speculative winner or opens the
  canonical upstream stream. Transient open/prefetch failure is retried internally before
  any model delta; exhaustion emits a terminal `error` event in the already-open SSE stream.
- A later mid-stream upstream failure likewise emits a terminal `error` event and stops.
  The stream **never** emits `done` it did not earn (a truncated upstream stream is an error).
- `done` replaces any `[DONE]` sentinel; there is no OpenAI framing.

---

## Skills

Skill selection is removed from the main thread:

1. **Selector cadence.** A separate non-thinking small-model call (returning a JSON object via
   `response_format` json_object) runs when: there is no valid
   `state`; `selection.force` is set or
   `selection.policy == "always"`; the catalog revision changed; the `turn_id` changed; or
   `instruction_revision` changed. It does **not** re-run on ordinary tool-continuation
   rounds (same turn). Selector failure **fails open** — the prior active set is retained.
2. **Active set.** A **successful** selector run is *authoritative*: its returned ids are the
   complete active set for the instruction (a topic-shift **replacement** — an empty result
   clears the active set), capped (default 3). Each selected specific is additionally paired
   with its **domain foundation** (the `[foundation]` skill sharing its id namespace) before
   the cap is applied, so `skills.active`/`newly_loaded` may include a foundation id the
   selector did not itself return. A tool-continuation round (no run) or a
   degraded selector reuses the prior set unchanged, so a classifier hiccup never evicts an
   in-progress runbook. The selector is instructed to re-list any active skill it means to
   keep. (The explicit `resolve` `load` action, by contrast, is additive over the prior set.)
3. **Streaming speculation.** Only when all of these are true — streaming response, selector
   run required, valid session-bound state, catalog revision current, and a non-empty prior
   active set — the server races the authoritative selector against a private main stream
   assembled from that exact ordered prior set. The private producer applies chunk and
   serialized-byte backpressure. If the final resolved ids match in exact order (including
   selector fail-open reuse), the already-running request becomes canonical. An empty,
   added, removed, reordered, or catalog-changed result closes and discards it before a new
   canonical request starts. Empty, invalid, expired, wrong-session, and stale-catalog state
   always waits for selection serially. The non-streaming path is unchanged.
4. **Injection.** Only the active skill *bodies* are injected into the main prompt — never
   the full catalog — and they reach the model as **plain context** (the "Loaded skills"
   system block), not a tool call. The model is **not** shown a synthetic `skill__load`
   self-call; a `skills.prelude` is still returned for the client to render a visible
   "Skill loaded" card.
5. **State token.** `state` is an opaque, signed (`HMAC-SHA256`, key-ring/`kid` rotation),
   session-bound, expiring token carrying the active skills + catalog revision + cadence
   keys. It is verified fail-closed; an invalid/expired/wrong-session token is ignored
   (selection simply re-runs) and a `warnings` entry is added.

`skill.find` / `skill.load` (and their wire-safe `skill__find` / `skill__load` forms) are
reserved and rejected if a client offers them as tools or replays them in history.

---

## Documentation lookup

Questions about **Daintree itself** are answered by the server, not by handing the client a
tool call to run. The public documentation MCP (`https://daintree.org/api/mcp`) is no-auth,
stateless and identical for every caller, so nothing about it needs to run on the user's
machine — unlike the Daintree control plane, which drives local terminals and worktrees
behind a per-session token.

1. **Classification.** The selector call already on the turn's first-byte path returns a
   second answer alongside `skill_ids`: `docs_query`, a short search string, or `null`. It is
   independent of skill selection — a turn can need both, either, or neither — and `null` is
   the common case. A turn that skips selection (continuation round, no-task fast path) or
   whose selector degraded never looks anything up.
2. **Retrieval.** One stateless JSON-RPC `tools/call` to the docs server's `search` tool,
   before the main upstream stream opens. It gets its own slice of the turn's `preparation`
   budget — *minus a reserve* for the assembly and teardown that still follow — enforced as a
   wall-clock bound and skipped entirely when too little remains. A slow docs host therefore
   costs first-byte latency and never the turn. Redirects are refused rather than followed,
   and the response is read under a size cap.
3. **Injection.** The top few ranked sections are sanitized, byte-capped, framed as an inert
   `<daintree_docs>` data block and appended to the **volatile user-role tail** (row 7 above)
   — never a system message, which would re-encode the tool schemas for content that lives
   for exactly one turn. Each section carries a complete `URL:` built server-side from a
   validated same-site path; the model cites what it is given rather than assembling a URL.
3b. **Continuation.** The query is carried in the signed state token, scoped to the
   `turn_id`, and re-run on the turn's tool-continuation rounds — those skip selection, and
   the tail carrying the pages is rebuilt every round, so without this a usage question that
   takes a tool call to answer would lose its sources between asking and answering. A new
   instruction is classified afresh and never inherits the query.
4. **Visibility.** The search appears in `skills.prelude.tool_executions` as a `docs__search`
   execution (display name `docs.search`), the same channel a skill load rides, so the user
   sees which query produced the cited pages. Its result carries the matched **paths only** —
   the page text is already going to the model through the prompt.
5. **Speculation.** A docs hit disqualifies an otherwise-exact speculative stream: that
   stream's prompt was assembled before the pages existed, so matching skill ids do not make
   it the same request.

**Best-effort by construction.** Every failure — no client, no budget, a docs server that
errors, times out or matches nothing — leaves the turn to answer without documentation. It
never fails the request. Advertised as `capabilities.docs_lookup`; a client gating on it
should stop offering its own docs tools, since two paths to the same answer means the model
uses both.

**The tradeoff, stated:** docs answers now depend on the backend being reachable. They
previously survived a backend outage and a Daintree outage independently, and usage
questions get asked precisely when something is broken.

Operational controls are `SKILL_SPECULATION_MAX_CONCURRENCY` (`0` is the kill switch),
`SKILL_SPECULATION_BUFFER_CHUNKS`, `SKILL_SPECULATION_BUFFER_BYTES`, and
`SKILL_SPECULATION_CLAIM_TIMEOUT_SECONDS` (closes a winner whose HTTP body is never
iterated). The `respond` log carries `skill_speculation` plus bounded-buffer/open stats;
`respond_upstream_open` records speculative reuse and post-`meta` wait. Dedicated
`skill_speculation_fallback`, `skill_speculation_abandoned`, and cleanup-timeout events
cover degraded lifecycle paths without logging request content.

## Prompt assembly & caching

The DeepSeek chat template serializes the prompt as **[all system messages, in
array order] → [tools] → [non-system conversation]** — a system message anywhere in the array
lands in the leading system region, before the ~18k-token tool schemas
(measured 2026-07-08: volatile system-role runtime context after the
conversation cut byte-identical turns to a 36% cache hit; 99% after this
layout). The rule: **only stable content may be system-role**; per-request
variable context rides ONE trailing user-role "[Injected context …]" data
block, where it costs exactly its own tokens.

| # | Message | Role | Stability |
|---|---------|------|-----------|
| 1 | Base system prompt | system | static — cached across all sessions |
| 2 | Integrations (MCP servers) | system | session-stable — client tools are NOT listed here; their schemas already ride the function-calling interface |
| 3 | Active skill bodies | system | semi-stable — replaced wholesale on a selector topic shift (omitted when empty) |
| 4 | Response contract | system | static — output-shape rules |
| 5 | Injected startup context | user | required, session-stable project/agent/instruction data |
| 6 | Visible `input.messages` conversation | user/assistant/tool | append-only — selector/learner transcript |
| 7 | Injected context (runtime + turn footer + retrieved docs) | user | per-request tail — the only volatile block |

All client-provided context (ids, names, MCP details, project instructions, memories,
worktrees) is treated as untrusted data. Prose fields collapse whitespace; multiline fields
(project instructions, MCP instructions) are framed in delimited, escape-neutralized data
blocks. Agent ids are the exception to prose normalization: the assembler preserves their
exact value, JSON-quotes it, and admits or omits each complete row under the roster budget.

No `prompt_cache_key` is sent: no endpoint for this model advertises the parameter, and the
routing policy's `require_parameters` filter would find no eligible endpoint for a request
carrying one. Prefix caching is implicit at whichever endpoint serves the request.

Every request also carries a top-level `provider` block — OpenRouter's endpoint-selection
policy. Its defaults sit beside the model ids and a caller may narrow or re-rank them via
the request's [`routing`](#endpoint-routing-optional) block: `sort: "throughput"` (fastest tokens/sec wins, price
second), `data_collection: "deny"` (only endpoints that do not collect prompt data or train on
it — a no-training floor, **not** zero data retention <!-- privacy-copy-ok -->; see
[`PROVIDER_POLICY.md`](PROVIDER_POLICY.md)), `require_parameters: true` (drop endpoints that would silently ignore something we
sent — this is what routes a json_object call away from an otherwise-faster endpoint with no
`response_format` support), and `allow_fallbacks: true` (the next-fastest compliant endpoint
covers an outage). A policy that matches nothing returns 404 **or** 503 depending on which
filter emptied the pool; both surface as `upstream_no_compliant_provider` rather than a
generic bad-request.

Reasoning is controlled by OpenRouter's unified `reasoning` object, sent explicitly on every
call: `{"enabled": false}` turns thinking off, `{"effort": "<tier>"}` turns it on at a tier.
A reasoning model thinks when the field is absent, so the explicit directive is what keeps
non-thinking calls (above all the selector) actually non-thinking.

The thinking posture is **hardcoded — a product decision like the model ids, not
configuration**. **The whole interactive surface is non-thinking**: the main `/respond` call,
every utility task, and the selector all send `{"enabled": false}`. This workload does not
need a chain-of-thought — the main model routes a request onto a skill runbook and emits tool
calls, and the utility tasks are bounded extractions and classifications over text the client
already gathered — while reasoning cost seconds of first-byte latency (a measured greeting
turn spent 366 reasoning tokens and ~3.5s of an 11.5s wall-clock).

There is **no middle tier to retreat to**. DeepSeek accepts only `high`/`max` and silently
remaps `low`/`medium` up to `high`; measured over 5 samples each on a fixed prompt, `low` and
`high` produced statistically identical chain-of-thought (mean 662 vs 656 reasoning tokens).
So the only real step below `high` is off, which is the posture taken. The selector reached
this position first, on its own first-byte argument, and its prompt asks for a reason-first
JSON key order (`task_type`/`reason` written before `skill_ids`) as an inline reasoning step —
benchmarked at equal selection quality and a fraction of the latency. (Because a forced
`tool_choice` is incompatible with thinking on DeepSeek, the selector was also moved off its
forced `select_skills` call onto `response_format` json_object.)

**Skill learning still thinks**: its gate runs thinking-high on the small model and its
builder/improver run `high` on GPT-5.6 Sol. It is local-dev-only and runs fire-and-forget
*after* the response is sent, so its latency never reaches a user, and writing a whole skill
from a transcript is the one genuinely hard task here.

`/respond` advertises the posture as `respond.thinking_enabled` on `/v1/daintree/capabilities`
(currently `false`), which is what a client should gate any thinking UI on. Because
`max_tokens` is shared by reasoning + visible output, a small-output **thinking** call adds
`THINKING_HEADROOM_TOKENS` to its tight cap so the chain-of-thought can't starve the answer
into a length truncation — under the current posture the skill-learning gate is the only such
call, and a non-thinking utility task deliberately does *not* get that slack (it would raise
the visible-output ceiling above the cap its profile set). The main `/respond` call instead
runs on its own flat budget (`MAIN_DEFAULT_MAX_TOKENS` == `MAIN_MAX_TOKENS_CEILING`).

Thinking is independently forced off for any request that pins a `tool_choice` (thinking + a
forced tool choice is rejected by the model; which endpoint enforces it is not worth betting a
turn on) — that guard is enforced separately from the posture, so it holds if the posture ever
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
| 401  | `invalid_api_key` | Bearer missing or malformed **at Daintree's door**. Send a valid header. |
| 413  | `request_too_large` | Body cap, or a per-message / per-tool cap. |
| 426  | `unsupported_daintree_protocol` | Upgrade the client. |
| 404  | `unknown_task` | `/v1/daintree/tasks` was asked for a task profile that does not exist. Read the available ids from `/v1/daintree/capabilities`. |
| 503  | *(not ready)* | `/readyz` is failing; the instance has not finished starting or is misconfigured. |
| 500  | `internal_error` | Daintree bug. Report with the request id. |

### The caller's provider account

The caller's own key rode the request, so these are settings on **their**
OpenRouter account — and each one has a different fix. Never retried.

| HTTP | Code | Action |
|------|------|--------|
| 401  | `provider_invalid_api_key` | OpenRouter rejected the key. Replace or rotate it. Distinct from `invalid_api_key`: the key travelled fine, the provider refused it. |
| 402  | `provider_insufficient_credits` | The account cannot fund the call. Add credits or use another key. |
| 403  | `provider_key_forbidden` | Key model-permissions, spend limit or a provider guardrail. |

### Provider routing and availability

| HTTP | Code | Action |
|------|------|--------|
| 429  | `upstream_rate_limited` | OpenRouter is rate limiting. Honour `Retry-After`. Retryable. |
| 503  | `upstream_no_compliant_provider` | No endpoint matched the active routing policy (see [`PROVIDER_POLICY.md`](PROVIDER_POLICY.md)). **Not** retried — the policy is deterministic, so an immediate replay re-derives the same empty pool. Relax the request's `routing` block, or report a routing-policy problem. |
| 503  | `upstream_unavailable` | Provider outage or no capacity. Retryable. |
| 504  | `upstream_timeout` | Upstream inactivity/deadline. Retryable. |

### Faults on our side of the upstream call

| HTTP | Code | Action |
|------|------|--------|
| 502  | `upstream_request_rejected` | OpenRouter judged the body **Daintree generated** malformed. Almost always a server bug; report the request id. Carries the provider's own reason. |
| 502  | `upstream_protocol_error` | An unparseable response: bad JSON, a broken chunk, a stream that ended without `[DONE]`. Provider/compatibility problem. |
| 502  | `upstream_error` | Residual/legacy catch-all. Still emitted for a missing or malformed upstream credential and for a non-verdict verification failure. |

Raw upstream bodies are never returned. For a non-auth 4xx a **bounded,
credential-scrubbed excerpt** of the provider's reason is appended to the message
— it is usually the only thing that says *why*. Account-class failures carry no
upstream prose at all.

For a streaming `/respond`, public generation begins only after the `200` and `meta` event;
an exhausted canonical 429/502/504 failure therefore uses the SSE `error` shape rather than
changing the already-committed HTTP status. A failed private speculative open is never
exposed before `meta`: a private post-open/buffer failure can fall back to a fresh canonical
open, while an already-exhausted open/prefetch retry budget is preserved (not doubled) and
becomes the terminal SSE error if that exact speculative request wins selection. When an
exhausted rate limit carried `Retry-After`, its raw string value is preserved as the optional
top-level SSE `retry_after` field; other terminal stream errors omit the field.

---

## Other endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/daintree/respond` | yes | Generation (native, + first-class skills) |
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task ids, catalog revision |
| `POST` | `/v1/daintree/auth/verify` | yes | Sign-in probe: does OpenRouter recognise the bearer? (no body; no inference). `valid` plus optional budget metadata; recognised is not the same as fundable |
| `GET`  | `/v1/daintree/skills` | yes | Skill metadata (ETag/304); never the body |
| `POST` | `/v1/daintree/skills/resolve` | yes | Manual `find` / `load` / `clear`; returns the same `skills` block + `state` |
| `POST` | `/v1/daintree/tasks` | yes | Named utility-model tasks (server-owned prompts; no client task prompt) |
| `GET`  | `/healthz` | no | Liveness |
| `GET`  | `/readyz` | no | Readiness (config, secrets, prompts, catalog, provider) |
| `GET`  | `/version` | no | Build SHA, version, protocol range |

## Versioning

`protocol_version` is explicit per request. `prompt_version` is a stable prompt-family
label (`"main"`) — pre-release, it is not bumped on prompt edits; everything on disk is
simply the current prompt. `catalog_revision` is the real change signal: it is a content
hash that moves whenever any skill changes, so the client re-runs selection when the
catalog moves. Both are returned on every response.
