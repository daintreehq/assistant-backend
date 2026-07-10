"""Tiny stats helpers (no numpy). Percentiles use nearest-rank on sorted data."""

from __future__ import annotations

import math
from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def pstdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (pct in [0, 100]). Empty → 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 100:
        return ordered[-1]
    rank = math.ceil(pct / 100 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def latency_summary(latencies_s: Sequence[float]) -> dict[str, float]:
    """The p50/p90/p99/mean block every latency-bearing suite reports."""
    return {
        "count": len(latencies_s),
        "mean_s": round(mean(latencies_s), 4),
        "p50_s": round(percentile(latencies_s, 50), 4),
        "p90_s": round(percentile(latencies_s, 90), 4),
        "p99_s": round(percentile(latencies_s, 99), 4),
        "max_s": round(max(latencies_s), 4) if latencies_s else 0.0,
    }
