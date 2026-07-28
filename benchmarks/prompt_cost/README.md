# Prompt cost/cache flagship

The deterministic scoreboard for the whole cost/latency story, plus a live
cross-check. Drives the REAL `PromptAssembler.build_main_request` and the EXACT
bytes `DeepSeekProvider` would POST.

```bash
python -m benchmarks.prompt_cost.bench run              # deterministic (free, CI-able)
python -m benchmarks.prompt_cost.bench run --live        # + live cache/TTFB (paid)
python -m benchmarks.prompt_cost.bench report results/<f>.json
```

## What it measures

**Deterministic (no key, no network):**

- **Cacheable-prefix stability per transition** — for `tool_continuation`,
  `runtime_context_change`, `startup_context_change`, `skill_topic_shift`, and
  `new_session`, the fraction
  of the `[system messages] + [tools]` region that stays a byte prefix of the next
  turn's. Target is **1.0** for the three continuity transitions (the layout's core
  promise — the ~18k-token tool region is fully reused); a topic-shift is an
  **expected** drop (only base+integrations stay stable) and is reported, not
  failed. Computed over the exact httpx wire bytes — the suite asserts its compact
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

## Latest deterministic snapshot

These are a **snapshot, not a contract**. Every absolute number below moves with
the catalog, the base prompt, and the captured CLI tool projection, so each is
stamped with what it was measured against. Only the *stability* rows are
invariants the suite enforces; the size rows are descriptive. Re-run
`python -m benchmarks.prompt_cost.bench run` and refresh this table rather than
reasoning from a stale figure.

Measured **2026-07-28**, `deepseek-v4-flash`, **16-skill catalog**
(`catalog_revision sha256:4b56003…`), **85-tool** CLI projection:

| metric | value |
|---|---|
| system+tools stable — continuation / runtime / startup / new-session | **1.0000** (enforced floor) |
| system+tools stable — topic-shift (expected drop) | 0.2718 |
| `wire_encoder_matches_httpx` | true |
| total request | ~122 KB, ~30.3k proxy tok |
| tool schemas share of request | ~76.7 KB, **63.0%**, ~19.1k tok |
| base prompt | ~28.8 KB, 23.6%, ~7.1k tok |
| loaded skills (2 active) | ~11.9 KB, 9.8%, ~2.9k tok |
| selector prompt | ~20.3 KB, ~5040 tok / 16 skills (~770 B per skill) |
| state token | 389–492 B (0–3 skills; 16 KiB cap) |

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
