# Session-trace correctness

Deterministic (no network, no key). Drives the REAL `ChatOrchestrator.complete`
over scripted multi-turn traces using `build_container` (production wiring) with a
replay provider, feeding the response `state` token back each turn — so the
orchestrator's non-model spine is exercised exactly as in
production.

```bash
python -m benchmarks.session_trace.bench run
python -m benchmarks.session_trace.bench list      # validate traces, no run
python -m benchmarks.session_trace.bench report results/<f>.json
```

## Metrics

- **cadence_accuracy** — the selector runs on new-instruction turns and stays
  silent on continuation rounds (same `turn_id` + a valid state token).
- **active_set_accuracy** — topic-shift replacement, continuation reuse, and clear.
- **selector_run_rate** — selector runs / total turns; the paid first-byte cost
  lever every avoided run saves ~1.75 s of.
- **state_continuity_rate** — every turn returns a verifiable state token (the
  portable, stateless-safe skill state that keeps the backend Cloud Run-safe).

## Traces (`cases/*.yaml`)

Each turn scripts what the replayed selector and main model return and asserts the
orchestrator's behavior. Reuse a `turn_id` to model a continuation round (selector
must not run); bump it for a new instruction. `main` is `{tool_calls: [name]}` (a
continuation) or `{final: "text"}` (turn complete). Skill ids and tool names are
validated against the real catalog / tool inventory at load.

## Baseline (2026-08-08)

A fourth metric, `learn_trigger_accuracy`, was removed when skill learning was
deleted from the server. It had already stopped measuring anything — the spy was
being attached to an attribute the orchestrator no longer has — and sat at
0.1667, which is what turned CI red on `develop`.

3 traces / 6 turns, all green: cadence 1.0, active-set 1.0,
state continuity 1.0, selector_run_rate 0.83 (the one continuation round correctly
skips the selector). Expected active sets include the domain foundation
(`daintree.foundation`) auto-paired with each selected specific on authoritative
selector runs — unit admission per `_admit_with_domain_foundations`.
