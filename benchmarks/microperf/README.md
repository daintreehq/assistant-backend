# microperf — deterministic startup + concurrency + CPU-throughput

The pure-CPU / boot numbers that gate Cloud Run cold start and the per-request CPU
floor. **100% deterministic**: no API key, no network — the only "provider" is an
in-process fake that answers instantly, so every measured millisecond is the server's
own work. Safe to run anywhere, free, repeatable.

```bash
python -m benchmarks.microperf.bench run                        # full suite
python -m benchmarks.microperf.bench run --concurrency 64       # heavier probe
python -m benchmarks.microperf.bench run --iters 50000          # longer codec loop
python -m benchmarks.microperf.bench run --no-save
python -m benchmarks.microperf.bench report results/<run>.json
```

## What it measures

1. **Cold start.** `import daintree_assistant_server` plus the app module
   (`.main` — FastAPI, providers, services, the module-level app) in a **fresh
   subprocess** per run, so import cost is real rather than module-cache-warm. Then,
   in-process, `build_container(settings)` — which validates every prompt and loads
   the skill catalog during construction. Together these bound the instance-boot
   part of a Cloud Run cold start.
2. **Catalog build + determinism.** `load_catalog()` latency, and a check that two
   builds produce the identical `revision` hash — prompt-cache identity and
   skill-state validation both ride that determinism.
3. **State-codec throughput.** `StateCodec.issue()` / `verify()` ops/sec over a
   fixed key/salt. Every `/respond` verifies the inbound token and issues a fresh
   one, so this is a per-request floor component (HMAC + canonical JSON + pydantic).
4. **Concurrency under load — the important one.** The REAL
   `ChatOrchestrator.complete` (non-streaming), built via the production
   `build_container` with the fake provider injected, driven (a) sequentially and
   (b) K-way via `asyncio.gather` at a few levels (8, K, 2K; K from
   `--concurrency`, default 32). Requests use the captured 72-tool CLI inventory and
   distinct session/turn ids, so every round runs the full selector → assemble →
   respond path: the selector prompt render over the live catalog, the unmemoized
   `base_prompt()` re-read, the ~18k-token tool-schema normalization walk, and
   response assembly. Concurrent latency is measured **from submission to
   completion**, so it includes event-loop queueing behind the other requests' CPU;
   `p99_inflation_ratio = concurrent_p99 / single_call_p50` is the stall/contention
   proxy. This is a stateless-server probe — there is no shared mutable state, so a
   regression that adds per-request sync I/O or shared state shows up here as p99
   inflation.
5. **Readiness (light).** `/healthz` and `/readyz` over the real app via FastAPI's
   TestClient (fake provider injected); both should be 200 with a valid `.env`.

## Reading the numbers

- `import_ms` / `build_container_ms` — instance boot; watch the median trend.
- `revision_deterministic` must stay `true`; a `false` means the catalog hash became
  input-order- or environment-dependent and cache identity is broken.
- `p99_inflation_ratio` grows roughly with K on a single event loop (CPU work
  serializes); a jump at fixed K is the regression signal, not the absolute value.
- The suite forces `SKILL_LEARNING_MODE=off` and configures logging at WARNING so
  background learning passes and INFO log rendering never contaminate the probe.

Results land in `results/` (gitignored); the JSON document (`meta` + `summary` +
per-section detail) is the record.
