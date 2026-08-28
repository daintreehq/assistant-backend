"""How long the screen stays empty after `meta` — the confirmation's pacing budget.

**Mode: live.** Makes real, paid Baseten calls.

The runbook confirmation is streamed word by word to fill the gap between `meta`
and the executor's first output. That gap is what this measures, and it is NOT
the same metric the `latency` suite reports:

* ``latency``'s ``ttft_s`` is the first delta carrying VISIBLE CONTENT. On a
  tool-calling turn there never is one, so the scenario that matters most here —
  a work request that loads a runbook, which is exactly when a confirmation
  fires — reports no ``ttft_s`` at all.
* ``first_delta_s`` here is the first delta of ANY kind: content, reasoning, or
  tool calls. That is when the executor stops being silent from the client's
  side, and therefore the moment the confirmation must already have finished.

Pacing past this number would push the confirmation's own words into the
executor's output. Pacing far short of it leaves the gap it was meant to fill.

    python -m benchmarks.executor_window.bench                 # 9 trials
    python -m benchmarks.executor_window.bench --trials 15
    python -m benchmarks.executor_window.bench --gap 4.0       # avoid throttling

Trials are SPACED by default. Back-to-back calls against the same key were
observed failing 6 of 9 trials, and a throttled sample reads as a slow model.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from typing import Any

from daintree_assistant_server.main import build_container

from .._harness.env import build_settings, caller_api_key, make_upstream_provider
from .._harness.fixtures import build_respond_request
from .._harness.stats import percentile

#: A first turn whose request genuinely routes to a runbook. The confirmation only
#: fires on a high-confidence FIRST load, so a greeting — which loads nothing —
#: would measure a window the feature never occupies.
MESSAGE = (
    "Start work on issue 412 — the checkout totals are wrong on multi-currency "
    "carts. Get a branch going."
)


def _events(buffer: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    out: list[tuple[str, dict[str, Any]]] = []
    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            with contextlib.suppress(json.JSONDecodeError):
                out.append((name, json.loads(data)))
    return out, buffer


async def _trial(container: Any, api_key: Any, turn_id: str) -> dict[str, Any]:
    request = build_respond_request(
        messages=[{"role": "user", "content": MESSAGE}],
        session_id=f"win-{turn_id}",
        turn_id=turn_id,
        stream=True,
    )
    t0 = time.perf_counter()
    outcome = await container.orchestrator.complete(request, api_key=api_key)
    meta_s: float | None = None
    first_delta_s: float | None = None
    error: str | None = None
    buffer = ""
    assert outcome.stream is not None
    async for chunk in outcome.stream:
        buffer += chunk
        events, buffer = _events(buffer)
        for name, payload in events:
            now = time.perf_counter() - t0
            if name == "meta" and meta_s is None:
                meta_s = now
            elif name == "delta" and first_delta_s is None:
                first_delta_s = now
            elif name == "error":
                error = str((payload.get("error") or {}).get("code") or "error")
    return {
        "ok": error is None and first_delta_s is not None,
        "error": error,
        "meta_s": meta_s,
        "first_delta_s": first_delta_s,
        "total_s": time.perf_counter() - t0,
    }


async def run(trials: int, gap: float) -> int:
    settings = build_settings()
    provider = make_upstream_provider(settings)
    container = build_container(settings, provider=provider)
    api_key = caller_api_key(settings)

    rows: list[dict[str, Any]] = []
    for i in range(trials):
        if i:
            # Spaced deliberately; see the module docstring.
            await asyncio.sleep(gap)
        rows.append(await _trial(container, api_key, f"w{i}"))
        mark = "ok" if rows[-1]["ok"] else f"FAIL {rows[-1]['error']}"
        window = rows[-1]["first_delta_s"]
        shown = window if window is None else round(window, 3)
        print(f"  trial {i + 1:>2}: {mark:<12} first_delta={shown}")

    good = [r["first_delta_s"] for r in rows if r["ok"] and r["first_delta_s"]]
    metas = [r["meta_s"] for r in rows if r["meta_s"]]
    failures = len(rows) - len(good)
    if not good:
        print("\nevery trial failed — nothing to report")
        return 1

    p50 = percentile(good, 50)
    p90 = percentile(good, 90)
    budget = min(0.8 * p90, 1.0)
    print(
        f"\n== executor window ({len(good)}/{trials} clean"
        f"{f', {failures} failed' if failures else ''}) ==\n"
        f"  meta_s          p50 {percentile(metas, 50):.3f}s\n"
        f"  first_delta_s   p50 {p50:.3f}s   p90 {p90:.3f}s   "
        f"min {min(good):.3f}s   max {max(good):.3f}s\n"
        f"\n  0.8 x p90       {0.8 * p90:.3f}s\n"
        f"  pacing budget   {budget:.3f}s  "
        f"({'the 1.0s cap binds' if 0.8 * p90 > 1.0 else 'derived from p90'})\n"
        f"  headroom at min {min(good) - budget:.3f}s "
        f"({'SAFE' if min(good) > budget else 'TOO SLOW — pacing would overrun'})"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=9)
    ap.add_argument("--gap", type=float, default=3.0, help="seconds between trials")
    args = ap.parse_args()
    return asyncio.run(run(args.trials, args.gap))


if __name__ == "__main__":
    raise SystemExit(main())
