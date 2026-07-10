# Daintree Assistant API v1

The Daintree Assistant Server exposes a **Daintree-native** HTTP API — it is *not*
OpenAI-compatible. There is exactly one client (the Daintree Assistant CLI), which
the server fully controls, so the wire contract is purpose-built rather than
shaped around a third-party schema.

The upstream provider is DeepSeek — its OpenAI-compatible wire shape is spoken
*internally*, behind a provider abstraction; the main model is `deepseek-v4-flash`.
That is invisible to clients.

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

A single fixed bearer key (development phase):

```
Authorization: Bearer <DAINTREE_API_KEY>
```

`/healthz`, `/readyz`, and `/version` are unauthenticated. (A full user/account system
will replace the fixed key later; the wire contract does not change when it does.)

## Protocol negotiation

Every request carries `protocol_version` (currently `2`). The supported range is also
advertised by `/version` and `/v1/daintree/capabilities`. A request may additionally
send `X-Daintree-Protocol: 2`; an unsupported version yields `426`.

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
  outside `input.messages`: only the main model receives its framed user-role data block;
  the selector and skill learner see the visible `input.messages` transcript only.
- Agent ids are launch identifiers. They are preserved exactly and JSON-quoted in the
  prompt; aggregate limits omit whole agent rows rather than truncating an id. No launch
  commands, environment variables, presets, authentication paths, raw settings, or secrets
  belong in `startup`.
- `runtime.worktree` is a typed, fresh per-request snapshot. An omitted wrapper means the
  live read was unavailable; `{ "current": null }` means Daintree successfully reported
  that there is no current worktree.
- `tools`/`tool_calls` use the standard `{id, type, function:{name, arguments}}` shape.
  `tool_choice` is flattened to `{ "name": … }` for the named case.
- `reasoning_content` (assistant turns only) carries DeepSeek thinking-mode chain-of-thought.
  It appears in the response only when thinking is enabled; on a turn that performed tool calls it
  MUST be echoed back verbatim on later requests or DeepSeek 400s (it is ignored otherwise). Omit
  it entirely when thinking is off. See Prompt assembly & caching.
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
        "retry_after":"5" }                    // optional raw Retry-After on rate limits
