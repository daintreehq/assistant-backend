"""Scripted engine, deterministic grading and the actual backend response loop.

No tool is executed. Live mode spends model tokens only; replay mode establishes
harness correctness, never model quality. Assertions grade observable actions;
the final report's semantic accuracy remains an explicit human-review item.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.decision import SelectionDecision

from .._harness.cases import CaseError, load_yaml_cases
from .._harness.fixtures import load_tool_inventory
from ..session_trace.replay import _result

CASES_DIR = Path(__file__).parent / "cases"
SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in load_tool_inventory()}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def matches(actual: Any, expected: Any) -> bool:
    """Recursive object subset; arrays and scalars must match exactly."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(actual[key], val) for key, val in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


def load_cases(id_filter: str | None = None) -> list[dict[str, Any]]:
    cases = load_yaml_cases(CASES_DIR, id_filter)
    catalog = load_catalog()
    for case in cases:
        allowed = {
            "id",
            "category",
            "_source",
            "user",
            "review",
            "replay",
            "runbooks",
            "max_rounds",
            "tools",
            "forbidden_tools",
            "observations",
            "history",
            "startup",
            "runtime",
            "turn",
        }
        if set(case) - allowed:
            raise CaseError(f"{case['id']}: unknown case fields {sorted(set(case) - allowed)}")
        if not case.get("user") or not case.get("review") or not case.get("replay"):
            raise CaseError(f"{case['id']}: user, review and replay are required")
        if not case.get("runbooks") or any(catalog.get(x) is None for x in case["runbooks"]):
            raise CaseError(f"{case['id']}: unknown/missing isolation runbook")
        if not 1 <= case.get("max_rounds", 6) <= 12:
            raise CaseError(f"{case['id']}: max_rounds must be 1..12")
        rules = case.get("tools", [])
        ids = [rule["id"] for rule in rules]
        if len(ids) != len(set(ids)):
            raise CaseError(f"{case['id']}: duplicate tool rule id")
        for rule in rules:
            allowed_rule = {
                "id",
                "name",
                "args",
                "result",
                "min_calls",
                "max_calls",
                "after",
                "report_ids",
            }
            if set(rule) - allowed_rule or not {"id", "name", "result"}.issubset(rule):
                raise CaseError(f"{case['id']}: malformed tool rule")
            if rule["name"] not in SCHEMAS:
                raise CaseError(f"{case['id']}: unknown tool {rule['name']}")
            if not 0 <= rule.get("min_calls", 0) <= rule.get("max_calls", 2):
                raise CaseError(f"{case['id']}: invalid call bounds")
            if any(
                not isinstance(value, str)
                or not value
                or json.dumps(value) not in json.dumps(rule["result"])
                for value in rule.get("report_ids", [])
            ):
                raise CaseError(f"{case['id']}: reported id must occur in the fixture result")
            if not set(rule.get("after", [])).issubset(ids):
                raise CaseError(f"{case['id']}: unknown prerequisite")
        for observation in case.get("observations", []):
            name = observation["name"]
            if name not in SCHEMAS or not Draft202012Validator(SCHEMAS[name]).is_valid(
                observation.get("args", {})
            ):
                raise CaseError(f"{case['id']}: invalid observation tool arguments")
        for name in case.get("forbidden_tools", []):
            if name not in SCHEMAS:
                raise CaseError(f"{case['id']}: unknown forbidden tool {name}")
    return cases


class IsolationSelector:
    """No selection candidates; caller pins still use real foundation admission."""

    @staticmethod
    async def select(_conversation: Any, _active_ids: Any, **_kwargs: Any) -> SelectionDecision:
        return SelectionDecision(runbook_ids=[], ran=True, degraded=False, authoritative=True)


class RecordingProvider:
    """Record offered schemas/hash, or replay synthetic assistant replies."""

    def __init__(self, delegate: Any = None, *, request_interval: float = 0) -> None:
        self.delegate = delegate
        self.request_interval = request_interval
        self.last_request_at = 0.0
        self.replies: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    async def complete(self, request: Any) -> Any:
        self.requests.append(
            {
                "offered": {t["function"]["name"] for t in request.tools or []},
                "prompt_sha256": digest(request.messages),
                "prompt_bytes": len(json.dumps(request.messages).encode()),
            }
        )
        if self.delegate is not None:
            delay = self.request_interval - (time.perf_counter() - self.last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_request_at = time.perf_counter()
            return await self.delegate.complete(request)
        if not self.replies:
            raise RuntimeError("Replay exhausted before the response loop stopped")
        reply = copy.deepcopy(self.replies.pop(0))
        calls = reply.pop("calls", [])
        message = {"role": "assistant", "content": reply.get("content")}
        if calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{len(self.requests)}_{i}",
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("args", {})),
                    },
                }
                for i, call in enumerate(calls)
            ]
        return _result(message, "tool_calls" if calls else "stop")

    async def aclose(self) -> None:
        if self.delegate is not None:
            await self.delegate.aclose()


