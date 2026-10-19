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
| `query` | string, 1–8,192 chars, required | the outcome wanted, in natural language — "ask one agent a question and bring back its answer", not keywords, not a transcript, terminal output or secrets — keeping how the user wants it done (several agents, a time limit, a running agent, a review loop), which decides the procedure |
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

Daintree tells the server which way it drives its Remote Hosts and what Remote Host
setup the caller has, in request headers sent on **every** POST, from a snapshot taken
when the assistant launches:

| header | values |
|---|---|
| `Daintree-Host-Control` | `1` from a build that controls its hosts directly (`host.list`, `host.call`) and delegates to a host's assistant as to any agent. Absent (v0.44) or anything else: the inbox model v0.44 shipped |
| `Daintree-Hosts` | `none` (no Remote Host configured), `configured` (at least one, none connected with control on), `connected` (at least one connected with control on) |
| `Daintree-Lane` | inbox callers only: `reachable` when another machine's assistant can message this caller (it is a lane); omit it otherwise |

**The model picks the catalog.** The server holds one catalog per model, and every
answer to a request comes from its caller's: the search, `get_runbook`,
`supporting_runbooks` and `catalog_revision`. `Daintree-Host-Control: 1` is the
control catalog. Anything else, a missing header included, is the inbox catalog: the
v0.44 hosts runbooks, frozen, and the copies of each shared runbook whose text differs
between the models. So a v0.44 client never sees `host.call`, and a later one never
sees an inbox tool. The model fails closed to inbox, unlike the host state, because a
value this server does not know is no evidence of a client that has `host.call`. A
control caller's `Daintree-Lane` is not read: it has no lanes.

**The host state narrows the search.** Each hosts runbook declares the state it fits
as `hostEligibility`: `connected`, `configured` (configured or connected, so recovery
stays offered while every host is down), `lane` (inbox only: follows `Daintree-Lane`,
not the caller's own hosts) or `any`. A search leaves out the runbooks the reported
state does not fit **before the shortlist**, so the rest of the catalog ranks as if
they were not there. Missing `Daintree-Hosts`, an unknown value, or an inbox caller's
`Daintree-Lane` other than `reachable` is an unreported caller, which is offered its
whole catalog, never an error. `get_runbook` ignores the state: it narrows what a
search offers, not what can be read by id.

**A connected control caller works on another machine.** A control caller reporting
`Daintree-Hosts: connected` gets `daintree.hosts.foundation` (how to work on another
machine) in `supporting_runbooks` with every runbook it is sent in full, not only the
hosts ones, since any task may be one whose work is on a host. It follows
`daintree.foundation` and comes before anything the runbook `requires`, and is sent
once per session like any supporting runbook. No other caller gets it attached; a
hosts runbook brings it as its domain foundation for every caller.

Only the coarse state leaves the machine: no host names, counts or ids. It is read
from each request and never kept between requests (the request log records the model
and the coarse value as metadata). The session record is by content hash, so a
runbook the two models word differently is sent in full again if one session spans
both.

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

The protocol lives in `src/daintree_assistant_server/runbook_mcp.py`; the HTTP
transport in `api/mcp.py`; the session record in `mcp_sessions.py`. `tests/integration/test_runbook_mcp.py` drives both eras
and both tools over HTTP.

## Other routes

| route | |
|---|---|
| `GET /health`, `GET /healthz` | liveness (`/healthz` is unreachable from outside Cloud Run) |
| `GET /readyz` | `503` until both catalogs and the selector artifact have loaded; then each catalog's revision and number of searchable runbooks (`catalog_revision`/`runbooks` for the control catalog, `inbox_catalog_revision`/`inbox_runbooks` for the inbox one) |
| `GET /version` | `server_version` and the `build_sha` the release stamped |
| `GET /` | redirect to the product page |
