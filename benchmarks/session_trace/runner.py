"""Drive the REAL ``ChatOrchestrator.complete`` over scripted multi-turn traces.

Uses ``build_container`` (the exact production wiring) with a replay provider and a
recording learner spy, feeding the response ``state`` token back each turn so the
selector cadence, active-set resolution, and learning trigger are exercised exactly
as they run in production — with no network and no paid call. Numbers produced:

- ``cadence_accuracy`` — did the selector run on the turns it should (new
  instruction) and stay silent on continuation rounds?
- ``active_set_accuracy`` — topic-shift replacement / continuation reuse / clear.
- ``learn_trigger_accuracy`` — the learner fires ONLY on a final-answer round.
- ``selector_run_rate`` — selector runs / total turns (the first-byte cost lever).
- ``state_continuity_rate`` — every turn returns a verifiable state token.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daintree_assistant_server.config import OPENROUTER_MODEL
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.skills.catalog import load_catalog

from .._harness.cases import CaseError, load_yaml_cases
from .._harness.env import build_settings
from .._harness.fixtures import load_tool_inventory, tool_names
from . import replay as R

CASES_DIR = Path(__file__).resolve().parent / "cases"


def _validate_trace(trace: dict[str, Any], skill_ids: set[str], tools: set[str]) -> None:
    if not trace.get("turns"):
        raise CaseError(f"{trace['id']}: trace has no turns")
    for i, turn in enumerate(trace["turns"]):
        for sid in turn.get("selector_pick", []) or []:
            if sid not in skill_ids:
                raise CaseError(f"{trace['id']} turn {i}: unknown skill id {sid!r}")
        for sid in (turn.get("expect", {}).get("active") or []):
            if sid not in skill_ids:
                raise CaseError(f"{trace['id']} turn {i}: unknown expect.active id {sid!r}")
        main = turn.get("main") or {}
        for name in main.get("tool_calls", []) or []:
            if name not in tools:
                raise CaseError(f"{trace['id']} turn {i}: unknown tool {name!r}")
        if "main" not in turn or not ({"final", "tool_calls"} & set(main)):
            raise CaseError(f"{trace['id']} turn {i}: main must have 'final' or 'tool_calls'")


def load_traces(id_filter: str | None = None) -> list[dict[str, Any]]:
    catalog = load_catalog()
    skill_ids = {s.id for s in catalog.skills}
    tools = set(tool_names())
    traces = load_yaml_cases(CASES_DIR, id_filter)
    for t in traces:
        _validate_trace(t, skill_ids, tools)
    return traces


def _request(
    messages: list[dict[str, Any]], state: str | None, session_id: str, turn_id: str
) -> RespondRequest:
    payload: dict[str, Any] = {
        "protocol_version": 2,
        "session": {"id": session_id, "turn_id": turn_id, "instruction_revision": 0},
        "input": {"messages": messages, "tools": load_tool_inventory()},
        "startup": {},
        "generation": {"stream": False},
    }
    if state:
        payload["state"] = state
    return RespondRequest.model_validate(payload)


async def _run_trace(orchestrator: Any, spy: R.SpyLearner, provider: R.ReplayProvider,
                     codec: Any, trace: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    state: str | None = None
    pending_call: dict[str, str] | None = None  # {id, name}
    turn_results: list[dict[str, Any]] = []
    session_id = trace["session_id"]

    for idx, turn in enumerate(trace["turns"]):
        if turn.get("user"):
            messages.append({"role": "user", "content": turn["user"]})
        if turn.get("tool_result") is not None:
            assert pending_call is not None, "tool_result with no pending tool call"
            messages.append({
                "role": "tool", "tool_call_id": pending_call["id"],
                "content": turn["tool_result"],
            })
            pending_call = None

        main = turn["main"]
        provider.next_selector = turn.get("selector_pick", []) or []
        if "tool_calls" in main:
            name = main["tool_calls"][0]
            call_id = f"call_{trace['id']}_{idx}"
            provider.next_main = R.main_tool_call(name, "{}", call_id)
        else:
            provider.next_main = R.main_final(main["final"])

        runs_before = len(spy.runs)
        sel_before, main_before = provider.selector_calls, provider.main_calls
        req = _request(list(messages), state, session_id, turn["turn_id"])
        outcome = await orchestrator.complete(req, upstream_model=OPENROUTER_MODEL)
        body = outcome.body or {}
        sel_delta = provider.selector_calls - sel_before
        main_delta = provider.main_calls - main_before
        # Learning is fired-and-forgotten via asyncio.create_task; yield the loop so
        # the (instant) spy.run executes before we read whether it fired this turn.
        for _ in range(3):
            await asyncio.sleep(0)

        # Append what the (replayed) model produced, so the next round's transcript
        # is well-formed (assistant tool_call → tool result, or a final answer).
        if "tool_calls" in main:
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": main["tool_calls"][0], "arguments": "{}"},
                }],
            })
            pending_call = {"id": call_id, "name": main["tool_calls"][0]}
        else:
            messages.append({"role": "assistant", "content": main["final"]})

        got_active = [s["id"] for s in body.get("skills", {}).get("active", [])]
        selector_ran = bool(body.get("skills", {}).get("selector", {}).get("ran"))
        learn_fired = len(spy.runs) > runs_before
        new_state = body.get("state")
        # The returned state token must actually verify (session-bound), and carry the
        # active set forward — a nonempty-but-forged/unverifiable token is not continuity.
        verification = codec.verify(new_state, session_id=session_id)
        state_ids = verification.payload.active_ids() if verification.payload else []
        state = new_state

        exp = turn.get("expect", {})
        expect_selector = exp.get("selector_ran")
        checks = {
            "cadence": selector_ran == expect_selector,
            # The selector provider was actually hit exactly when cadence says it ran.
            "selector_calls": sel_delta == (1 if expect_selector else 0),
            "main_called_once": main_delta == 1,
            "active_set": set(got_active) == set(exp.get("active", [])),
            "learn": learn_fired == exp.get("learn"),
            # The state token verifies AND round-trips the active set (not just nonempty).
            "state_verifies": verification.valid and set(state_ids) == set(got_active),
        }
        turn_results.append({
            "turn_id": turn["turn_id"],
            "selector_ran": selector_ran,
            "active": got_active,
            "learn_fired": learn_fired,
            "checks": checks,
            "passed": all(checks.values()),
            "expect": exp,
        })
    return {"id": trace["id"], "turns": turn_results,
            "passed": all(t["passed"] for t in turn_results)}


def _summ(traces: list[dict[str, Any]]) -> dict[str, Any]:
    all_turns = [t for tr in traces for t in tr["turns"]]
    n = len(all_turns)

    def rate(key: str) -> float:
        return round(sum(t["checks"][key] for t in all_turns) / n, 4) if n else 0.0

    selector_runs = sum(t["selector_ran"] for t in all_turns)
    return {
        "traces_total": len(traces),
        "traces_passed": sum(t["passed"] for t in traces),
        "turns_total": n,
        "turns_passed": sum(t["passed"] for t in all_turns),
        "cadence_accuracy": rate("cadence"),
        "selector_call_accuracy": rate("selector_calls"),
        "main_call_accuracy": rate("main_called_once"),
        "active_set_accuracy": rate("active_set"),
        "learn_trigger_accuracy": rate("learn"),
        "state_continuity_rate": rate("state_verifies"),
        "selector_run_rate": round(selector_runs / n, 4) if n else 0.0,
    }


async def run_bench(id_filter: str | None = None, env_file: str | None = None) -> dict[str, Any]:
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.observability import configure_logging

    configure_logging("WARNING")  # keep the benchmark's stdout to the report only
    settings = build_settings(env_file)
    traces = load_traces(id_filter)
    provider = R.ReplayProvider()
    container = build_container(settings, provider=provider)
    spy = R.SpyLearner()
    container.orchestrator.learner = spy  # inject the recording spy (default is None)

    results = []
    try:
        for trace in traces:
            results.append(await _run_trace(
                container.orchestrator, spy, provider, container.state_codec, trace))
        await container.orchestrator.drain_background_tasks(timeout=5.0)
    finally:
        await provider.aclose()

    return {
        "meta": {
            "benchmark": "session_trace",
            "mode": "deterministic",
            "model": OPENROUTER_MODEL,
            "catalog_revision": load_catalog().revision,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        "summary": _summ(results),
        "traces": results,
    }
