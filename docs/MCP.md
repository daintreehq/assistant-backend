# The runbook-search MCP server

`POST https://assistant.daintree.org/v1/daintree/mcp` — a stateless MCP server
(Streamable HTTP) with one tool, `search_runbooks`. Anyone can add it: there is
no credential, no account and no session.

```json
{
  "mcpServers": {
    "daintree-runbooks": { "type": "http", "url": "https://assistant.daintree.org/v1/daintree/mcp" }
  }
}
```

## `search_runbooks`

| argument | type | |
|---|---|---|
| `query` | string, 1–8,192 chars, required | the task in natural language — "rename my terminals so I can tell the agents apart", not keywords |
| `max_results` | integer 1–20, default 5 | the most runbooks to return |

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
      "required_tools": ["context.snapshot", "terminal.extract.json", "terminal.rename"],
      "score": 0.9998,
      "selected": true,
      "instructions": "<the full runbook body, Markdown>"
    }
  ]
}
```

- `score` is the head's probability for that runbook against the query, not a cosine.
- `selected` is whether the classifier would load the runbook for this query (its
  cardinality answer and admission floors applied). Prefer selected runbooks; an empty
  selected set means no runbook really fits. The ranking always fills `max_results`
  when the catalog has that many, so lower hits are neighbours rather than answers.
- `foundation` marks a domain's base-judgment runbook (e.g. `daintree.foundation`).
  Search does not pair a specific with its foundation; ask for it by name if needed.
- `required_tools` and the steps in `instructions` name Daintree CLI tools. An agent
  without those tools should read a runbook as a procedure to adapt, not execute.

The answer is in `structuredContent` and, as the same JSON, in a `text` content block
for clients that predate structured output. The tool declares `outputSchema` and
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
over HTTP.

## Other routes

| route | |
|---|---|
| `GET /health`, `GET /healthz` | liveness (`/healthz` is unreachable from outside Cloud Run) |
| `GET /readyz` | `503` until the catalog and selector artifact have loaded; then the catalog revision and the number of searchable runbooks |
| `GET /version` | `server_version` and the `build_sha` the release stamped |
| `GET /` | redirect to the product page |
