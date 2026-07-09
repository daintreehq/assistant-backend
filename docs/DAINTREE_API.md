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
  messages or any free-form prompt; the request schema is strict (`extra="forbid"`).
- **Cache-optimized prompt layout.** The upstream prompt keeps the large append-only
  conversation in the cached prefix (base prompt → integration surface → conversation),
  then appends the small per-request tail (skill bodies → runtime → turn footer) after it,
  so a skill load/evict re-encodes only that tail. See [Prompt assembly](#prompt-assembly--caching).
- **Honest streaming.** Metadata arrives as the first SSE event; the stream never
  fabricates a successful finish after an upstream failure.

## Authentication

A single fixed bearer key (development phase):

```
Authorization: Bearer <DAINTREE_API_KEY>
```

`/healthz`, `/readyz`, and `/version` are unauthenticated. (A full user/account system
will replace the fixed key later; the wire contract does not change when it does.)

## Protocol negotiation

Every request carries `protocol_version` (currently `1`). The supported range is also
advertised by `/version` and `/v1/daintree/capabilities`. A request may additionally
send `X-Daintree-Protocol: 1`; an unsupported version yields `426`.

---

## POST /v1/daintree/respond

The single generation endpoint. Returns a native JSON body, or — when
`generation.stream` is `true` — a named-event SSE stream.

### Request

```jsonc
{
  "protocol_version": 1,                       // required

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

  "runtime": {                                 // CLI-reported environment (optional, rendered as inert data)
    "permission_tier": "operator",
    "project_path": "/path", "project_id": "proj_…",
    "mcp_servers": [                           // the CLI's actual MCP surface (server-rendered)
      { "name": "daintree", "transport": "http", "status": "connected",
        "tool_count": 12, "description": "…", "instructions": "…" }
    ],
    "configured_agent_ids": ["claude", "codex"],
    "scheduler_active": true,
    "active_worktree": "feature/x",
    "project_instructions": "…"                // framed as a delimited data block
  },

  "turn": {                                    // per-turn context (optional)
    "goal": "…", "is_wake": false,
    "workflow_runs": ["…"],
    "async_operations": ["…"],                 // live runtime-owned async futures (one line each)
    "memories": { "pinned": ["…"], "relevant": ["…"] },
    "session_ended_watchers": ["…"],
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
  "protocol_version": 1,
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
data: { "protocol_version":1, "request_id":"req_…", "model":"daintree-assistant",
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
data: { "error": { "type":"…","code":"…","message":"…","param":"…" } }
```

Guarantees:
- `meta` is always first, before any token — so the client renders the skill prelude and
  active set up front, as if the skill load happened before the reply. Its arrival also marks
  the upstream as connected (the first token is already in hand): show a "connecting" state
  until `meta`, then switch to generation.
- `status` (optional) marks a generation phase. Today the only phase is `thinking`, emitted
  once the instant the model's chain-of-thought begins; the `reasoning_content` deltas that
  follow are the CoT to display. It never appears when thinking is off. Unknown future phases
  should be ignored by clients.
- The server **prefetches the first upstream token before committing the `200`**, so a
  pre-stream upstream failure is delivered as an ordinary JSON error (with retries),
  not a broken stream.
- After `meta`, a mid-stream upstream failure emits a terminal `error` event and stops.
  The stream **never** emits `done` it did not earn (a truncated upstream stream is an error).
- `done` replaces any `[DONE]` sentinel; there is no OpenAI framing.

---

## Skills

Skill selection is removed from the main thread:

1. **Selector cadence.** A separate small-model call (returning a JSON object via
   `response_format` json_object, so it can run thinking-on) runs when: there is no valid
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
3. **Injection.** Only the active skill *bodies* are injected into the main prompt — never
   the full catalog — and they reach the model as **plain context** (the "Loaded skills"
   system block), not a tool call. The model is **not** shown a synthetic `skill__load`
   self-call; a `skills.prelude` is still returned for the client to render a visible
   "Skill loaded" card.
4. **State token.** `state` is an opaque, signed (`HMAC-SHA256`, key-ring/`kid` rotation),
   session-bound, expiring token carrying the active skills + catalog revision + cadence
   keys. It is verified fail-closed; an invalid/expired/wrong-session token is ignored
   (selection simply re-runs) and a `warnings` entry is added.

`skill.find` / `skill.load` (and their wire-safe `skill__find` / `skill__load` forms) are
reserved and rejected if a client offers them as tools or replays them in history.

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
| 5 | Conversation | user/assistant/tool | append-only — the cached bulk |
| 6 | Injected context (runtime + turn footer) | user | per-request tail — the only volatile block |

All client-provided context (ids, names, MCP details, project instructions, memories,
worktrees) is treated as untrusted data: single-line fields collapse whitespace; multiline
fields (project instructions, MCP instructions) are framed in delimited, escape-neutralized
data blocks so they cannot inject new instructions.

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

Stable envelope (pre-stream as JSON, mid-stream as an `error` event):

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