```

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
   clears the active set), capped (default 3). A tool-continuation round (no run) or a
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

Operational controls are `SKILL_SPECULATION_MAX_CONCURRENCY` (`0` is the kill switch),
`SKILL_SPECULATION_BUFFER_CHUNKS`, `SKILL_SPECULATION_BUFFER_BYTES`, and
`SKILL_SPECULATION_CLAIM_TIMEOUT_SECONDS` (closes a winner whose HTTP body is never
iterated). The `respond` log carries `skill_speculation` plus bounded-buffer/open stats;
`respond_upstream_open` records speculative reuse and post-`meta` wait. Dedicated
`skill_speculation_fallback`, `skill_speculation_abandoned`, and cleanup-timeout events
cover degraded lifecycle paths without logging request content.

## Prompt assembly & caching

DeepSeek serializes the prompt as **[all system messages, in array order] →
[tools] → [non-system conversation]** — a system message anywhere in the array
lands in the leading system region, before the ~18k-token tool schemas
(measured 2026-07-08: volatile system-role runtime context after the
conversation cut byte-identical turns to a 36% cache hit; 99% after this
layout). The rule: **only stable content may be system-role**; per-request
variable context rides ONE trailing user-role "[Injected context …]" data
block, where it costs exactly its own tokens.

| # | Message | Role | Stability |
|---|---------|------|-----------|
| 1 | Base system prompt | system | static — cached across all sessions |
| 2 | Integrations (MCP servers + tool index) | system | session-stable |
| 3 | Active skill bodies | system | semi-stable — replaced wholesale on a selector topic shift (omitted when empty) |
| 4 | Response contract | system | static — output-shape rules |
| 5 | Injected startup context | user | required, session-stable project/agent/instruction data |
| 6 | Visible `input.messages` conversation | user/assistant/tool | append-only — selector/learner transcript |
| 7 | Injected context (runtime + turn footer) | user | per-request tail — the only volatile block |

All client-provided context (ids, names, MCP details, project instructions, memories,
worktrees) is treated as untrusted data. Prose fields collapse whitespace; multiline fields
(project instructions, MCP instructions) are framed in delimited, escape-neutralized data
blocks. Agent ids are the exception to prose normalization: the assembler preserves their
exact value, JSON-quotes it, and admits or omits each complete row under the roster budget.

The main request is sent with `prompt_cache_key` ("daintree-main") by default
(`DEEPSEEK_SEND_PROMPT_CACHE_KEY`; suppressed in JSON mode). Reasoning is controlled by a
top-level `thinking` object, sent explicitly on every call: `{"type": "disabled"}` turns
thinking off, `{"type": "enabled"}` (optionally with a `reasoning_effort` tier) turns it on.
DeepSeek defaults thinking ON when the field is absent, so the explicit directive is what
keeps non-thinking calls (above all the selector) actually non-thinking.

The thinking posture is **hardcoded — a product decision like the model ids, not
configuration**. The main `/respond` call, the utility tasks, and the skill-learning gate run
**thinking-high** (`high` is DeepSeek's lightest tier; `low`/`medium` silently map up to it).
The **selector is the one exception**: it gates the turn's first byte, so it is hardcoded
**non-thinking** and its prompt instead asks for a reason-first JSON key order
(`task_type`/`reason` written before `skill_ids`) as the inline reasoning step — benchmarked
at equal selection quality and a fraction of the latency. Because a forced `tool_choice` is
the one thing incompatible with thinking on DeepSeek, the selector was moved off its forced
`select_skills` call onto `response_format` json_object (json_object output and non-forced
tool calling are both compatible with thinking). DeepSeek accepts only `high`/`max` and silently remaps `low`/`medium` up to
`high` — so `high` is the lightest thinking available, the only lower setting being thinking
off. Since DeepSeek's `max_tokens` is shared by reasoning + visible output, the small-output
calls (selector, utility tasks, gate) add `DEEPSEEK_THINKING_HEADROOM_TOKENS` to their tight
caps so the chain-of-thought can't starve the answer into a length truncation; the main
`/respond` call instead runs on its own flat budget (`MAIN_DEFAULT_MAX_TOKENS` ==
`MAIN_MAX_TOKENS_CEILING`, already sized for reasoning + output). Thinking is still forced off
for any request that pins a `tool_choice` (thinking + a forced tool choice is rejected upstream). When enabled, the model returns its chain-of-thought as `reasoning_content`; the
client stores it on the assistant turn and replays it verbatim on any turn that performed tool
calls (a hard DeepSeek requirement). Thinking mode ignores `temperature`/`top_p`/penalties.

## Errors

Stable envelope (request/auth/validation failures before SSE as JSON; generation failures
after streaming `meta` as an `error` event):

```jsonc
{ "error": { "type": "invalid_request" | "authentication" | "rate_limit" | "api_error",
             "code": "…", "message": "…", "param": "…" } }
```

| HTTP | Examples |
|------|----------|
| 400  | malformed request, `system_messages_not_allowed`, `reserved_tool_name`, `invalid_tool_schema`, `tool_choice_not_offered` |
| 401  | `invalid_api_key` |
| 413  | `request_too_large` (body cap, per-message/tool caps) |
| 426  | `unsupported_daintree_protocol` |
| 429  | `upstream_rate_limited` (with `Retry-After`) |
| 502 / 504 | `upstream_error` / `upstream_timeout` |
| 503  | not ready (`/readyz` failing) |

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
| `GET`  | `/v1/daintree/capabilities` | yes | Protocol range, limits, stream events, task profiles, catalog revision |
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
