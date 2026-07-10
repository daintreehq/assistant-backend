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
  `runtime_context_change`, `skill_topic_shift`, and `new_session`, the fraction
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

## Baseline (2026-07-10, deepseek-v4-flash, 72-tool CLI inventory)

| metric | value |
|---|---|
| system+tools stable — continuation / runtime / new-session | **1.0000** |
| system+tools stable — topic-shift (expected drop) | ~0.39 |
| tool schemas share of request | ~72 KB, **54%**, ~18k tok |
| selector prompt | ~6033 tok / 18 skills |
| state token | 389–492 B (0–3 skills; 16 KiB cap) |
| **live** warm tool-region cache ratio | **~0.996** |
| **live** streaming TTFB p50 | ~0.63 s |
| **live** composed first-byte p50 | ~2.2 s |

## Notes

- Token numbers are a bytes/4 proxy; the live `cached_tokens`/`prompt_tokens` are
  the only true tokens. Keep the two on separate axes.
- The fixture `_harness/fixtures/cli_tool_inventory.json` is the real 72-tool
  projection the Go CLI sends (`Registry.OpenAITools(nil)`); regenerate it if the
  CLI's tool surface changes.
- Built against committed `main`. When the in-flight `StartupContext` layer lands
  (a stable user-role snapshot before the conversation), add it as a segmented
  layer here; it does not change the cache-stability model (it sits in the stable
  region).
