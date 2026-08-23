"""Live end-to-end latency over the REAL request paths.

Every number here comes from driving production classes against the real serving
endpoints — ``ChatOrchestrator.complete`` for turns, ``TaskRunner.run`` for utility
tasks, ``Selector.select`` for selection — with the captured CLI tool inventory and
a representative startup/runtime block. Nothing is stubbed, so a measured second is
a second a user would have waited.

WHAT IS TIMED, AND WHY THOSE SPLITS

A streaming turn is decomposed at the two boundaries the SSE contract already
exposes, which is exactly how the latency divides in practice:

- ``meta_s``  — request in → ``event: meta`` out. Selection + assembly. The
  orchestrator yields ``meta`` as soon as selection completes and BEFORE the
  upstream model is opened, so this isolates everything the server does on its own.
- ``ttft_s``  — request in → first ``delta`` carrying visible content. What the user
  actually experiences as "it started answering". Includes ``meta_s``, the upstream
  open, prompt prefill, and any reasoning the model emits before its first token.
- ``total_s`` — request in → ``done``.

``ttft_s`` is the headline. ``meta_s`` is the half this server controls outright;
``ttft_s - meta_s`` is the half it can only influence through prompt size, routing,
and thinking posture.

Non-streaming turns have no first byte, so only ``total_s`` is meaningful — that
case exists precisely to show what the streaming path is buying.

REPAIR RATE. ``Selector``/``TaskRunner`` sum usage across a parse-repair retry and
expose no attempt count, so a silently-repaired call is indistinguishable from a
clean one in the result object. Selector and task cases therefore run through
``CountingProvider``: ``calls > 1`` means the first output failed to parse and a
second round-trip was spent. A repair roughly doubles the call's latency, so a
scenario's p90 is often a repair rate in disguise.

TRIALS AND WARMUP. Each scenario runs ``--trials`` times with a discarded warmup
trial, because the very first call pays TLS + connection setup on a fresh httpx
client. Endpoint choice is recorded per trial, because a bimodal latency
distribution is usually two different endpoints rather than noise.

Do NOT read that recording as a sticky-session effect either way. The affinity
key these scenarios' calls compute is never SENT (docs/PROVIDER_POLICY.md
section 3a) — the serving host is one host with automatic prefix caching, so
there is no endpoint pool to pin and nothing steers a trial toward the endpoint
that served the last one. ``endpoint`` is reported by the provider or it is not;
a single-host provider generally reports nothing at all, which is why most runs
record ``None``. Scatter, when a name does arrive, is scatter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.main import build_container

from .._harness.cases import CaseError, load_yaml_cases
from .._harness.counting import CountingProvider
from .._harness.env import build_settings, caller_api_key, make_upstream_provider
from .._harness.fixtures import build_respond_request
from .._harness.stats import latency_summary, percentile

CASES_DIR = Path(__file__).resolve().parent / "cases"

# A turn is given a generous ceiling: the point is to MEASURE a slow tail, not to
# truncate it into a timeout that silently drops the worst samples from the stats.
_TURN_TIMEOUT_S = 180.0

_KINDS = {"turn", "task", "selector"}

# The turn id the priming turn mints. A `reuse_turn_id` scenario replays under it so
# the state token's last_turn_id matches and the selector cadence skips selection.
_PRIMED_TURN_ID = "primed-turn"


def load_scenarios(id_filter: str | None = None) -> list[dict[str, Any]]:
    scenarios = load_yaml_cases(CASES_DIR, id_filter)
    for s in scenarios:
        kind = s.get("kind")
        if kind not in _KINDS:
            raise CaseError(f"{s['id']}: kind must be one of {sorted(_KINDS)}, got {kind!r}")
        if kind == "task" and not s.get("task"):
            raise CaseError(f"{s['id']}: a task scenario needs a 'task' id")
        if kind in {"turn", "selector"} and not s.get("messages"):
            raise CaseError(f"{s['id']}: needs 'messages'")
    return scenarios


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def _iter_events(buffer: str) -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Split a raw SSE buffer into (event, data) pairs plus the unparsed remainder.

    The orchestrator emits ``event: <name>\\ndata: <json>\\n\\n`` and bare ``:``
    comment heartbeats. Heartbeats are dropped here rather than counted as events —
    they carry no payload and would otherwise register as a first byte that the CLI
    does not render, which is the whole thing ``ttft_s`` is trying to measure.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    while "\n\n" in buffer:
        block, buffer = buffer.split("\n\n", 1)
        event = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if not event or not data_lines:
            continue  # comment heartbeat or malformed block
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        out.append((event, payload))
    return out, buffer


def _delta_has_content(payload: dict[str, Any]) -> bool:
    """True when a ``delta`` carries VISIBLE text (not a tool-call fragment).

    Tool-call deltas are excluded deliberately: on a turn that opens with a tool
    call there is no prose for the user to read, and counting that fragment as the
    first byte would report a responsiveness the user never saw.
    """
    return bool(payload.get("content"))


# ---------------------------------------------------------------------------
# Scenario runners
# ---------------------------------------------------------------------------


async def _prime_state(
    container: Any, scenario: dict[str, Any], api_key: SecretStr
) -> str | None:
    """Run a throwaway first turn to mint a valid state token for a continuation.

    A continuation case is only meaningful against state the server itself signed
    for this session AND this catalog revision — a hand-made token would be rejected
    by ``verify`` and silently degrade the case back into a cold turn.
    """
    request = build_respond_request(
        messages=[{"role": "user", "content": scenario["messages"][0]["content"]}],
        session_id=f"lat-{scenario['id']}",
        turn_id=_PRIMED_TURN_ID,
        stream=False,
    )
    outcome = await container.orchestrator.complete(
        request, api_key=api_key
    )
    body = outcome.body or {}
    state = body.get("state")
    return state if isinstance(state, str) else None


async def _run_turn_trial(
    container: Any,
    scenario: dict[str, Any],
    state: str | None,
    turn_id: str,
    api_key: SecretStr,
) -> dict[str, Any]:
    stream = bool(scenario.get("stream", True))
    # A genuine tool-continuation ROUND reuses the turn's id — that is precisely what
    # `_should_run_selector` keys on to skip selection. Minting a fresh turn_id per
    # trial would silently turn this into a new-turn case and re-run the selector,
    # measuring the opposite of what the scenario claims.
    effective_turn_id = _PRIMED_TURN_ID if scenario.get("reuse_turn_id") else turn_id
    request = build_respond_request(
        messages=scenario["messages"],
        session_id=f"lat-{scenario['id']}",
        turn_id=effective_turn_id,
        stream=stream,
    )
    if state:
        # `state` is not a build_respond_request parameter; set it on the validated
        # model so the continuation cadence (skip the selector) is exercised.
        request = request.model_copy(update={"state": state})

    t0 = time.perf_counter()
    outcome = await container.orchestrator.complete(
        request, api_key=api_key
    )

    if not stream:
        total = time.perf_counter() - t0
        body = outcome.body or {}
        runbooks = body.get("runbooks") or {}
        selector = runbooks.get("selector") or {}
        usage = body.get("usage") or {}
        return {
            "ok": True,
            "meta_s": None,
            "ttft_s": None,
            "total_s": total,
            "selector_ran": bool(selector.get("ran")),
            "selector_degraded": bool(selector.get("degraded")),
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": usage.get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

    meta_s: float | None = None
    ttft_s: float | None = None
    selector_ran = False
    selector_degraded = False
    usage: dict[str, Any] = {}
    error: str | None = None
    buffer = ""

    assert outcome.stream is not None
    async for chunk in outcome.stream:
        buffer += chunk
        events, buffer = _iter_events(buffer)
        for event, payload in events:
            now = time.perf_counter() - t0
            if event == "meta" and meta_s is None:
                meta_s = now
                sel = (payload.get("runbooks") or {}).get("selector") or {}
                selector_ran = bool(sel.get("ran"))
                selector_degraded = bool(sel.get("degraded"))
            elif event == "delta" and ttft_s is None and _delta_has_content(payload):
                ttft_s = now
            elif event == "done":
                usage = payload.get("usage") or {}
            elif event == "error":
                error = str(payload.get("error", {}).get("code") or "error")

    total = time.perf_counter() - t0
    return {
        "ok": error is None,
        "error": error,
        "meta_s": meta_s,
        "ttft_s": ttft_s,
        "total_s": total,
        "selector_ran": selector_ran,
        "selector_degraded": selector_degraded,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": usage.get("cached_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


async def _run_task_trial(
    container: Any, counter: CountingProvider, scenario: dict[str, Any], api_key: SecretStr
) -> dict[str, Any]:
    counter.reset()
    # The case files say `data:` for readability; the wire field is `input`.
    request = TaskRequest.model_validate({"task": scenario["task"], "input": scenario["data"]})
    t0 = time.perf_counter()
    result = await container.task_runner.run(request, api_key=api_key)
    total = time.perf_counter() - t0
    first = counter.first_result
    return {
        "ok": True,
        "total_s": total,
        "calls": counter.complete_calls,
        "repaired": counter.complete_calls > 1,
        "endpoint": first.endpoint if first else None,
        "prompt_tokens": result.usage.prompt_tokens if result.usage else None,
        "completion_tokens": result.usage.completion_tokens if result.usage else None,
    }


async def _run_selector_trial(
    container: Any, counter: CountingProvider, scenario: dict[str, Any], api_key: SecretStr
) -> dict[str, Any]:
    counter.reset()
    conversation = [
        ChatMessage(role=m["role"], content=m["content"]) for m in scenario["messages"]
    ]
    t0 = time.perf_counter()
    decision = await container.selector.select(conversation, active_ids=[], api_key=api_key)
    total = time.perf_counter() - t0
    first = counter.first_result
    return {
        "ok": True,
        "total_s": total,
        "calls": counter.complete_calls,
        "repaired": counter.complete_calls > 1,
        "degraded": decision.degraded,
        "runbook_ids": list(decision.runbook_ids),
        "endpoint": first.endpoint if first else None,
        # finish_reason is the smoking gun for the truncation failure mode: a
        # `length` finish means the model burned its whole cap and the JSON was cut
        # off mid-object, which is a prompt problem, not a slow-endpoint problem.
        "finish_reason": first.finish_reason if first else None,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _summarize(scenario: dict[str, Any], trials: list[dict[str, Any]]) -> dict[str, Any]:
    good = [t for t in trials if t.get("ok")]
    out: dict[str, Any] = {
        "id": scenario["id"],
        "kind": scenario["kind"],
        "title": scenario.get("title", scenario["id"]),
        "trials": len(trials),
        "failures": len(trials) - len(good),
    }
    # Keep every raw trial. Percentiles over 9 samples are jumpy on a live endpoint —
    # one bad election shows up as a p90 several times the p50 — so a saved run must
    # stay re-analyzable after the fact (pool two runs, drop an outlier, check whether
    # a tail was one trial or a trend) without paying for the calls again.
    out["raw_trials"] = trials

    if not good:
        # Surface WHY, not just that it failed. A scenario that dies on a contract
        # mistake and one that dies on an upstream 429 look identical without this,
        # and a silently-empty row reads as "not measured" rather than "broken".
        errors = [t.get("error") for t in trials if t.get("error")]
        out["errors"] = sorted(set(errors))[:3]
        return out

    def block(key: str) -> dict[str, Any] | None:
        values = [t[key] for t in good if t.get(key) is not None]
        return latency_summary(values) if values else None

    for key in ("meta_s", "ttft_s", "total_s"):
        summary = block(key)
        if summary is not None:
            out[key] = summary

    calls = [t["calls"] for t in good if "calls" in t]
    if calls:
        out["repair_rate"] = round(sum(1 for c in calls if c > 1) / len(calls), 3)
        out["mean_calls"] = round(sum(calls) / len(calls), 2)
    finishes = [t.get("finish_reason") for t in good if t.get("finish_reason")]
    if finishes:
        out["finish_reasons"] = {f: finishes.count(f) for f in sorted(set(finishes))}
    endpoints = [t.get("endpoint") for t in good if t.get("endpoint")]
    if endpoints:
        out["endpoints"] = {e: endpoints.count(e) for e in sorted(set(endpoints))}
    ran = [t["selector_ran"] for t in good if "selector_ran" in t]
    if ran:
        out["selector_run_rate"] = round(sum(1 for r in ran if r) / len(ran), 3)
    degraded = [t.get("selector_degraded") or t.get("degraded") for t in good]
    if any(d is not None for d in degraded):
        out["degraded_rate"] = round(sum(1 for d in degraded if d) / len(degraded), 3)
    prompt = [t["prompt_tokens"] for t in good if t.get("prompt_tokens")]
    if prompt:
        out["prompt_tokens_p50"] = int(percentile(prompt, 50))
    cached = [t["cached_tokens"] for t in good if t.get("cached_tokens") is not None]
    if cached and prompt:
        out["cache_hit_ratio_p50"] = round(percentile(cached, 50) / percentile(prompt, 50), 3)
    completion = [t["completion_tokens"] for t in good if t.get("completion_tokens")]
    if completion:
        out["completion_tokens_p50"] = int(percentile(completion, 50))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_async(
    settings: Settings,
    scenarios: list[dict[str, Any]],
    *,
    trials: int,
    warmup: bool = True,
) -> dict[str, Any]:
    provider = make_upstream_provider(settings, timeout=_TURN_TIMEOUT_S)
    counter = CountingProvider(provider)
    # Wrap BEFORE the container is built so selector, task runner, and the main
    # stream all resolve to the counting decorator — it delegates byte-identically,
    # so the model sees exactly the production request either way.
    container = build_container(settings, counter)
    api_key = caller_api_key(settings)

    results: list[dict[str, Any]] = []
    try:
        for scenario in scenarios:
            kind = scenario["kind"]
            state: str | None = None
            if kind == "turn" and scenario.get("prime"):
                state = await _prime_state(container, scenario, api_key)

            trial_rows: list[dict[str, Any]] = []
            total_runs = trials + (1 if warmup else 0)
            for i in range(total_runs):
                try:
                    if kind == "turn":
                        row = await _run_turn_trial(
                            container, scenario, state, turn_id=f"t{i}", api_key=api_key
                        )
                    elif kind == "task":
                        row = await _run_task_trial(container, counter, scenario, api_key)
                    else:
                        row = await _run_selector_trial(container, counter, scenario, api_key)
                except Exception as exc:  # noqa: BLE001 — a failed trial is data
                    row = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                # The first run pays TLS + connection setup on a fresh client; it is
                # run (so the pool is warm) but never scored.
                if warmup and i == 0:
                    continue
                trial_rows.append(row)
            results.append(_summarize(scenario, trial_rows))
    finally:
        # The container's own teardown, not just the provider's: it owns a second
        # connection pool (the documentation MCP client) that closing the
        # provider alone would leave open.
        await container.aclose()

    return {"scenarios": results}


def run(
    settings: Settings, scenarios: list[dict[str, Any]], *, trials: int, warmup: bool = True
) -> dict[str, Any]:
    return asyncio.run(run_async(settings, scenarios, trials=trials, warmup=warmup))


def build_settings_for_bench(env_file: str | None = None) -> Settings:
    # Runbook learning must be OFF for a latency run: it fires after a final-answer
    # turn and would add a GPT-5.6 Sol call plus a catalog reload mid-benchmark,
    # mutating the very catalog the later scenarios select against.
    return build_settings(env_file, RUNBOOK_LEARNING_MODE="off")
