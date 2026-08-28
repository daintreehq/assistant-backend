# executor_window — how long the screen stays empty

**Mode: live.** Makes real, paid Baseten calls.

Answers one question: how long after `meta` does the executor stay silent? That
gap is what `config.CONFIRMATION_PACING_SECONDS` is allowed to fill.

```bash
python -m benchmarks.executor_window.bench                 # 9 trials
python -m benchmarks.executor_window.bench --trials 15
python -m benchmarks.executor_window.bench --gap 4.0       # if throttled
```

## Why this is not the `latency` suite

`latency` reports `ttft_s` — the first delta carrying **visible content**. On a
tool-calling turn there never is one, and a work request that loads a runbook —
which is *exactly* when a confirmation fires — is a tool-calling turn. That
scenario reports no `ttft_s` at all.

`first_delta_s` here is the first delta of **any** kind: content, reasoning, or
tool calls. That is when the executor stops being silent from the client's side,
and therefore the moment the confirmation must already have finished.

## Trials are spaced

`--gap` defaults to 3s. Back-to-back trials against the same key were observed
failing 6 of 9, and a throttled sample reads as a slow model — which would push
the pacing budget UP, in the direction that hurts.

## Reading it

The tail of the report is the part that matters:

```
  0.8 x p90       1.213s
  pacing budget   1.000s  (the 1.0s cap binds)
  headroom at min 0.080s (SAFE)
```

`headroom at min` compares the budget against the **fastest** window observed,
not the median. A negative number means the budget would have overrun a real
turn — which is survivable (the remaining words are flushed in one event) but
should be rare, so it is the number to tune against.

## Measured

GLM-5.3-Flash on Baseten, warm prefix cache, 24 clean trials over two runs:

| metric | value |
|---|---|
| `meta_s` p50 | 0.015s |
| `first_delta_s` p50 | 1.26s |
| `first_delta_s` p90 | 1.52s |
| `first_delta_s` min | 0.98s |

`0.8 × p90` is 1.22s, so the sub-second cap binds — and it has to, because the
fastest turn left only 0.98s. Every number here describes this model on this
host with a warm cache; re-measure after either changes.
