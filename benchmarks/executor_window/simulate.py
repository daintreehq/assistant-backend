"""What the confirmation looks like on screen, second by second. Offline and free.

Answers the question a latency number cannot: does the typing rate read as a
natural response, and how much of the dead time does it actually cover?

    python -m benchmarks.executor_window.simulate
    python -m benchmarks.executor_window.simulate --window 1.26 --runbook daintree.ci.diagnose-and-fix

The default window is the MEASURED median from `bench.py` — request-in to the
executor's first delta of any kind. Everything here is arithmetic over that
number plus the shipped pacing config; nothing is called.
"""

from __future__ import annotations

import argparse

from daintree_assistant_server.config import (
    CONFIRMATION_LEAD_IN_SECONDS,
    CONFIRMATION_PACING_SECONDS,
)
from daintree_assistant_server.runbooks.catalog import load_catalog

#: Measured medians from `bench.py`, so the simulation and the benchmark cannot
#: drift apart silently.
WINDOW_P50 = 1.26
WINDOW_MIN = 0.98
WINDOW_P90 = 1.52

#: Measured separately: the executor does not trickle, it DUMPS. ~100 words in
#: ~0.33s over three trials. That is why the dead time is the whole experience —
#: there is no gradual arrival to blend into, so whatever fills the gap is the
#: only thing the reader has.
EXECUTOR_WORDS_PER_SEC = 287.0


def simulate(text: str, window: float, *, frames: int = 14) -> None:
    words = text.split()
    n = len(words)
    lead = CONFIRMATION_LEAD_IN_SECONDS
    pace = CONFIRMATION_PACING_SECONDS
    rate = (n - 1) / pace if n > 1 else 0.0
    total = lead + pace

    print(f"  text        {n} words, {len(text)} chars")
    print(f"  lead-in     {lead:.2f}s")
    print(f"  typing      {pace:.2f}s at {rate:.0f} words/sec ({1000 / rate:.0f} ms/word)")
    print(f"  finishes    {total:.2f}s")
    print(f"  window      {window:.2f}s (executor's first output)")
    covered = min(total, window) / window
    print(f"  COVERAGE    {covered:.0%} of the dead time is filled")
    if total > window:
        print(f"  overrun     {total - window:.2f}s — flushed in one event, answer not delayed")
    print()

    step = max(total, window) / frames
    print(f"  {'t':>6}  screen")
    print(f"  {'-' * 6}  {'-' * 62}")
    for i in range(frames + 1):
        t = i * step
        if t < lead:
            shown = ""
        else:
            k = n if pace <= 0 else min(n, int((t - lead) / pace * (n - 1)) + 1)
            shown = " ".join(words[:k])
        marker = ""
        if t >= window > t - step:
            marker = "   <- executor arrives"
        body = shown if len(shown) <= 60 else shown[:57] + "..."
        print(f"  {t:5.2f}s  {body:<62}{marker}")

    dump = 100 / EXECUTOR_WORDS_PER_SEC
    print(
        f"\n  For contrast: the executor delivers ~100 words in ~{dump:.2f}s once it "
        f"starts\n  ({EXECUTOR_WORDS_PER_SEC:.0f} words/sec, measured). It does not "
        "trickle — so the gap is\n  the whole wait, and the confirmation is the only "
        "thing in it."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runbook", default="daintree.orchestration.competition")
    ap.add_argument("--window", type=float, default=WINDOW_P50)
    args = ap.parse_args()

    runbook = load_catalog().get(args.runbook)
    if runbook is None or not runbook.confirmation:
        print(f"no confirmation on {args.runbook!r}")
        return 1
    print(f"\n== {args.runbook} ==\n")
    simulate(runbook.confirmation, args.window)
    print(f"\n== same text against the FASTEST window observed ({WINDOW_MIN}s) ==\n")
    simulate(runbook.confirmation, WINDOW_MIN, frames=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
