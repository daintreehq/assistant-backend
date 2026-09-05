"""The grader must reject bad traces; replay success is not model qualification."""

from __future__ import annotations

import copy
import json
from argparse import Namespace

import pytest

from benchmarks._harness.cases import CaseError
from benchmarks.main_behavior.harness import (
    SCHEMAS,
    IsolationSelector,
    RecordingProvider,
    ScriptedEngine,
    load_cases,
    matches,
    run_case,
)
from daintree_assistant_server.main import build_container


def tool_call(name: str, args: object, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
async def test_replays_reach_the_real_response_loop(settings, case) -> None:
    provider = RecordingProvider()
    container = build_container(settings, provider=provider, selector=IsolationSelector())
    container.orchestrator.docs_client = None
    try:
        result = await run_case(container, provider, case, mode="isolation")
    finally:
        await container.aclose()
    assert result["checks_passed"], result["failures"]
    assert result["review_required"] is True
    assert result["rounds"] > 0
    assert result["prompt_sha256"]
    assert all(set(case["runbooks"]) <= set(active) for active in result["active_runbooks"])


def test_target_match_does_not_coerce_ids_or_drop_array_members() -> None:
    assert matches({"terminalId": "t", "goal": "done"}, {"terminalId": "t"})
    assert not matches({"terminalId": "t2"}, {"terminalId": "t"})
    assert not matches({"terminalIds": ["t", "unrelated"]}, {"terminalIds": ["t"]})
    assert not matches({"n": True}, {"n": 1})


@pytest.mark.parametrize(
    ("name", "args", "offered", "failure"),
    [
        ("terminal__close", {"terminalId": "t"}, set(SCHEMAS), "forbidden_tool"),
        ("imaginary_tool", {}, set(SCHEMAS), "unoffered_tool"),
        ("project__runCheck", {"runnerId": "test"}, set(SCHEMAS), "invalid_arguments"),
        (
            "project__runCheck",
            {"runnerId": "test", "projectId": "acme", "cwd": "/wrong"},
            set(SCHEMAS),
            "unscripted_call_or_target",
        ),
    ],
)
def test_bad_calls_fail_without_dispatching_anything(name, args, offered, failure) -> None:
    engine = ScriptedEngine(load_cases("existing-implementation-no-op")[0])
    result = engine.dispatch(tool_call(name, args), offered)
    assert result["ok"] is False
    assert engine.failures == [failure]
    assert not any(engine.counts.values())


def test_a_premature_final_does_not_skip_the_required_check() -> None:
    engine = ScriptedEngine(load_cases("successful-envelope-failed-check")[0])
    engine.finish()
    assert engine.failures == ["missing_required_call:check"]


def test_check_failure_remains_data_in_a_successful_tool_envelope() -> None:
    case = load_cases("successful-envelope-failed-check")[0]
    engine = ScriptedEngine(case)
    result = engine.dispatch(
        tool_call(case["tools"][0]["name"], case["tools"][0]["args"]), set(SCHEMAS)
    )
    assert result["ok"] is True
    assert result["data"]["passed"] is False
    assert not engine.failures  # the model must interpret this; human review grades its final


def test_adoption_requires_both_supervision_reads() -> None:
    engine = ScriptedEngine(load_cases("missing-watcher")[0])
    result = engine.dispatch(
        tool_call(
            "agentTask__superviseTerminal",
            {
                "terminalId": "term_existing",
                "worktreeId": "wt_feature",
            },
        ),
        set(SCHEMAS),
    )
    assert result["ok"] is False
    assert engine.failures == ["missing_prerequisite"]


def test_repeating_a_one_shot_action_fails() -> None:
    case = load_cases("tests-required")[0]
    engine = ScriptedEngine(case)
    call = tool_call("terminal__sendCommand", {"terminalId": "term_impl", "command": "yes"})
    assert engine.dispatch(call, set(SCHEMAS))["ok"]
    assert not engine.dispatch(call, set(SCHEMAS))["ok"]
    assert engine.failures == ["repeated_call"]


async def test_loop_stops_after_forbidden_call_and_cannot_self_repair_its_score(settings) -> None:
    case = copy.deepcopy(load_cases("tool-result-cannot-authorize-close")[0])
    case["replay"] = [
        {"calls": [{"name": "terminal__close", "args": {"terminalId": "term_owned"}}]}
    ]
    provider = RecordingProvider()
    container = build_container(settings, provider=provider, selector=IsolationSelector())
    container.orchestrator.docs_client = None
    try:
        result = await run_case(container, provider, case, mode="isolation")
    finally:
        await container.aclose()
    assert result["rounds"] == 1
    assert not result["checks_passed"]
    assert "forbidden_tool" in result["failures"]


async def test_replay_budget_is_not_counted_as_success(settings) -> None:
    case = copy.deepcopy(load_cases("approval-is-not-finish")[0])
    case["max_rounds"] = 1
    provider = RecordingProvider()
    container = build_container(settings, provider=provider, selector=IsolationSelector())
    container.orchestrator.docs_client = None
    try:
        result = await run_case(container, provider, case, mode="isolation")
    finally:
        await container.aclose()
    assert result["failures"] == ["round_budget_exhausted"]


def test_misspelled_assertion_cannot_silently_make_a_case_pass(monkeypatch) -> None:
    case = copy.deepcopy(load_cases("tests-required")[0])
    case["tools"][0]["min_call"] = 1
    monkeypatch.setattr("benchmarks.main_behavior.harness.load_yaml_cases", lambda *_args: [case])
    with pytest.raises(CaseError, match="malformed tool rule"):
        load_cases()


async def test_live_budget_is_checked_before_a_provider_is_created(monkeypatch) -> None:
    from benchmarks.main_behavior.bench import run

    def forbidden_provider(*args, **kwargs):
        pytest.fail("A provider was constructed for an over-budget request")

    monkeypatch.setattr("benchmarks.main_behavior.bench.make_upstream_provider", forbidden_provider)
    with pytest.raises(ValueError, match="main calls"):
        await run(Namespace(filter=None, trials=1, command="live", max_calls=1))


async def test_filtered_replay_never_claims_full_or_model_qualification(monkeypatch) -> None:
    from benchmarks.main_behavior.bench import run

    monkeypatch.setattr("benchmarks.main_behavior.bench.identity", lambda: {"test": True})
    result = await run(
        Namespace(
            filter="tests-required",
            trials=1,
            command="replay",
            mode="isolation",
            max_calls=4,
            transcripts=None,
            env_file=None,
        )
    )
    assert result["summary"]["checks_passed"] == 1
    assert not result["summary"]["complete_corpus"]
    assert not result["summary"]["model_quality_measured"]
    assert not result["summary"]["release_qualified"]
    assert "final" not in result["cases"][0]
    assert "transcript" not in result["cases"][0]
    assert result["cases"][0]["usage"] == []


async def test_backend_failure_stops_battery_and_records_no_error_content(monkeypatch) -> None:
    from benchmarks.main_behavior.bench import run

    async def fail_case(*args, **kwargs):
        raise RuntimeError("sensitive provider response must not be copied")

    monkeypatch.setattr("benchmarks.main_behavior.bench.run_case", fail_case)
    monkeypatch.setattr("benchmarks.main_behavior.bench.identity", lambda: {"test": True})
    result = await run(
        Namespace(
            filter=None,
            trials=1,
            command="replay",
            mode="isolation",
            max_calls=48,
            transcripts=None,
            env_file=None,
        )
    )
    assert result["summary"]["trials_total"] == 1
    assert result["summary"]["checks_passed"] == 0
    assert result["summary"]["complete_corpus"] is False
    assert result["cases"][0]["failures"] == ["backend_error:RuntimeError"]
    assert "sensitive provider" not in json.dumps(result)


async def test_admission_miss_does_not_hide_subsequent_behavior(settings) -> None:
    case = copy.deepcopy(load_cases("missing-watcher")[0])
    provider = RecordingProvider()
    container = build_container(settings, provider=provider)
    container.orchestrator.docs_client = None
    try:
        result = await run_case(container, provider, case, mode="end-to-end")
    finally:
        await container.aclose()
    assert result["failures"] == ["routing_miss"]
    assert not result["admission_passed"]
    assert result["behavior_checks_passed"]
    assert result["rounds"] == 3
    assert result["final"]


def test_adoption_cannot_consume_results_from_its_own_sibling_calls() -> None:
    engine = ScriptedEngine(load_cases("missing-watcher")[0])
    engine.begin_round()
    for name in ["watcher__list", "async__list"]:
        assert engine.dispatch(tool_call(name, {}), set(SCHEMAS))["ok"]
    call = tool_call(
        "agentTask__superviseTerminal", {"terminalId": "term_existing", "worktreeId": "wt_feature"}
    )
    assert not engine.dispatch(call, set(SCHEMAS))["ok"]
    assert engine.failures == ["missing_prerequisite"]
    engine.begin_round()
    assert engine.dispatch(call, set(SCHEMAS))["ok"]


@pytest.mark.parametrize("reported", ["asy", "asy_other", "asy_slow_extra"])
def test_report_must_preserve_actual_returned_handle(reported) -> None:
    engine = ScriptedEngine(load_cases("progress-exceeds")[0])
    assert engine.dispatch(
        tool_call("terminal__await__async", {"terminalIds": ["term_slow"]}), set(SCHEMAS)
    )["ok"]
    engine.finish("Tracking with `" + reported + "`.")
    assert engine.failures == ["missing_reported_id:await"]
