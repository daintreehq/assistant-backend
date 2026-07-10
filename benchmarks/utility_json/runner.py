"""Live benchmark for the two JSON utility tasks the accuracy suite skips.

``terminal_extract_json`` and ``memory_distill`` produce structured values (a
schema-shaped object; a list of facts) rather than a single enum/bool field, so they
do not fit the ``utility_tasks`` "expect a field == value" scorer (where a list means
an acceptable set). They run through the exact production ``TaskRunner`` path here —
including the ``result_schema`` conformance gate and the one repair retry — with the
provider wrapped in ``CountingProvider`` so a repaired result is separable from a
clean first-try one.

Scoring:
- extract_json — the ``result`` value equals the expected value (deep equality); an
  injection case asserts a directive planted in the untrusted tail does not steer it.
- memory_distill — phrase-anchored recall (each expected concept appears in some
  fact) + a false-capture guard (a forbidden ephemeral phrase must NOT appear).

Makes REAL, paid calls.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from daintree_assistant_server.config import DEEPSEEK_MODEL
from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.task_runner import build_profiles

from .._harness.cases import CaseError, load_yaml_cases
from .._harness.counting import CountingProvider
from .._harness.env import build_settings, make_deepseek_provider
from .._harness.stats import latency_summary

CASES_DIR = Path(__file__).resolve().parent / "cases"
SUPPORTED = {"terminal_extract_json", "memory_distill"}
_PROFILES = build_profiles()


def load_cases(id_filter: str | None = None) -> list[dict[str, Any]]:
    cases = load_yaml_cases(CASES_DIR, id_filter)
    for c in cases:
        if c.get("task") not in SUPPORTED:
            raise CaseError(f"{c['id']}: task must be one of {sorted(SUPPORTED)}")
        if not isinstance(c.get("input"), dict):
            raise CaseError(f"{c['id']}: 'input' must be a mapping")
        try:
            _PROFILES[c["task"]].input_model.model_validate(c["input"])
        except ValidationError as exc:
            raise CaseError(f"{c['id']}: invalid input: {exc.errors()[0].get('msg', '')}") from exc
    return cases


def _score_extract(output: dict[str, Any], expect: dict[str, Any]) -> tuple[bool, list[str]]:
    got = output.get("result")
    want = expect.get("result")
    if got == want:
        return True, []
    # Partial credit note: which keys diverged (deep-equality is the pass bar).
    notes = []
    if isinstance(got, dict) and isinstance(want, dict):
        for k, wv in want.items():
            if got.get(k) != wv:
                notes.append(f"{k}={got.get(k)!r}!={wv!r}")
    else:
        notes.append(f"result={got!r}!={want!r}")
    return False, notes


def _score_distill(output: dict[str, Any], case: dict[str, Any]) -> tuple[bool, list[str]]:
    facts = output.get("facts") or []
    blob = " ".join(str(f.get("fact", "")) for f in facts).lower()
    notes: list[str] = []
    for phrase in case.get("expect_recall", []) or []:
        if phrase.lower() not in blob:
            notes.append(f"missing:{phrase!r}")
    for phrase in case.get("forbid_recall", []) or []:
        if phrase.lower() in blob:
            notes.append(f"false-capture:{phrase!r}")
    return (not notes), notes


async def _run_case(
    runner: Any, provider: CountingProvider, case: dict[str, Any]
) -> dict[str, Any]:
    provider.reset()
    t0 = time.perf_counter()
    error: str | None = None
    output: dict[str, Any] = {}
    try:
        result = await runner.run(TaskRequest(
            task=case["task"], input=case["input"], result_schema=case.get("result_schema"),
        ))
        output = result.output
    except DaintreeError as exc:
        error = f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    latency = time.perf_counter() - t0

    passed, notes = False, ["errored"]
    if error is None:
        if case["task"] == "terminal_extract_json":
            passed, notes = _score_extract(output, case.get("expect") or {})
        else:
            passed, notes = _score_distill(output, case)
    return {
        "id": case["id"],
        "task": case["task"],
        "category": case.get("category", ""),
        "ok": error is None,
        "passed": passed and error is None,
        "first_try": error is None and provider.complete_calls == 1,
        "repaired": error is None and provider.complete_calls >= 2,
        "model_calls": provider.complete_calls,
        "notes": [] if passed else notes,
        "error": error,
        "latency_s": round(latency, 3),
        "output": output,
    }


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def frac(items: list[dict[str, Any]], pred: Any) -> float:
        return round(sum(1 for x in items if pred(x)) / len(items), 4) if items else 0.0

    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_task.setdefault(r["task"], []).append(r)
    tasks = {
        t: {
            "n": len(items),
            "pass_rate": frac(items, lambda x: x["passed"]),
            "first_try_rate": frac(items, lambda x: x["first_try"]),
            "repair_rate": frac(items, lambda x: x["repaired"]),
        }
        for t, items in by_task.items()
    }
    return {
        "cases_total": len(results),
        "pass_rate": frac(results, lambda x: x["passed"]),
        "repair_rate": frac(results, lambda x: x["repaired"]),
        "error_rate": frac(results, lambda x: not x["ok"]),
        "latency_s": latency_summary([r["latency_s"] for r in results]),
        "by_task": tasks,
        "metric_note": "first_try = exactly one provider call; repair = >=2 provider calls "
        "(a JSON/conformance repair OR a rare transport retry).",
    }


async def run_bench(
    *, trials: int = 1, id_filter: str | None = None, env_file: str | None = None
) -> dict[str, Any]:
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.observability import configure_logging

    configure_logging("WARNING")
    settings = build_settings(env_file)
    cases = load_cases(id_filter)
    inner = make_deepseek_provider(settings, timeout=settings.utility_timeout_seconds)
    provider = CountingProvider(inner)
    container = build_container(settings, provider=provider)
    runner = container.task_runner

    started_at = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    try:
        for _ in range(trials):
            for case in cases:
                results.append(await _run_case(runner, provider, case))
    finally:
        await inner.aclose()
    finished_at = datetime.now(UTC)

    return {
        "meta": {
            "benchmark": "utility_json",
            "mode": "live",
            "model": DEEPSEEK_MODEL,
            "trials": trials,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
        },
        "summary": _summarize(results),
        "cases": results,
    }
