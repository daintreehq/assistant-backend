# Daintree Assistant API v1

The Daintree Assistant Server exposes a **Daintree-native** HTTP API — it is *not*
OpenAI-compatible. There is exactly one client (the Daintree Assistant CLI), which
the server fully controls, so the wire contract is purpose-built rather than
shaped around a third-party schema.

DeepSeek (the upstream model) is still spoken in the OpenAI wire shape *internally*,
behind a provider abstraction; that is invisible to clients.

## Principles

- **Skills are the core.** Skill selection runs on a separate small model and skill
  bodies are injected server-side; the result is surfaced as a first-class `skills`
  block (active set, the newly-loaded prelude, and selector telemetry). A small model
  with the right runbook loaded is the source of the system's intelligence.
- **The server owns all system text.** Clients cannot send `system`/`developer`
  messages or any free-form prompt; the request schema is strict (`extra="forbid"`).
- **Cache-optimized prompt layout.** The upstream prompt is ordered most-stable-first
  (base prompt → integration surface → skill bodies → conversation → variable runtime
  context) so DeepSeek's prefix cache covers the large, stable content across a turn's
  rounds. See [Prompt assembly](#prompt-assembly--caching).
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
    "memories": { "pinned": ["…"], "relevant": ["…"] },
    "session_ended_watchers": ["…"]
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
- Tool definitions are bounded (total bytes, schema depth, property count, string sizes);
  `function.parameters` must be a JSON object whose root `type`, when present, is `object`.

### Response — non-streaming

```jsonc
{
  "protocol_version": 1,
  "request_id": "req_…",
  "model": "daintree-assistant",
  "message": {
    "role": "assistant",
    "content": "…",
    "tool_calls": [ { "id": "call_…", "type": "function",
                      "function": { "name": "git__status", "arguments": "{…}" } } ]
  },
  "finish_reason": "stop",                      // "stop" | "tool_calls" | "length" | …
  "usage": { "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0 },

  "skills": {                                   // first-class skill outcome
    "active":       [ { "id": "…", "version": "…", "title": "…" } ],
    "newly_loaded": [ { "id": "…", "version": "…", "title": "…" } ],
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
  "prompt_version": "main-2026-06-26.1",
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

event: delta
data: { "content": "Hel" }                      // repeated; may carry "tool_calls" delta fragments

event: done
data: { "finish_reason": "stop",
        "usage": { "prompt_tokens":0, "completion_tokens":0, "total_tokens":0, "cached_tokens":0 } }

event: error
data: { "error": { "type":"…","code":"…","message":"…","param":"…" } }
```

Guarantees:
- `meta` is always first, before any token — so the client renders the skill prelude and
  active set up front, as if the skill load happened before the reply.
- The server **prefetches the first upstream token before committing the `200`**, so a
  pre-stream upstream failure is delivered as an ordinary JSON error (with retries),
  not a broken stream.
- After `meta`, a mid-stream upstream failure emits a terminal `error` event and stops.
  The stream **never** emits `done` it did not earn (a truncated upstream stream is an error).
- `done` replaces any `[DONE]` sentinel; there is no OpenAI framing.

---

## Skills

Skill selection is removed from the main thread:

1. **Selector cadence.** A separate small-model call (`select_skills`, a forced function
   call) runs when: there is no valid `state`; `selection.force` is set or
   `selection.policy == "always"`; the catalog revision changed; the `turn_id` changed; or
   `instruction_revision` changed. It does **not** re-run on ordinary tool-continuation
   rounds (same turn). Selector failure **fails open** — the prior active set is retained.
2. **Active set.** Newly selected skills are merged additively (new-first) with the prior
   active set and capped (default 3).
3. **Injection.** Only the active skill *bodies* are injected into the main prompt — never
   the full catalog. Newly-loaded skills also produce a synthetic `skill__load` tool
   exchange in the upstream transcript (so the model observes the load) and the
   `skills.prelude` the client renders first.
4. **State token.** `state` is an opaque, signed (`HMAC-SHA256`, key-ring/`kid` rotation),
   session-bound, expiring token carrying the active skills + catalog revision + cadence
   keys. It is verified fail-closed; an invalid/expired/wrong-session token is ignored
   (selection simply re-runs) and a `warnings` entry is added.

`skill.find` / `skill.load` (and their wire-safe `skill__find` / `skill__load` forms) are
reserved and rejected if a client offers them as tools or replays them in history.

## Prompt assembly & caching

The upstream message array is ordered most-stable-first so DeepSeek's prefix cache covers
the largest stable content:

| # | Message | Stability |
|---|---------|-----------|
| 1 | Base system prompt | static — cached across all sessions |
| 2 | Integrations (MCP servers + tool index) | session-stable |
| 3 | Active skill bodies | turn-stable — cached across a turn's rounds |
| 4 | Conversation + synthetic skill-load | grows within a turn |
| 5 | Runtime context | per-request (kept out of the cached prefix) |
| 6 | Turn footer | per-request |

All client-provided context (ids, names, MCP details, project instructions, memories,
worktrees) is treated as untrusted data: single-line fields collapse whitespace; multiline
fields (project instructions, MCP instructions) are framed in delimited, escape-neutralized
data blocks so they cannot inject new instructions.

`prompt_cache_key` ("daintree-main") and the provider-specific `thinking:{disabled}` field
are opt-in (`DEEPSEEK_SEND_PROMPT_CACHE_KEY`, `DEEPSEEK_SEND_THINKING`), off by default
until a live smoke test confirms the exact endpoint accepts them.

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

`protocol_version` is explicit per request; `prompt_version` and `catalog_revision` are
returned on every response so the client can detect server-side prompt/catalog changes
(and re-run selection when the catalog moves).
