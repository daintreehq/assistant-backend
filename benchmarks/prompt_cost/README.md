# Prompt cost/cache flagship

The deterministic scoreboard for the whole cost/latency story, plus a live
cross-check. Drives the REAL `PromptAssembler.build_main_request` and the EXACT
bytes the serving adapter would POST.

```bash
python -m benchmarks.prompt_cost.bench run              # deterministic (free, CI-able)
python -m benchmarks.prompt_cost.bench run --live        # + live cache/TTFB (paid)
python -m benchmarks.prompt_cost.bench run --live --topic-shift   # + topic-shift reuse (paid)
python -m benchmarks.prompt_cost.bench tools                 # per-tool schema cost (free)
python -m benchmarks.prompt_cost.bench report results/<f>.json
```

## Per-tool schema cost (`bench tools`)

A **report, never a gate**. Offline and free — it reads the pinned CLI inventory
fixture and the packaged catalog, and makes no network call.

Every other measurement of the tool region is an AGGREGATE (`tools_bytes`, the
`tools_*_bytes` log fields, the `system_tools_bytes` this benchmark gates). That
answers "how big is the tool region" and refuses the only question an author can
act on, which is *which tools*. This prints the array one row at a time: schema
bytes, description bytes, parameter bytes, and who declares it.

**Bytes are not the ranking.** A tool's cost is its size times how often it is
offered, so each row carries a computed **reach** — how many of the catalog's
runbooks would admit it — and rows sort by reach, then size. `tier` sits beside
that as the *reason*, not the ranking, because the labels do not line up with
exposure the way they look like they do:

| Tier | Offered on | Why |
|---|---|---|
| `always` | every request | `ALWAYS_AVAILABLE` — the permanent base prompt instructs it, so withholding it is a contradiction the model cannot resolve |
| `undeclared` | every request | projection argues from metadata and there is none about a tool the catalog has never heard of |
| `scaffold` | every runbook-bearing turn | `loaded_runbooks.md.j2` instructs it and no runbook declares it |
| `foundation` | every runbook-bearing turn (today) | admission pairs each selected specific with its domain foundation, and 38 of 39 runbooks point at one |
| `runbook` | turns that loaded a declaring runbook | reach varies from 1 to 20+ declarers, which a tier alone cannot express |

Two labels can mean identical exposure by opposite arguments (`always` and
`undeclared`; `scaffold` and `foundation`), and one tier spans an order of
magnitude on its own — which is why an earlier tier-ordered version put a tool
offered on *every* request below one that needs an active runbook.

**Reach is static, not observed.** It is the baseline projection over the
packaged catalog — how widely a tool *could* be admitted — and knows nothing
about which runbooks actually get selected. It also ignores the two per-request
admission paths (a forced `tool_choice`, an unresolved tool call), which depend
on a turn rather than on the catalog.

**Prose bytes, not description bytes.** `function.description` is only the top
of the editable surface; the inventory carries substantial prose under the
parameter schema's own nested `description` keys — 49.1 KiB of prose against
32.9 KiB of top-level descriptions. Ranking a prose audit on the top-level field
alone would hide a third of what it can actually edit.

The summary's **runbook-less enforced floor** is the serialized `always` +
`undeclared` subset — what an enforced assistant turn with no active runbook
proposes from this pinned inventory. Not a floor under literally every request:
a caller can offer fewer tools, and a shadow, disabled or fail-open turn sends
the whole inventory.

## What it measures

**Deterministic (no key, no network):**

- **Cacheable-prefix stability per transition** — for `tool_continuation`,
  `runtime_context_change`, `startup_context_change`, `runbook_topic_shift`, and
  `new_session`, the fraction
  of the `[system messages] + [tools]` region that stays a byte prefix of the next
  turn's. Target is **1.0** for all of them, topic-shift included (the layout's core
  promise — the ~21.5k-token tool region is fully reused). Since issue #39 the
  loaded-runbooks block is a user-role row *below* the schemas, so changing the active
  set cannot touch that region and the floor is gated rather than merely reported;
  `full_prefix` still drops from the runbook row down, which is inherent.
  Computed over the exact httpx wire bytes — the suite asserts its compact
  encoder equals `httpx`'s (`wire_encoder_matches_httpx`), so it can never measure
  a body production doesn't send.
