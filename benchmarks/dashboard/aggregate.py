"""Run every sub-suite and merge them into one dashboard document.

The deterministic suites (free, CI-able) always run; the live suites run only when
a DeepSeek key is present and live mode is requested. Each suite is isolated: if one
raises, its slot records the error and the rest still run — the dashboard is a
best-effort roll-up, never all-or-nothing.

A suite entry is ``(name, mode, thunk)`` where ``thunk(settings, ctx)`` returns that
suite's results doc (with ``meta`` + ``summary``). The dashboard keeps each suite's
own ``summary`` verbatim and layers a small ``headline`` of the few numbers worth a
single glance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .._harness.env import build_settings, require_api_key
from .._harness.quality import VERDICTS

# ---- suite adapters --------------------------------------------------------
# Each returns a full suite results doc. Imports are deferred inside the thunks so a
# broken/absent suite never stops the dashboard from importing.


def _prompt_cost_det(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..prompt_cost.deterministic import run_deterministic

    return run_deterministic(settings)


def _safety(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..safety.runner import run_suite

    return run_suite(settings)


def _microperf(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..microperf.perf import BenchConfig, run_bench

    return run_bench(BenchConfig(env_file=ctx.get("env_file")))


def _session_trace(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..session_trace.runner import run_bench

    return asyncio.run(run_bench(env_file=ctx.get("env_file")))


def _prompt_cost_live(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..prompt_cost.live import run_live

    return asyncio.run(run_live(settings, trials=ctx.get("live_trials", 2)))


def _workflow_intel(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..workflow_intel.runner import run_bench

    return asyncio.run(run_bench(trials=ctx.get("live_trials", 1), env_file=ctx.get("env_file")))


def _injection_live(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..injection_live.runner import run_bench

    return asyncio.run(run_bench(trials=ctx.get("live_trials", 3), env_file=ctx.get("env_file")))


def _utility_json(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    from ..utility_json.runner import run_bench

    return asyncio.run(run_bench(trials=ctx.get("live_trials", 1), env_file=ctx.get("env_file")))


def _skill_selection_scale(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """Does the shortlist still hold the answer at 200 skills? ~20s, free.

    The live catalog is 38 skills against k=32, so every recall number the other
    suites report is 84%-of-the-catalog arithmetic rather than evidence that
    retrieval filters. This pads the catalog with derived distractors and
    re-measures. 200 rather than the full sweep's 1,000 because a thousand skill
    documents cost ~220s to embed, which is not a per-push price.
    """
    from ..skill_selection.scale import smoke

    return smoke()


def _skill_selection_local(settings: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """The selector that ships, scored offline. Free, deterministic, gated.

    The release authority for routing quality. It drives the exported ONNX
    artifact through the production decision procedure and reports which STAGE
    lost each missing skill, so a candidate-recall regression — the one that
    arrives first as the catalog grows — is separable from a head regression.
    """
    from daintree_assistant_server.skills.catalog import load_catalog

    from ..skill_selection.cases import load_cases
    from ..skill_selection.coverage import print_coverage
    from ..skill_selection.local_runner import run_local

    catalog = load_catalog()
    cases = load_cases(catalog)
    # Same free preflight the paid row runs, and it matters more here: this row
    # is a release gate, and a skill nobody wrote a case for is invisible to it
    # by construction.
    rows = print_coverage(catalog, cases)
    if not rows or not all(r.gate_ok for r in rows):
        gaps = [r.skill_id for r in rows if not r.gate_ok]
        raise RuntimeError(
            "skill-selection routing coverage has gaps, so no run can qualify the "
            f"catalog: {', '.join(gaps) or '<no skills>'}. "
            "Run `python -m benchmarks.skill_selection.bench coverage`."
        )
    return run_local(catalog=catalog, cases=cases)

DETERMINISTIC: list[tuple[str, Callable[..., dict[str, Any]]]] = [
    ("prompt_cost", _prompt_cost_det),
    ("safety", _safety),
    ("microperf", _microperf),
    ("session_trace", _session_trace),
    # THE routing-quality gate. Deterministic and free because selection is
    # local: there is no reason for the number a release is judged on to cost
    # money or to wait for a nightly.
    ("skill_selection_local", _skill_selection_local),
    ("skill_selection_scale", _skill_selection_scale),
]
#: Suites that are REPORTED but do not decide whether a run passed.
#:
#: EMPTY, and kept as the concept rather than deleted with its one member. That
#: member was the prompt-based selector comparator — reported but not voting,
#: because a regression in a component nobody runs must not block a release. The
#: comparator is gone with the selector it compared, and the distinction it
#: needed is exactly the one a future paid or flaky suite will need again.
INFORMATIONAL: frozenset[str] = frozenset()

LIVE: list[tuple[str, Callable[..., dict[str, Any]]]] = [
    ("prompt_cost_live", _prompt_cost_live),
    ("workflow_intel", _workflow_intel),
    ("injection_live", _injection_live),
    ("utility_json", _utility_json),
]


# ---- headline extraction (the few numbers worth one glance) ----------------

def _passed(name: str, s: dict[str, Any]) -> bool:
    """A semantic pass per suite, so a regression (not just an exception) reddens
    the dashboard.

    The three live suites now carry their own ``summary.passed``, stamped by
    ``_harness.quality`` — the same verdict their standalone CLI exits on, so the
    dashboard and the command can never disagree about whether a run was
    acceptable. They are handled by the first branch here.

    They used to be graded in this function on ``error_rate == 0`` (did anything
    502?) and, for injection, ``counter_over_refusal_rate == 0`` (did it answer the
    benign controls?). None of that looked at obedience, canary leakage, or
    forbidden tool calls: a model could fail every hostile case in the corpus and
    this printed `ok`.
    """
    if "passed" in s:
        return bool(s["passed"])
    if name == "prompt_cost":
        return bool(s.get("passed", s.get("wire_encoder_matches_httpx")))
    if name == "safety":
        return s.get("false_accept_total") == 0
    if name == "session_trace":
        return s.get("turns_passed") == s.get("turns_total")
    # `name in VERDICTS` is reached only if a live suite somehow returned a summary
    # with no verdict stamped — a wiring break, and it fails rather than defaulting
    # to green. An ungraded security suite is not a passing one. Everything else
    # (microperf / prompt_cost_live) carries its own verdict or is informational.
    return name not in VERDICTS


def _headline(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    s = doc.get("summary", {})
    try:
        if name == "prompt_cost":
            return {
                "min_system_tools_stable": s.get("min_system_tools_stable_continuity"),
                "wire_matches_httpx": s.get("wire_encoder_matches_httpx"),
            }
        if name == "safety":
            return {"false_accept_total": s.get("false_accept_total")}
        if name == "microperf":
            return {k: s.get(k) for k in
                    ("import_ms_median", "build_container_ms_median", "p99_inflation_ratio")
                    if k in s}
        if name == "session_trace":
            return {k: s.get(k) for k in
                    ("cadence_accuracy", "active_set_accuracy", "state_continuity_rate")}
        if name == "prompt_cost_live":
            return {"warm_tool_region_cache_ratio": s.get("warm_tool_region_cache_ratio"),
                    "composed_first_byte_p50_s": s.get("composed_first_byte_p50_s")}
        if name == "workflow_intel":
            return {"first_try_valid_rate": s.get("first_try_valid_rate"),
                    "error_rate": s.get("error_rate")}
        if name == "injection_live":
            return {"injection_obey_rate": s.get("injection_obey_rate")}
        if name == "utility_json":
            return {"pass_rate": s.get("pass_rate"), "repair_rate": s.get("repair_rate")}
        if name == "skill_selection_scale":
            return {
                "size": s.get("size"),
                # A count, not a rate: the denominator is 154 target-bearing
                # cases and one of them is 0.65 points.
                "candidate_misses": s.get("candidate_misses"),
                # What dense retrieval alone keeps of the actives a turn SHOULD
                # keep. Not "every active" — that counts topic shifts, where
                # dropping the old runbook is the right answer.
                "dense_retained": s.get("dense_retained"),
            }
        if name == "skill_selection_local":
            spans = s.get("spans_ms") or {}
            total = spans.get("total_ms") or {}
            return {
                "recall_pass": f"{s.get('recall_pass_cases')}/{s.get('cases_total')}",
                # Beside the headline on purpose: it is the stage no other suite
                # can see, and the first one to move as the catalog grows.
                "candidate_miss": s.get("candidate_miss_trials"),
                "p90_ms": total.get("p90"),
            }
    except Exception:  # noqa: BLE001 — a headline is best-effort
        return {}
    return {}


def run_dashboard(
    *,
    include_live: bool,
    env_file: str | None = None,
    live_trials: int | None = None,
    require_live: bool = False,
) -> dict[str, Any]:
    settings = build_settings(env_file)
    ctx: dict[str, Any] = {"env_file": env_file}
    if live_trials is not None:
        ctx["live_trials"] = live_trials

    suites = list(DETERMINISTIC)
    live_note = None
    live_missing = None
    if include_live:
        try:
            require_api_key(settings)
            suites += LIVE
        except Exception as exc:  # noqa: BLE001
            live_missing = str(exc)
            live_note = f"live suites skipped: {exc}"

    results: dict[str, Any] = {}
    for name, thunk in suites:
        try:
            doc = thunk(settings, ctx)
            summary = doc.get("summary", {})
            results[name] = {
                "ok": True,
                "passed": _passed(name, summary),
                "meta": doc.get("meta", {}),
                "summary": summary,
                "headline": _headline(name, doc),
            }
        except Exception as exc:  # noqa: BLE001 — one suite's failure must not sink the roll-up
            results[name] = {"ok": False, "passed": False,
                             "error": f"{type(exc).__name__}: {exc}"}

    if live_missing and require_live:
        # A release qualification that cannot reach the model has not qualified
        # anything. The convenient "skip without a key" behaviour is right for the
        # everyday dashboard and wrong here, where the absence of evidence would
        # otherwise be recorded as a green run.
        for name, _ in LIVE:
            results[name] = {
                "ok": False,
                "passed": False,
                "error": f"live run required but unavailable: {live_missing}",
            }

    return {
        "meta": {
            "benchmark": "dashboard",
            "mode": "full" if include_live else "deterministic-only",
            "require_live": require_live,
            "suites_run": list(results),
            "live_note": live_note,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "suites": results,
    }
