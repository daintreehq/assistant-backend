"""Drive the REAL workflow-intelligence tasks against live DeepSeek.

The three tasks (``workflow_plan`` / ``workflow_reconcile`` / ``workflow_resume_digest``)
run through the exact production ``TaskRunner`` path — the real profiles, the real
semantic validators (``validate_plan``/``validate_reconcile``: id-uniqueness, DAG-ness,
terminal-status immutability, tool grounding), and the one repair retry. The provider
is wrapped in ``CountingProvider`` so we can separate a clean **first-try** structural
success from a **repaired** one (production sums usage across attempts and exposes no
``repaired`` flag, so this distinction is invisible in any saved artifact otherwise).

Metrics per task:
- ``first_try_valid_rate`` — parsed + passed the structural validators on the FIRST
  model call (a repair doubles latency and cost, so this is the direct prompt lever).
- ``repair_rate`` — needed the second (repair) call but then succeeded.
- ``error_rate`` (502) — still invalid after the repair.
- plan: node/edge counts vs the 20/40 caps; ``next_action_grounded`` (an ungrounded
  tool degrades to a warning, never a fake capability).
- resume: ``focus_accuracy`` — the workflow blocked on the user is the suggested focus.

Makes REAL, paid calls.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from daintree_assistant_server.config import CEREBRAS_MODEL
from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.task_runner import build_profiles

from .._harness.cases import CaseError, load_yaml_cases
from .._harness.counting import CountingProvider
from .._harness.env import build_settings, caller_api_key, make_upstream_provider
from .._harness.quality import attach_verdict
from .._harness.stats import latency_summary

CASES_DIR = Path(__file__).resolve().parent / "cases"
SUPPORTED = {"workflow_plan", "workflow_reconcile", "workflow_resume_digest"}
_PROFILES = build_profiles()


def load_cases(id_filter: str | None = None) -> list[dict[str, Any]]:
    cases = load_yaml_cases(CASES_DIR, id_filter)
    for c in cases:
        task = c.get("task")
        if task not in SUPPORTED:
            raise CaseError(
                f"{c['id']}: unsupported task {task!r} (want one of {sorted(SUPPORTED)})"
            )
        if not isinstance(c.get("input"), dict):
            raise CaseError(f"{c['id']}: 'input' must be a mapping")
        try:
            _PROFILES[task].input_model.model_validate(c["input"])
        except ValidationError as exc:
            raise CaseError(f"{c['id']}: invalid input: {exc.errors()[0].get('msg', '')}") from exc
    return cases


def _score_plan(
    output: dict[str, Any], expect: dict[str, Any], inventory: set[str]
) -> dict[str, Any]:
    nodes = output.get("nodes") or []
    edges = output.get("edges") or []
    na = output.get("next_action")
    warnings = output.get("warnings") or []
    # Grounded means the SURVIVING next_action names no tool outside the supplied
    # inventory. The validator nulls an ungrounded tool (degrade), so a next_action
    # that still carries a tool_name must have that name in the inventory.
    na_tool = na.get("tool_name") if isinstance(na, dict) else None
    grounded = na_tool is None or na_tool in inventory
    # Any node referencing a tool outside the inventory should have raised a warning.
    node_tools = {n.get("tool_name") for n in nodes if isinstance(n, dict) and n.get("tool_name")}
    hallucinated = sorted(t for t in node_tools if t and t not in inventory)
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "within_node_cap": len(nodes) <= expect.get("max_nodes", 20),
        "within_edge_cap": len(edges) <= 40,
        "next_action_grounded": grounded,
        "next_action_grounded_ok": (grounded == expect["next_action_grounded"])
        if "next_action_grounded" in expect else None,
        "hallucinated_node_tools": hallucinated,
        "tool_warnings": [w for w in warnings if "tool" in w.lower()],
    }


def _score_resume(output: dict[str, Any], expect: dict[str, Any]) -> dict[str, Any]:
    focus = output.get("suggested_focus_workflow_id")
    want = expect.get("suggested_focus_workflow_id")
    ranked = output.get("ranked") or []
    top1 = ranked[0].get("workflow_id") if ranked else None
    return {
        "suggested_focus": focus,
        "focus_correct": (focus == want) if want is not None else None,
        "top1": top1,
        "top1_correct": (top1 == want) if want is not None else None,
    }


async def _run_case(
    runner: Any, provider: CountingProvider, case: dict[str, Any], api_key: SecretStr
) -> dict[str, Any]:
    provider.reset()
    started = time.perf_counter()
    error: str | None = None
    output: dict[str, Any] = {}
    try:
        result = await runner.run(
            TaskRequest(task=case["task"], input=case["input"]), api_key=api_key
        )
        output = result.output
    except DaintreeError as exc:
        error = f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 — a crash is a scored failure, not a raise
        error = f"{type(exc).__name__}: {exc}"
    latency = time.perf_counter() - started

    calls = provider.complete_calls
    ok = error is None
    detail: dict[str, Any] = {}
    if ok and case["task"] == "workflow_plan":
        inv = {t.get("name") for t in case["input"].get("tool_inventory", []) if t.get("name")}
        detail = _score_plan(output, case.get("expect") or {}, inv)
    elif ok and case["task"] == "workflow_resume_digest":
        detail = _score_resume(output, case.get("expect") or {})
    return {
        "id": case["id"],
        "task": case["task"],
        "category": case.get("category", ""),
        "ok": ok,
        "first_try": ok and calls == 1,
        "repaired": ok and calls >= 2,
        "error": error,
        "model_calls": calls,
        "latency_s": round(latency, 3),
        "detail": detail,
    }


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_task.setdefault(r["task"], []).append(r)

    def frac(items: list[dict[str, Any]], pred: Any) -> float:
        return round(sum(1 for x in items if pred(x)) / len(items), 4) if items else 0.0

    tasks = {}
    for task, items in by_task.items():
        block = {
            "n": len(items),
            "first_try_valid_rate": frac(items, lambda x: x["first_try"]),
            "repair_rate": frac(items, lambda x: x["repaired"]),
            "error_rate": frac(items, lambda x: not x["ok"]),
        }
        if task == "workflow_resume_digest":
            scored = [x for x in items if x["detail"].get("focus_correct") is not None]
            block["focus_accuracy"] = frac(scored, lambda x: x["detail"]["focus_correct"])
        tasks[task] = block

    return {
        "cases_total": len(results),
        "first_try_valid_rate": frac(results, lambda x: x["first_try"]),
        "repair_rate": frac(results, lambda x: x["repaired"]),
        "error_rate": frac(results, lambda x: not x["ok"]),
        "latency_s": latency_summary([r["latency_s"] for r in results]),
        "by_task": tasks,
        "metric_note": "first_try = exactly one provider call; repair_rate = >=2 provider "
        "calls (a JSON/validation repair OR a rare transport retry); error_rate = task 502'd.",
    }


async def run_bench(
    *, trials: int = 1, id_filter: str | None = None, env_file: str | None = None,
) -> dict[str, Any]:
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.observability import configure_logging

    configure_logging("WARNING")
    settings = build_settings(env_file)
    # Two reads: what a full run WOULD be, and what this run will actually do.
    whole_corpus = load_cases()
    cases = load_cases(id_filter)

    inner = make_upstream_provider(settings, timeout=settings.utility_timeout_seconds)
    provider = CountingProvider(inner)
    container = build_container(settings, provider=provider)
    runner = container.task_runner

    started_at = datetime.now(UTC)
    # Counting is per-run, so the workflow cases run SEQUENTIALLY (concurrency=1 for
    # counting fidelity); trials repeat the corpus.
    results: list[dict[str, Any]] = []
    try:
        for _ in range(trials):
            for case in cases:
                results.append(
                    await _run_case(runner, provider, case, caller_api_key(settings))
                )
    finally:
        # The container's own teardown, not just the provider's: it owns a second
        # connection pool (the documentation MCP client) that closing the
        # provider alone would leave open.
        await container.aclose()
    finished_at = datetime.now(UTC)

    return attach_verdict({
        "meta": {
            "benchmark": "workflow_intel",
            "mode": "live",
            "model": CEREBRAS_MODEL,
            "trials": trials,
            # What a COMPLETE run looks like, so the quality gate can tell one
            # from a --filter'd or interrupted run rather than grading whatever
            # happened to execute. A floor picked as a constant would either sit
            # under the real corpus (and let a partial run pass) or drift above it
            # as cases are added (and go permanently red).
            #
            # From `whole_corpus`, NOT `cases`: measuring the filtered list would
            # let a run declare its own subset to be the corpus and pass for
            # covering it — a self-certifying expectation, i.e. none at all.
            "cases_expected": len(whole_corpus) * trials,
            "tasks_expected": sorted({c["task"] for c in whole_corpus}),
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
        },
        "summary": _summarize(results),
        "cases": results,
    }, "workflow_intel")