- **Per-layer byte/token budget** — where the request's size lives. `token_est` is
  a **bytes/4 proxy** (no tokenizer in the repo), under-counts the JSON tool
  schemas, and is **trend-only** — never on the same axis as a live `prompt_tokens`.
- **Selector prompt cost** and **state-token size** vs active-set size (tiny; the
  portable token keeps the backend stateless / Cloud Run-safe).

**Live (`--live`, real paid DeepSeek):**

- **Warm-turn cache reuse** — sends an identical request twice; reads
  `cached_tokens/prompt_tokens` on the **second (warm)** call. DeepSeek's prefix
  cache is shared and server-side, so a first call is never truly "cold" — only the
  warm call is a clean signal. This is the number that validates the deterministic
  proxy end-to-end.
- **Streaming TTFB** and **composed first-byte** through the real orchestrator
  (selector → assembly → main stream) — the latency a selector-firing turn actually
  costs the user.

**Topic-shift reuse (`--live --topic-shift`):** runs one three-turn session that
**shifts the active runbook set between turns on purpose**. The runbook block is the
first non-system message, so a shift invalidates everything below it while the
invariant head above — base prompt, response contract, tool schemas — stays
perfectly reusable. It reports mean cache ratio after the first turn, plus
endpoint switches where the host reports an endpoint at all (this one does not,
so expect `n/a`).