class ScriptedEngine:
    """Only fixture responses: even a forbidden mutation cannot touch reality."""

    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case
        self.counts = {rule["id"]: 0 for rule in case.get("tools", [])}
        self.observed_counts = dict(self.counts)
        self.failures: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def begin_round(self) -> None:
        # Results from sibling calls cannot have informed this same model reply.
        self.observed_counts = dict(self.counts)

    def dispatch(self, call: dict[str, Any], offered: set[str]) -> dict[str, Any]:
        name = call["function"]["name"]
        record: dict[str, Any] = {"tool": name}
        self.calls.append(record)

        def fail(reason: str) -> dict[str, Any]:
            self.failures.append(reason)
            record["failure"] = reason
            return {"ok": False, "error": {"code": "BENCH_SCRIPT_REFUSAL"}}

        if name not in offered:
            return fail("unoffered_tool")
        if name in self.case.get("forbidden_tools", []):
            return fail("forbidden_tool")
        try:
            args = json.loads(call["function"]["arguments"])
        except (ValueError, TypeError):
            return fail("invalid_json")
        if not isinstance(args, dict) or not Draft202012Validator(SCHEMAS[name]).is_valid(args):
            return fail("invalid_arguments")
        for rule in self.case.get("tools", []):
            if rule["name"] != name or not matches(args, rule.get("args", {})):
                continue
            if any(self.observed_counts[dep] == 0 for dep in rule.get("after", [])):
                return fail("missing_prerequisite")
            if self.counts[rule["id"]] >= rule.get("max_calls", 2):
                return fail("repeated_call")
            self.counts[rule["id"]] += 1
            record["rule"] = rule["id"]
            return copy.deepcopy(rule["result"])
        # This can be a legitimate alternative the fixture does not model.
        # Report separately; do not call it a proven unsafe model action.
        return fail("unscripted_call_or_target")

    def finish(self, final: str | None = None) -> None:
        for rule in self.case.get("tools", []):
            if self.counts[rule["id"]] < rule.get("min_calls", 0):
                self.failures.append("missing_required_call:" + rule["id"])
            if final is not None and self.counts[rule["id"]]:
                for value in rule.get("report_ids", []):
                    if not re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", final):
                        self.failures.append("missing_reported_id:" + rule["id"])


async def run_case(
    container: Any,
    provider: RecordingProvider,
    case: dict[str, Any],
    *,
    mode: str,
    trial: int = 0,
) -> dict[str, Any]:
    engine = ScriptedEngine(case)
    provider.replies = copy.deepcopy(case["replay"])
    messages = copy.deepcopy(case.get("history", []))
    if case.get("observations"):
        messages.append({"role": "user", "content": "Inspect the task state before the next step."})
        for i, observation in enumerate(case["observations"]):
            call_id = f"observed_{i}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": observation["name"],
                                "arguments": json.dumps(observation.get("args", {})),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(observation["result"]),
                }
            )
    messages.append({"role": "user", "content": case["user"]})
    state = None
    session_id = f"behavior-{case['id']}-{trial}"
    request_start = len(provider.requests)
    active_history: list[list[str]] = []
    admission_failures: list[str] = []
    final = ""
    terminated = False
    started = time.perf_counter()
    usage: list[dict[str, Any]] = []
    for round_index in range(case.get("max_rounds", 6)):
        payload = {
            "protocol_version": 3,
            "session": {"id": session_id, "turn_id": "task", "round": round_index},
            "state": state,
            "input": {"messages": messages, "tools": load_tool_inventory()},
            "startup": case.get("startup", {}),
            "runtime": case.get("runtime", {"mcp": {"connected": True, "tool_count": 96}}),
            "turn": case.get("turn"),
            "selection": {"pinned_runbook_ids": case["runbooks"] if mode == "isolation" else []},
            "generation": {"stream": False, "max_tokens": 1600},
        }
        outcome = await container.orchestrator.complete(RespondRequest.model_validate(payload))
        body = outcome.body or {}
        msg = body.get("message") or {}
        active_history.append([x["id"] for x in body.get("runbooks", {}).get("active", [])])
        if mode == "isolation" and not set(case["runbooks"]).issubset(active_history[-1]):
            engine.failures.append("isolation_pin_missing")
        if (
            mode == "end-to-end"
            and round_index == 0
            and not set(case["runbooks"]).issubset(active_history[-1])
        ):
            admission_failures.append("routing_miss")
        if provider.delegate is not None and body.get("usage"):
            usage.append(body["usage"])
        state = body.get("state")
        if not container.state_codec.verify(state, session_id=session_id).valid:
            engine.failures.append("invalid_state_token")
        if body.get("finish_reason") == "length":
            engine.failures.append("truncated_response")
            break
        calls = msg.get("tool_calls") or []
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content"),
                **({"tool_calls": calls} if calls else {}),
            }
        )
        if not calls:
            final = msg.get("content") or ""
            terminated = bool(final.strip())
            if not terminated:
                engine.failures.append("empty_final")
            break
        engine.begin_round()
        seen: set[str] = set()
        for call in calls:
            if call["id"] in seen:
                engine.failures.append("duplicate_call_id")
            seen.add(call["id"])
            result = engine.dispatch(call, provider.requests[-1]["offered"])
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            )
        if engine.failures:
            break  # failed fixture, not a reason to spend further model calls
    if not terminated and not engine.failures:
        engine.failures.append("round_budget_exhausted")
    engine.finish(final if terminated else None)
    requests = provider.requests[request_start:]
    return {
        "id": case["id"],
        "trial": trial,
        "checks_passed": not engine.failures and not admission_failures,
        "behavior_checks_passed": not engine.failures,
        "admission_passed": not admission_failures,
        "failures": admission_failures + engine.failures,
        "calls": engine.calls,
        "active_runbooks": active_history,
        "rounds": len(requests),
        "prompt_sha256": [r["prompt_sha256"] for r in requests],
        "prompt_bytes": [r["prompt_bytes"] for r in requests],
        "usage": usage,
        "elapsed_s": round(time.perf_counter() - started, 3),
        "review_required": True,
        "review": case["review"],
        # Used only for opt-in local transcripts; removed from summary artifacts.
        "transcript": messages,
        "final": final,
    }
