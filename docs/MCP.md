# The runbook MCP server

`POST https://assistant.daintree.org/v1/daintree/mcp` — a stateless MCP server
(Streamable HTTP) with two read-only tools, `search_runbooks` and `get_runbook`.
Anyone can add it: there is no credential, no account and no session.

```bash
claude mcp add --transport http daintree-runbooks https://assistant.daintree.org/v1/daintree/mcp
codex mcp add daintree-runbooks --url https://assistant.daintree.org/v1/daintree/mcp
```

**It supplies instructions, not actions.** A runbook tells Claude Code or Codex which
Daintree app actions to take — `agent.launch`, `terminal.getOutput`,
`worktree.createWithRecipe` — and those are served by the *Daintree app's own* MCP
connection (the per-pane one Daintree configures, or an API-key client). Installing
this server gives a caller no workspace control and no notices, and a runbook grants
no permission. Keep the two connections distinct: `daintree-runbooks` for this one,
`daintree` for the app. Client-side guidance for both hosts is
[`docs/agents/DAINTREE_RUNBOOK_CLIENT.md`](agents/DAINTREE_RUNBOOK_CLIENT.md).

## `search_runbooks`

| argument | type | |
|---|---|---|
| `query` | string, 1–8,192 chars, required | the outcome wanted, in natural language — "ask one agent a question and bring back its answer", not keywords, not a transcript, terminal output or secrets |
| `max_results` | integer 1–20, default 3 | the most ranked runbooks to return; supporting runbooks come on top |

The search is the local two-stage selector (`runbooks/local/`): a cosine shortlist
of 32 over the embedded catalog, then a set-wise head that scores the shortlist
together and predicts how many runbooks the query needs. The query is ranked as a
lone user message with nothing loaded. Results come back best first:

```json
{
  "catalog_revision": "sha256:…",
  "runbooks": [
    {
      "id": "daintree.terminals.rename",
      "title": "Rename terminal tabs",
      "summary": "…",
      "when_to_use": "…",
      "foundation": false,
      "risk": "ui",
      "required_tools": ["terminal.list", "terminal.rename"],
      "optional_tools": ["terminal.getOutput", "terminal.readLastMessageOwned"],
      "requires": [],
      "content_hash": "sha256:…",
      "instructions": "<the full runbook body, Markdown>",
      "score": 0.9998,
      "selected": true
    }
  ],
  "supporting_runbooks": [
    { "id": "daintree.foundation", "foundation": true, "instructions": "…", "…": "…" }
  ]
}
```

- `score` is the head's probability for that runbook against the query, not a cosine.
- `selected` is whether the classifier judges the runbook fits (its cardinality answer
  and admission floors applied). Follow selected runbooks; an empty selected set means
  no runbook really fits, and a failed search is an error, never an empty ranking. The
  ranking fills `max_results` when the catalog has that many, so lower hits are
  neighbours rather than answers.
- `supporting_runbooks` is every returned runbook's instruction dependencies — its
  nearest domain foundation, then anything it declares in `requires`, recursively —
  once each, dependencies first, and never a runbook already in `runbooks`. Read them
  before following the procedure. They are not ranked hits and do not count against
  `max_results`.

## `get_runbook`

| argument | type | |
|---|---|---|
| `id` | string, required | an exact runbook id, as a runbook, search result or checkpoint gave it |

```json
{
  "catalog_revision": "sha256:…",
  "runbook": { "id": "daintree.agents.ask-and-relay", "content_hash": "sha256:…", "instructions": "…", "…": "…" },
  "supporting_runbooks": [ { "id": "daintree.foundation", "…": "…" } ]
}
```

Exact retrieval ranks nothing, so it carries no `score` or `selected`, and it never
touches the classifier. An unknown id is a tool error; so is the retired
`daintree.docs.answer-usage-question`, which is loaded only because the training
corpus names it and is never served by either tool. A caller that records
`content_hash` in its checkpoint can tell on resume whether the procedure it was
following has changed.

## Served fields

Both tools serve a runbook in the same form: `id`, `title`, `summary`, `when_to_use`,
`foundation`, `risk`, `required_tools`, `optional_tools`, `requires`, `content_hash`
and `instructions`; a search hit adds `score` and `selected`.

- `required_tools` and `optional_tools` are **Daintree app MCP action ids** — the
  baseline path's actions, and those of a conditional branch or caller variant. A
  host may expose them under a namespaced or escaped name. Neither is a capability
  gate: the procedure itself says what to check on the caller's actual surface, and
  what to do when an action or argument (such as `notify`) is absent.
- `risk` is descriptive (`read local ui terminal project external git system`), not
  an authorization tier.

The answer is in `structuredContent` and, as the same JSON, in a `text` content block
for clients that predate structured output. Both tools declare `outputSchema` and
`readOnlyHint: true`.

## Protocol

- **Both protocol eras, one endpoint, no sessions.** `2026-07-28` clients call
  `server/discover` / `tools/list` / `tools/call` directly, with the version in the
  `MCP-Protocol-Version` header and `params._meta["io.modelcontextprotocol/protocolVersion"]`.
  `2025-11-25`, `2025-06-18` and `2025-03-26` clients open with `initialize`, which is
  answered and forgotten. No `Mcp-Session-Id` is ever issued, so consecutive requests
  may land on different Cloud Run instances. `ping` is answered; notifications and
  client responses get `202` with no body.
- **Plain JSON only.** Every response is `application/json`; `GET` and `DELETE` are
  `405` with `Allow: POST`.
- **Errors.** Argument problems are tool results with `isError: true`, so the calling
  model can read and fix them; so is a search the classifier could not answer, which
  is retryable. Unknown tool is `-32602` and unknown method `-32601`. Transport faults
  are HTTP `400`: parse error `-32700`; batch or malformed message `-32600`; an
  `Mcp-Method`, `Mcp-Name` or version header contradicting the body `-32020`; an
  unsupported version `-32022` with `data.supported`. A foreign `Origin` is `403`
  (DNS-rebinding defence), a non-JSON `Content-Type` `415`, a body over 64 KiB `413`,
  and a server that is still starting `503`.

The protocol lives in `src/daintree_assistant_server/runbook_mcp.py`; the HTTP
transport in `api/mcp.py`. `tests/integration/test_runbook_mcp.py` drives both eras
and both tools over HTTP.

## Other routes

| route | |
|---|---|
| `GET /health`, `GET /healthz` | liveness (`/healthz` is unreachable from outside Cloud Run) |
| `GET /readyz` | `503` until the catalog and selector artifact have loaded; then the catalog revision and the number of searchable runbooks |
| `GET /version` | `server_version` and the `build_sha` the release stamped |
| `GET /` | redirect to the product page |