**This replaced a two-arm cache-affinity A/B (issue #42), which measured
nothing.** That experiment ran the sequence twice — once setting the affinity
key, once leaving it unset — and reported the delta. It made sense against a
router, where the key decided which of many endpoints answered. It does not make
sense here: the adapter **drops** the key under both vendors' spellings
(`docs/PROVIDER_POLICY.md` section 3a), so both arms POSTed byte-identical bodies
and the delta was run order wearing a measurement's clothes. Keeping it would
have spent paid calls re-proving what
`tests/unit/test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent`
asserts for free.

Read the remaining number with one limit in mind. Together matches prefixes
**org-wide**, so any traffic from this account — another bench, a dev server, a
real turn — can have warmed the same base prompt and runbook blocks already. The
run-randomized catalog offset is the only freshness available and the catalog
affords a couple of dozen positions, so **a rerun is not guaranteed cold**: space
runs beyond the cache TTL if independence matters. The invariant head stays
shared in every case, deliberately — every Daintree request shares it, so meeting
it warm is the production condition, not a confound.

One run is still indicative rather than significant — re-run before concluding.
Endpoint switch rate is diagnostic only: an endpoint is many GPU workers, so the
same name is not the same cache, and the count reports `n/a` when any endpoint went
unreported. See `docs/PROVIDER_POLICY.md` section 3a.

## Latest deterministic snapshot

These are a **snapshot, not a contract**. Every absolute number below moves with
the catalog, the base prompt, and the captured CLI tool projection, so each is
stamped with what it was measured against. Only the *stability* rows are
invariants the suite enforces; the size rows are descriptive. Re-run
`python -m benchmarks.prompt_cost.bench run` and refresh this table rather than
reasoning from a stale figure.

> **These are a dated snapshot, not a live reading.** Hand-copied numbers decay
> silently — this table claimed a 16-runbook catalog and a 20.3 KB selector prompt
> (against a real 43 KB one)
> for long enough that a reviewer quoted it back as current. Regenerate with
> `python -m benchmarks.prompt_cost.bench run` (free, no key) and update the date
> when you do. The two figures a change is most likely to move — selector prompt
> bytes and catalog size — are separately gated by `benchmarks/baseline.json`
> with declared movement bands, which is the mechanism that actually catches
> drift; this table is for reading, not for gating.

Measured 2026-09-09, 39 runbooks / 98 tools (sizes are KiB). The catalog holds 39
but the selector prompt renders 38 — `daintree.docs.answer-usage-question` is
server-pinned off the turn's `docs_query` and hidden from the selector, so it
costs the selector nothing:

| metric | value |
|---|---|
| system+tools stable — continuation / runtime / startup / new-session | **1.0000** (enforced floor) |
| system+tools stable — topic-shift | **1.0000** (enforced floor) |
| `wire_encoder_matches_httpx` | true |
| total request | ~140.5 KiB, ~35.8k proxy tok |
| tool schemas share of request | ~96.5 KiB, **68.7%**, ~24.6k tok |
| base prompt | ~24.9 KiB, 17.7%, ~6.3k tok |
| loaded runbooks (2 active) | ~12.1 KiB, 8.6%, ~3.1k tok |
| runbook selection | **local two-stage selector — 0 upstream bytes** (the ~49.0 KB selector prompt is retired to the test seam) |
| state token | 392–503 B (0–3 runbooks; 16 KiB cap) |

The current capture pins CLI `240f0d2`, including `forge.getPRs`, its batch PR read and the `terminal.revealOwned` wrapper. Agent capability entries stay dynamically discoverable through the existing dispatcher, so individual commands add no permanent tool schemas; the deterministic cache-stability checks still pass.

The tool region rose previously, 88.2 → 94.8 KiB (~22.5k → ~24.2k proxy tokens), when the
pin moved to `272b13c` — the CLI's account-authentication line. **No tool definition
changed at all** in that step: `internal/tools/` is byte-identical across the two
commits, and what moved was which tools the capture SELECTS. `cmd/tooldump` had gated
the seven `workflow__*` execution-graph tools behind an opt-in flag while the CLI
itself registers them unless `DAINTREE_WORKFLOW_INTELLIGENCE=0`, so the dump described
itself as "the projection a normal launch sends" and was not one; its default now
matches. The seven were therefore always in a real request and were missing from this
measurement, which makes this less a rise than a correction — and a reminder that
diffing tool definitions between two pins says nothing about which of them ship.

Before it, 79.3 → 88.2 KiB when the pin moved to `2b38799` — the CLI's protocol-3
runbooks line. That capture added ten tools and removed none: `agentSessionHistory__list`, `browser__getConsoleMessages`,
`daintree__invoke`, `errors__recent`, `forge__listIssueComments`, `notifications__recent`,
`project__detectRunners`, `project__runCheck`, `tool__schema` and
`worktree__resource__status`. Most of them are typed wrappers for MCP actions the
runbooks previously reached through `daintree.call`, so the request got bigger in
exchange for those calls no longer needing a system-risk confirmation. The two
preceding steps were `cb12688` (73.5 → 79.3 KiB, adding `subagent__run` and
`terminal__moveToWorktree`) and, before it, the ~10% fall when the CLI dropped its
`docs__*` family because the backend retrieves documentation itself now. The region
remains the largest single part of the request by a wide margin, and the largest
remaining lever: runbook-aware tool projection (`runbooks/tool_projection.py`, still
in shadow mode) would cut it to ~54.1 KiB on this fixture, 43% off.

The base-prompt row is the one that moved most against the previous snapshot,
and it moved because the 2026-08-17 reading was already stale rather than because
anything shrank this week: the rules relocated into `daintree.foundation` had
landed before that table was written and were never re-measured into it.

Live rows below are **older and not re-measured here** (they cost money); treat
them as an order-of-magnitude reference only. Measured 2026-07-10 against the
then-current 18-runbook catalog and 72-tool projection:

| metric (live, 2026-07-10, 18 runbooks / 72 tools) | value |
|---|---|
| warm tool-region cache ratio | ~0.996 |
| streaming TTFB p50 | ~0.63 s |
| composed first-byte p50 | ~2.2 s |

### Snapshot history

The 2026-07-10 deterministic figures (18-runbook catalog, 72-tool projection) read
`tool schemas ~72 KB / 54% / ~18k tok`, `selector prompt ~6033 tok`, and
`topic-shift ~0.39`. Kept only as history — the catalog has since been
consolidated to 16 runbooks and the selector prompt rewritten, so those numbers are
**not** evidence about the current system. The deterministic half was also
**broken from 2026-07-11 to 2026-07-28** (a `render_integrations` arity mismatch
raised `TypeError` on every run and took the `dashboard/` roll-up down with it),
so nothing between those dates was measured at all.

## Notes

- Token numbers are a bytes/4 proxy; the live `cached_tokens`/`prompt_tokens` are
  the only true tokens. Keep the two on separate axes.
- The fixture `_harness/fixtures/cli_tool_inventory.json` is the captured tool
  projection the Go CLI sends (`(*tools.Registry).OpenAITools(nil)`). The CLI is a
  pinned submodule CI never fetches, so it goes stale silently — regeneration
  recipe in `_harness/fixtures/README.md`.
- The required `startup` snapshot is measured as its own stable user-role layer
  before the visible conversation. Runtime/worktree changes remain confined to the
  trailing volatile block, so startup does not change the system+tools stability model.
