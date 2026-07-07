"""Trial scoring and suite aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cases import BenchCase


@dataclass(frozen=True)
class TrialResult:
    case_id: str
    trial: int
    picked: list[str]
    missing: list[str]
    extra: list[str]
    degraded: bool
    latency_s: float
    confidence: float | None
    task_type: str | None
    reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def passed(self) -> bool:
        return not self.degraded and not self.missing and not self.extra


def score_trial(
    case: BenchCase,
    trial: int,
    picked: list[str],
    *,
    degraded: bool,
    latency_s: float,
    confidence: float | None,
    task_type: str | None,
    reason: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> TrialResult:
    picked_set = set(picked)
    return TrialResult(
        case_id=case.id,
        trial=trial,
        picked=list(picked),
        missing=sorted(set(case.required) - picked_set),
        extra=sorted(picked_set - case.allowed),
        degraded=degraded,
        latency_s=latency_s,
        confidence=confidence,
        task_type=task_type,
        reason=reason,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def summarize(cases: list[BenchCase], results: list[TrialResult]) -> dict[str, Any]:
    by_case: dict[str, list[TrialResult]] = {c.id: [] for c in cases}
    for r in results:
        by_case[r.case_id].append(r)

    case_pass_all = {cid: bool(trials) and all(t.passed for t in trials)
                     for cid, trials in by_case.items()}
    categories: dict[str, dict[str, int]] = {}
    for case in cases:
        cat = categories.setdefault(case.category, {"cases": 0, "passed": 0, "trials": 0,
                                                    "trials_passed": 0})
        trials = by_case[case.id]
        cat["cases"] += 1
        cat["passed"] += int(case_pass_all[case.id])
        cat["trials"] += len(trials)
        cat["trials_passed"] += sum(t.passed for t in trials)

    latencies = sorted(r.latency_s for r in results)
    completion = [r.completion_tokens for r in results if r.completion_tokens is not None]
    prompt = [r.prompt_tokens for r in results if r.prompt_tokens is not None]
    return {
        "cases_total": len(cases),
        "cases_passed": sum(case_pass_all.values()),
        "trials_total": len(results),
        "trials_passed": sum(r.passed for r in results),
        "degraded_trials": sum(r.degraded for r in results),
        "recall_miss_trials": sum(bool(r.missing) for r in results),
        "precision_extra_trials": sum(bool(r.extra) for r in results),
        "latency_s": {
            "mean": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(latencies, 50), 3),
            "p90": round(percentile(latencies, 90), 3),
            "p99": round(percentile(latencies, 99), 3),
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
        "tokens": {
            "prompt_mean": round(sum(prompt) / len(prompt), 1) if prompt else None,
            "completion_mean": round(sum(completion) / len(completion), 1)
            if completion else None,
        },
        "categories": categories,
    }
