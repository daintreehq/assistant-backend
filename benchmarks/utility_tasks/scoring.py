"""Trial scoring and suite aggregation for the utility-task accuracy suite."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cases import TaskCase, mismatches


@dataclass(frozen=True)
class TrialResult:
    case_id: str
    task: str
    category: str
    trial: int
    output: dict[str, Any]
    mismatch: list[str]
    error: str | None
    latency_s: float
    confidence: float | None
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def passed(self) -> bool:
        return self.error is None and not self.mismatch


def score_trial(
    case: TaskCase,
    trial: int,
    output: dict[str, Any],
    *,
    error: str | None,
    latency_s: float,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> TrialResult:
    conf = output.get("confidence")
    return TrialResult(
        case_id=case.id,
        task=case.task,
        category=case.category,
        trial=trial,
        output=output,
        mismatch=[] if error else mismatches(output, case.expect),
        error=error,
        latency_s=latency_s,
        confidence=float(conf) if isinstance(conf, int | float) else None,
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


def _group(
    results: list[TrialResult], key: str
) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = {}
    for r in results:
        name = getattr(r, key)
        g = groups.setdefault(name, {"trials": 0, "trials_passed": 0, "errors": 0})
        g["trials"] += 1
        g["trials_passed"] += int(r.passed)
        g["errors"] += int(r.error is not None)
    return groups


def summarize(cases: list[TaskCase], results: list[TrialResult]) -> dict[str, Any]:
    by_case: dict[str, list[TrialResult]] = {c.id: [] for c in cases}
    for r in results:
        by_case[r.case_id].append(r)
    case_pass_all = {
        cid: bool(trials) and all(t.passed for t in trials) for cid, trials in by_case.items()
    }

    latencies = sorted(r.latency_s for r in results)
    completion = [r.completion_tokens for r in results if r.completion_tokens is not None]
    prompt = [r.prompt_tokens for r in results if r.prompt_tokens is not None]
    return {
        "cases_total": len(cases),
        "cases_passed": sum(case_pass_all.values()),
        "trials_total": len(results),
        "trials_passed": sum(r.passed for r in results),
        "error_trials": sum(r.error is not None for r in results),
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
            if completion
            else None,
        },
        "by_task": _group(results, "task"),
        "by_category": _group(results, "category"),
    }
