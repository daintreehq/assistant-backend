# Prompt cost/cache flagship

The deterministic scoreboard for the whole cost/latency story, plus a live
cross-check. Drives the REAL `PromptAssembler.build_main_request` and the EXACT
bytes `OpenRouterProvider` would POST.

```bash
python -m benchmarks.prompt_cost.bench run              # deterministic (free, CI-able)
python -m benchmarks.prompt_cost.bench run --live        # + live cache/TTFB (paid)
python -m benchmarks.prompt_cost.bench run --live --affinity-ab   # + cache-affinity A/B (paid)
python -m benchmarks.prompt_cost.bench report results/<f>.json
```

## What it measures

**Deterministic (no key, no network):**

- **Cacheable-prefix stability per transition** — for `tool_continuation`,
  `runtime_context_change`, `startup_context_change`, `skill_topic_shift`, and
  `new_session`, the fraction
  of the `[system messages] + [tools]` region that stays a byte prefix of the next
  turn's. Target is **1.0** for all of them, topic-shift included (the layout's core
  promise — the ~21.5k-token tool region is fully reused). Since issue #39 the
  loaded-skills block is a user-role row *below* the schemas, so changing the active
  set cannot touch that region and the floor is gated rather than merely reported;
  `full_prefix` still drops from the skill row down, which is inherent.
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

**Cache-affinity A/B (`--live --affinity-ab`, issue #42):** runs a three-turn
session twice — once with **no** affinity key (the pre-change baseline, where
OpenRouter falls back to hashing the first system and first non-system message) and
once with a stable `session_id` (what the server sends today) — and reports endpoint
switches plus mean cache ratio after the first turn. The sequence **shifts the
active skill set between turns on purpose**: the skill block is the first non-system
message, so a shift is exactly what invalidates the default key while leaving the
invariant head above it reusable.

The two arms take **disjoint, run-randomized slices of the catalog**, and the
treatment key is fresh per run. That is what keeps arm order from deciding the
result *within* a run: the skill block is the only thing below the invariant head
that the default sticky key hashes, so disjoint slices mean neither arm can warm the
other's prefix or leave a sticky record the other inherits.

Across runs it is weaker, and worth knowing before you read a delta. The no-key
arm's key is OpenRouter's default — a hash of the base prompt and the skill block,
both fixed content — so the offset is the only freshness available and the catalog
affords only a couple of dozen positions; two runs can draw overlapping slices, and
served traffic may have used those blocks anyway. **A rerun is not guaranteed
cold**: space runs beyond the 10-minute sticky idle window if independence matters.
The invariant head above the skill block stays shared in every case, deliberately —
every Daintree request shares it, so both arms meeting it warm is the production
condition, not a confound.

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
> silently — this table claimed a 16-skill catalog and a 20.3 KB selector prompt
> (against a real 43 KB one)
> for long enough that a reviewer quoted it back as current. Regenerate with
> `python -m benchmarks.prompt_cost.bench run` (free, no key) and update the date
> when you do. The two figures a change is most likely to move — selector prompt
> bytes and catalog size — are separately gated by `benchmarks/baseline.json`
> with declared movement bands, which is the mechanism that actually catches
> drift; this table is for reading, not for gating.

Measured 2026-08-20, 39 skills / 77 tools (sizes are KiB). The catalog holds 39
but the selector prompt renders 38 — `daintree.docs.answer-usage-question` is
server-pinned off the turn's `docs_query` and hidden from the selector, so it
costs the selector nothing:

| metric | value |
|---|---|
| system+tools stable — continuation / runtime / startup / new-session | **1.0000** (enforced floor) |
| system+tools stable — topic-shift | **1.0000** (enforced floor) |
| `wire_encoder_matches_httpx` | true |
| total request | ~113.9 KB, ~29.0k proxy tok |
| tool schemas share of request | ~73.5 KB, **64.4%**, ~18.7k tok |
| base prompt | ~23.3 KB, 20.4%, ~5.9k tok |
| loaded skills (2 active) | ~11.8 KB, 10.3%, ~3.0k tok |
| skill selection | **local two-stage selector — 0 upstream bytes** (the ~49.0 KB selector prompt is retired to the test seam) |
| state token | 389–492 B (0–3 skills; 16 KiB cap) |

The tool region fell ~10% (81.6 → 73.5 KiB, ~20.9k → ~18.8k proxy tokens) when
the CLI dropped its `docs__*` family — the backend retrieves documentation itself
now, so the client no longer needs a docs tool or a second MCP connection. It is
still the largest single region of the request by a wide margin, and still the
largest remaining lever.

The base-prompt row is the one that moved most against the previous snapshot,
and it moved because the 2026-08-17 reading was already stale rather than because
anything shrank this week: the rules relocated into `daintree.foundation` had
landed before that table was written and were never re-measured into it.

Live rows below are **older and not re-measured here** (they cost money); treat
them as an order-of-magnitude reference only. Measured 2026-07-10 against the
then-current 18-skill catalog and 72-tool projection:

| metric (live, 2026-07-10, 18 skills / 72 tools) | value |
|---|---|
| warm tool-region cache ratio | ~0.996 |
| streaming TTFB p50 | ~0.63 s |
| composed first-byte p50 | ~2.2 s |

### Snapshot history

The 2026-07-10 deterministic figures (18-skill catalog, 72-tool projection) read
`tool schemas ~72 KB / 54% / ~18k tok`, `selector prompt ~6033 tok`, and
`topic-shift ~0.39`. Kept only as history — the catalog has since been
consolidated to 16 skills and the selector prompt rewritten, so those numbers are
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
