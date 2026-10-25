# The runbook MCP server

`POST https://assistant.daintree.org/v1/daintree/mcp` — an MCP server (Streamable
HTTP) with two read-only tools, `search_runbooks` and `get_runbook`. Anyone can add
it: there is no credential and no account. The only session state is which
supporting runbooks a session was already sent ([Protocol](#protocol)).

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
| `query` | string, 1–8,192 chars, required | the outcome wanted, in natural language — "ask one agent a question and bring back its answer", not keywords, not a transcript, terminal output or secrets — keeping how the user wants it done (several agents, a time limit, a running agent, a review loop, another machine: say remote host, whatever the user calls it), which decides the procedure |
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
    },
    {
      "id": "daintree.worktree.relocate-agents",
      "title": "…",
      "score": 0.0031,
      "selected": false
    }
  ],
  "supporting_runbooks": [
    { "id": "daintree.foundation", "title": "…", "content_hash": "sha256:…", "instructions": "…" }
  ]
}
```

- `score` is the head's probability for that runbook against the query, not a cosine.
- `selected` is whether the classifier judges the runbook fits (its cardinality answer
  and admission floors applied). Follow selected runbooks; an empty selected set means
  no runbook really fits, and a failed search is an error, never an empty ranking. The
  ranking fills `max_results` when the catalog has that many, so lower hits are
  neighbours rather than answers.
- Only a selected hit carries the full document. An unselected neighbour is `id`,
  `title`, `score` and `selected: false` and nothing else: its procedure, and even
  its summary, would cost the caller context it will not use. `get_runbook` fetches
  one.
- `supporting_runbooks` is every selected runbook's instruction dependencies — its
  nearest domain foundation, then anything it declares in `requires`, recursively —
  once each, dependencies first, and never a runbook already selected (a foundation
  ranked as a neighbour still arrives here in full). With nothing selected it is
  empty. Read them before following the procedure. They are not ranked hits and do
  not count against `max_results`. A supporting runbook is `id`, `title`,
  `content_hash` and `instructions`: it is read because a delivered runbook needs
  it, so its routing text and tool lists would be paid for and never used.
- **Once per session.** A supporting runbook this session was already sent in full
  (by either tool, at the same `content_hash`) comes back as
  `{"id": "daintree.foundation", "note": "Sent in full earlier in this session; …"}`:
  the caller still has it in context, and resending it cost ~5 KB a search. A
  caller that no longer has it (compaction, a subagent sharing the connection)
  fetches it with `get_runbook`, which always returns the runbook it names in full.
  Only a 2025-era client has a session: measured on 2026-09-26, Codex 0.157
  (`2025-06-18`) keeps one per process, while Claude Code 2.1.283 negotiates
  `2026-07-28`, which has no sessions, so it is sent everything every time.

### Host model and host state

Every client is served the current direct-control catalog. The v0.44 inbox protocol
is retired; its procedures and captures are archived outside the package. Manually
configured MCP clients no longer default to inbox procedures.

Daintree sends `Daintree-Hosts` on each POST, from the snapshot taken when the help
session launches: `none`, `configured` (hosts exist, none connected with control on),
or `connected`. A missing, repeated or unknown value is unreported and filters
nothing. The old `Daintree-Host-Control` and `Daintree-Lane` headers are ignored;
current app builds may keep sending the former when talking to older servers.

Each hosts runbook declares `hostEligibility`: `any`, `configured` or `connected`.
Search applies the exclusion before retrieval, so eligible runbooks can occupy all
shortlist slots. Exact `get_runbook` retrieval ignores this search filter.

A caller reporting `connected` receives `daintree.hosts.foundation` alongside every
selected procedure, after `daintree.foundation`; a hosts procedure always brings
its own foundation. Supporting documents are deduplicated by content hash within
an MCP session. No host names, counts or ids are sent or logged.

## `get_runbook`

| argument | type | |
|---|---|---|
| `id` | string, required | an exact runbook id, as a runbook, search result or checkpoint gave it |

```json
{
  "catalog_revision": "sha256:…",
  "runbook": { "id": "daintree.agents.ask-and-relay", "content_hash": "sha256:…", "instructions": "…", "…": "…" },
  "supporting_runbooks": [ { "id": "daintree.foundation", "instructions": "…", "…": "…" } ]
}
```

Exact retrieval ranks nothing, so it carries no `score` or `selected`, and it never
touches the classifier. An unknown id is a tool error; so is the retired
`daintree.docs.answer-usage-question`, which is loaded only because the training
corpus names it and is never served by either tool. A caller that records
`content_hash` in its checkpoint can tell on resume whether the procedure it was
following has changed.

## Served fields

Both tools serve a runbook the caller will follow in the same form: `id`, `title`,
`summary`, `when_to_use`, `foundation`, `risk`, `required_tools`, `optional_tools`,
`requires`, `content_hash` and `instructions`; a selected search hit adds `score` and
`selected`. An unselected hit is only `id`, `title`, `score` and `selected`; a
supporting runbook is `id`, `title`, `content_hash` and `instructions`, or `id` and
`note` once the session has it. `when_to_use` stays on a selected hit on purpose: its
carve-outs are how a caller notices a selection that does not fit its task.

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

- **Both protocol eras, one endpoint.** `2026-07-28` clients call
  `server/discover` / `tools/list` / `tools/call` directly, with the version in the
  `MCP-Protocol-Version` header and `params._meta["io.modelcontextprotocol/protocolVersion"]`,
  and have no session. `2025-11-25`, `2025-06-18` and `2025-03-26` clients open with
  `initialize`, which is answered with a fresh `Mcp-Session-Id`.
  `ping` is answered; notifications and client responses get `202` with no body.
- **A session only saves bytes.** Its id keys one thing, in the memory of the
  instance that served the request: which supporting runbooks it was sent in full.
  No request is ever refused for its session id. A missing or malformed id is no
  session, and an id this instance never issued (another Cloud Run instance did, or
  this one restarted) is adopted with an empty record, so every gap errs toward
  resending. A reconnecting client initializes again and starts over. Records are
  bounded (least-recently-used past 4,096 sessions, and dropped after 12 idle hours),
  and a client cannot end one: `DELETE` is `405`, which the transport spec allows.
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
- **What is logged.** One `mcp_request` line per request, of bounded labels, counts and
  timings only: `rpc_method` (an unrecognised method is `other`), `status`, `tool`,
  `outcome` (`selected`, `none_selected`, `found`, `unknown_id`, `invalid_arguments`,
  `unavailable`, `unknown_tool`), `ranked` and `selected` counts for a search,
  `supporting_full` and `supporting_named`, the inference pool's verdict on a search
  (`inference`: `ok`, `pool_full`, `timeout`, `failed`, `closed`) with its
  `queue_wait_ms` and `rank_ms`, the response `bytes`, the coarse `hosts` state and
  `duration_ms`. Never a query, an id or name the caller sent, or a procedure's text.

The protocol lives in `src/daintree_assistant_server/runbook_mcp.py`; the HTTP
transport in `api/mcp.py`; the session record in `mcp_sessions.py`. `tests/integration/test_runbook_mcp.py` drives both eras
and both tools over HTTP.

## Other routes

| route | |
|---|---|
| `GET /health`, `GET /healthz` | liveness (`/healthz` is unreachable from outside Cloud Run) |
| `GET /readyz` | `503` until the current catalog and selector artifact have loaded; then `catalog_revision` and the number of searchable `runbooks` |
| `GET /version` | `server_version` and the `build_sha` the release stamped |
| `GET /` | redirect to the product page |
