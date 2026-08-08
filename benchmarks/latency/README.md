# latency — live end-to-end speed

**Mode: live.** Makes real, paid OpenRouter calls. This is the suite that answers
"how long does a user actually wait?", measured over the real request paths with the
captured CLI tool inventory — not a stub.

```bash
python -m benchmarks.latency.bench run                       # all scenarios, 5 trials
python -m benchmarks.latency.bench run --trials 9 --label baseline
python -m benchmarks.latency.bench run --scenario cold_      # id prefix filter
python -m benchmarks.latency.bench compare results/A.json results/B.json
```

## What it measures

A streaming turn is split at the two boundaries the SSE contract already exposes:

| metric | boundary | who owns it |
|---|---|---|
| `meta_s` | request in → `event: meta` | **this server** — selection + assembly, before the model is opened |
| `ttft_s` | request in → first `delta` with visible content | the headline; what the user calls "it started answering" |
| `total_s` | request in → `done` | full generation |

`ttft_s − meta_s` is the part the server only influences indirectly: upstream open,
prompt prefill, and any reasoning emitted before the first visible token.

## Scenarios

| id | shape |
|---|---|
| `cold_greeting` | first turn of a session, no state, trivial message — the worst first-byte case |
| `cold_work_request` | first turn where the selector has real routing work to do |
| `warm_continuation` | tool-result round under the **same** `turn_id` — selector must not run |
| `warm_new_turn` | follow-up instruction with valid state — selector runs, speculation eligible |
| `cold_greeting_nonstream` | no first-byte escape hatch; shows what streaming buys |
| `task_*` | utility tasks (`watcher_classify`, `terminal_summarize`, `terminal_extract_text`) |
| `selector_*` | the selector in isolation, including its `repair_rate` |

## Reading the numbers

- **`repair_rate`** is the metric the turn cases cannot see. `Selector`/`TaskRunner`
  sum usage across a parse-repair retry and expose no attempt count, so a silently
  repaired call is byte-indistinguishable from a clean one. The suite wraps the
  provider in `CountingProvider`; `calls > 1` means a second round-trip was spent. A
  repair roughly doubles the call, so a bad p90 is often a repair rate in disguise.
- **`finish_reasons`** — a `length` count on a selector scenario means the model burned
  its whole token cap and the JSON was cut off mid-object. That is a prompt problem,
  not a slow endpoint.
- **`endpoints`** — OpenRouter re-elects per request across ~24 endpoints. A bimodal
  distribution is usually two different endpoints, not noise, so the endpoint that
  served each trial is recorded.
- **A discarded warmup trial** runs first: the very first call pays TLS + connection
  setup on a fresh httpx client.

### Cache-warmth caveat

Trials within a scenario replay a byte-identical request, so the upstream prefix cache
runs hotter (`cache_hit_ratio_p50` near 1.0) than a genuinely cold first-ever session
would. This is not as artificial as it looks — the base prompt and the ~18k-token tool
schema region are shared by *every* session in production, so a busy endpoint really
does hold them. But treat the absolute `cold_*` numbers as a warm-endpoint floor, and
the **baseline-vs-improved comparison** as the trustworthy output: both runs use the
identical protocol, so the delta is real either way.

Skill learning is forced `off` for a run — it fires after a final-answer turn and would
otherwise add a GPT-5.6 Sol call and a catalog reload mid-benchmark, mutating the very
catalog later scenarios select against.
