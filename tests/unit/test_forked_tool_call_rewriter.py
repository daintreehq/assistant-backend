"""Offering the internal tool, and making the call it produces invisible.

The rewriter is the boundary the whole feature's safety rests on. Everything it
lets through reaches a client that is FORBIDDEN from replaying it: the request
boundary refuses the `daintree_internal__` namespace, so an internal call that
escaped would come back as a 400 on the continuation round — after the user had
already watched four terminals fail to appear.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_fork_prompt_expansion import (
    BRANCH_IDS,
    SPAWN_SCHEMA,
    ScriptedProvider,
    plan,
    settings,
)

from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.services.fork_prompt_expansion import (
    ForkPromptExpansionService,
)
from daintree_assistant_server.services.forked_terminal_calls import (
    FORK_TOOL_DEFINITION,
    FORK_TOOL_WIRE_NAME,
    SPAWN_TOOL_WIRE_NAME,
)
from daintree_assistant_server.services.forked_tool_call_rewriter import (
    REASON_DISABLED,
    REASON_ENABLED,
    REASON_PROFILE_EXCLUDED,
    REASON_PROVIDER_TOOL_LIMIT,
    REASON_TARGET_TOOL_ABSENT,
    ForkedToolCallRewriter,
    decide_fork_feature,
    inject_fork_tool,
    is_direct_spawn_batch,
    is_fork_call,
    mentions_fork_tool,
)


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": SPAWN_SCHEMA}}


def _fork_call(plan_body: Any = None, *, call_id: str = "call_internal_1") -> dict[str, Any]:
    body = plan_body if plan_body is not None else plan().model_dump(by_alias=True, mode="json")
    arguments = body if isinstance(body, str) else json.dumps(body)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": FORK_TOOL_WIRE_NAME, "arguments": arguments},
    }


def _other_call(name: str = "fs__read") -> dict[str, Any]:
    return {
        "id": "call_other",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def rewriter(provider: Any | None = None, **overrides: Any) -> ForkedToolCallRewriter:
    return ForkedToolCallRewriter(
        expansion=ForkPromptExpansionService(
            settings=settings(**overrides), provider=provider or ScriptedProvider()
        )
    )


# ---------------------------------------------------------------------------
# Who is offered the tool.
# ---------------------------------------------------------------------------


def _decide(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "mode": "enabled",
        "profile": "assistant",
        "tools": [_tool(SPAWN_TOOL_WIRE_NAME)],
        "max_tools": 128,
    }
    kwargs.update(overrides)
    return decide_fork_feature(**kwargs)


def test_an_ordinary_enabled_assistant_turn_is_offered_the_tool() -> None:
    decision = _decide()
    assert decision.enabled is True
    assert decision.reason == REASON_ENABLED


def test_disabled_is_the_default_posture() -> None:
    # A deployment that sets nothing must behave exactly as it did before this
    # feature existed.
    decision = _decide(mode="disabled")
    assert decision.enabled is False
    assert decision.reason == REASON_DISABLED


def test_a_subagent_is_never_offered_the_tool() -> None:
    # A sub-agent is read-only and cannot spawn a visible terminal at all, so
    # the tool would be an instruction it has no legal move for.
    decision = _decide(profile="subagent")
    assert decision.enabled is False
    assert decision.reason == REASON_PROFILE_EXCLUDED


def test_the_tool_is_withheld_when_its_target_is_absent() -> None:
    # It expands into exactly one other tool. Offering it to a caller without
    # that tool would produce four calls the client cannot execute — worse than
    # not offering it, because the model would believe it had spawned them.
    decision = _decide(tools=[_tool("fs__read")])
    assert decision.enabled is False
    assert decision.reason == REASON_TARGET_TOOL_ABSENT


def test_the_tool_is_withheld_rather_than_pushing_past_the_tool_count() -> None:
    # The conservative half of the trade. Advertising one fewer client tool
    # would 400 a request that is legal today, for an upstream ceiling this repo
    # has never verified against the live host.
    tools = [_tool(SPAWN_TOOL_WIRE_NAME)] + [_tool(f"t{i}") for i in range(127)]
    decision = _decide(tools=tools, max_tools=128)
    assert decision.enabled is False
    assert decision.reason == REASON_PROVIDER_TOOL_LIMIT


def test_one_slot_short_of_the_limit_still_gets_the_tool() -> None:
    tools = [_tool(SPAWN_TOOL_WIRE_NAME)] + [_tool(f"t{i}") for i in range(126)]
    assert _decide(tools=tools, max_tools=128).enabled is True


def test_the_decision_does_not_depend_on_which_skills_loaded() -> None:
    # Deliberately session-stable. A tool list that changed on a topic shift
    # would re-encode the largest region of the request for nothing, and the
    # base prompt's rule would refer to a tool that appears halfway through a
    # workflow.
    assert set(_decide().__dict__) == {"enabled", "reason"}


def test_injection_appends_rather_than_inserts() -> None:
    # The tools region is serialized ahead of the conversation and matched
    # literally by the prefix cache, so appending leaves every existing schema
    # at the byte offset it already had.
    existing = [_tool("fs__read"), _tool(SPAWN_TOOL_WIRE_NAME)]
    injected = inject_fork_tool(existing)
    assert injected[:-1] == existing
    assert injected[-1] is FORK_TOOL_DEFINITION


def test_injection_into_an_empty_inventory_still_works() -> None:
    assert inject_fork_tool(None) == [FORK_TOOL_DEFINITION]


# ---------------------------------------------------------------------------
# Recognising the call.
# ---------------------------------------------------------------------------


def test_is_fork_call_matches_only_the_internal_function() -> None:
    assert is_fork_call(_fork_call()) is True
    assert is_fork_call(_other_call()) is False
    assert is_fork_call({"id": "x"}) is False
    assert is_fork_call({"function": "not a dict"}) is False


@pytest.mark.parametrize(
    "name",
    ["daintree_internal__fork_spawn_terminals", "daintree_internal__fork", "daintree"],
)
def test_a_partial_internal_name_in_a_raw_fragment_is_treated_as_the_real_thing(
    name: str,
) -> None:
    # Fragments carry arbitrary slices of a name, so the passthrough check is
    # coarse on purpose: anything that COULD be part of it fails the turn closed
    # rather than risking emitting it.
    assert mentions_fork_tool({"function": {"name": name}}) is True


@pytest.mark.parametrize("fragment", [{"function": {"name": "fs__read"}}, {}, "nope", None])
def test_an_unrelated_fragment_is_not_mistaken_for_the_internal_call(fragment: Any) -> None:
    assert mentions_fork_tool(fragment) is False


# ---------------------------------------------------------------------------
# Rewriting.
# ---------------------------------------------------------------------------


async def _rewrite(calls: list[dict[str, Any]], provider: Any | None = None) -> Any:
    return await rewriter(provider).rewrite(
        calls,
        base_request=ProviderChatRequest(
            model="m",
            messages=[{"role": "user", "content": "go"}],
            tools=[_tool(SPAWN_TOOL_WIRE_NAME)],
            max_tokens=1024,
        ),
        request_id="req-1",
    )


async def test_a_round_with_no_fork_call_passes_through_untouched() -> None:
    calls = [_other_call(), _other_call("git__status")]
    result = await _rewrite(calls)
    assert result.calls is calls
    assert result.rewritten is False
    assert result.added_usage is None


async def test_one_fork_call_becomes_four_spawn_calls() -> None:
    result = await _rewrite([_fork_call()])
    assert result.rewritten is True
    assert [c["function"]["name"] for c in result.calls] == [SPAWN_TOOL_WIRE_NAME] * 4


async def test_the_internal_call_is_absent_from_the_result() -> None:
    result = await _rewrite([_fork_call()])
    assert all(not is_fork_call(call) for call in result.calls)
    assert FORK_TOOL_WIRE_NAME not in json.dumps(result.calls)


async def test_the_expansion_usage_comes_back_to_be_billed() -> None:
    result = await _rewrite([_fork_call()])
    assert result.added_usage is not None
    assert result.added_usage.prompt_tokens == 4000


async def test_branch_order_is_the_plan_order() -> None:
    provider = ScriptedProvider(
        delays={"backend": 0.04, "frontend": 0.01, "tests": 0.03, "performance": 0.02}
    )
    result = await _rewrite([_fork_call()], provider)
    worktrees = [json.loads(c["function"]["arguments"])["worktreeId"] for c in result.calls]
    assert worktrees == [f"/repo/{bid}" for bid in BRANCH_IDS]


# ---------------------------------------------------------------------------
# Failing closed.
# ---------------------------------------------------------------------------


async def test_a_fork_call_with_a_sibling_is_refused_whole() -> None:
    # Running the siblings while dropping the fork would leave the model
    # believing it had spawned four agents; running the fork while dropping the
    # siblings could skip a prerequisite it was told to do first. There is no
    # safe half.
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_fork_call(), _other_call()])
    assert exc.value.code == "fork_call_not_alone"
    assert exc.value.status_code == 502


async def test_a_sibling_BEFORE_the_fork_call_is_refused_too() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_other_call(), _fork_call()])
    assert exc.value.code == "fork_call_not_alone"


async def test_two_fork_calls_are_refused() -> None:
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_fork_call(call_id="a"), _fork_call(call_id="b")])
    assert exc.value.code == "fork_call_not_alone"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json", id="invalid_json"),
        pytest.param("[]", id="not_an_object"),
        pytest.param("", id="empty"),
        pytest.param({"sharedGoal": "x"}, id="missing_branches"),
        pytest.param({"branches": []}, id="no_branches"),
    ],
)
async def test_an_unusable_plan_fails_closed(body: Any) -> None:
    # No repair path and no default. Every field either names prose or selects a
    # mutation target, and the server has no standing to guess a worktree, an
    # agent or a supervision decision on the user's behalf.
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_fork_call(body)])
    assert exc.value.code == "fork_plan_invalid"
    assert exc.value.status_code == 502


async def test_a_plan_naming_one_worktree_twice_fails_closed() -> None:
    # The disjointness gate reaches the rewriter as an ordinary schema failure —
    # which is the point: two agents editing one checkout is refused before any
    # expansion call is paid for.
    body = plan().model_dump(by_alias=True, mode="json")
    body["branches"][1]["worktreeId"] = body["branches"][0]["worktreeId"]
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_fork_call(body)])
    assert exc.value.code == "fork_plan_invalid"


async def test_a_rejected_plan_costs_no_expansion_calls() -> None:
    provider = ScriptedProvider()
    with pytest.raises(DaintreeError):
        await _rewrite([_fork_call("not json")], provider)
    assert provider.requests == []


async def test_the_missing_target_tool_fails_rather_than_fabricating_calls() -> None:
    # Unreachable through the gate, which refuses to offer the fork tool without
    # its target. Reaching it means assembly and rewrite disagree about what was
    # offered, and fabricating calls the client cannot execute is the one wrong
    # answer.
    with pytest.raises(DaintreeError) as exc:
        await rewriter().rewrite(
            [_fork_call()],
            base_request=ProviderChatRequest(
                model="m",
                messages=[{"role": "user", "content": "go"}],
                tools=[_tool("fs__read")],
                max_tokens=1024,
            ),
            request_id="req-1",
        )
    assert exc.value.code == "fork_target_unavailable"


async def test_the_plan_is_never_echoed_into_the_error_message() -> None:
    # A plan quotes the user's goal and their project's file scopes. The
    # validation PATH is what gets logged and reported, never the content.
    secret = "the client's private release plan"
    body = plan().model_dump(by_alias=True, mode="json")
    body["sharedGoal"] = secret
    body["branches"] = body["branches"][:2]
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([_fork_call(body)])
    assert secret not in exc.value.message


# ---------------------------------------------------------------------------
# The synthesized batch is a legal transcript.
# ---------------------------------------------------------------------------


async def test_the_four_calls_form_a_batch_the_validator_accepts() -> None:
    from daintree_assistant_server.contracts.daintree_api import RespondRequest
    from daintree_assistant_server.services.validation import validate_respond_request

    result = await _rewrite([_fork_call()])
    # Replay the batch the way a client would on the continuation round: the
    # assistant turn that opened it, then one result per call.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "split this four ways"},
        {"role": "assistant", "content": None, "tool_calls": result.calls},
    ]
    for call in result.calls:
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": "spawned"})
    request = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": messages},
            "startup": {},
        }
    )
    validate_respond_request(request, Settings(TOGETHER_API_KEY="sk", STATE_SIGNING_KEY_CURRENT="s"))


async def test_the_call_ids_are_unique_and_wire_legal() -> None:
    result = await _rewrite([_fork_call()])
    ids = [call["id"] for call in result.calls]
    assert len(set(ids)) == 4
    assert all(id_.strip() == id_ and len(id_) <= 256 for id_ in ids)


async def test_the_same_fork_call_id_produces_the_same_batch_ids() -> None:
    first = await _rewrite([_fork_call(call_id="call_x")])
    second = await _rewrite([_fork_call(call_id="call_x")])
    assert [c["id"] for c in first.calls] == [c["id"] for c in second.calls]


async def test_a_different_fork_call_id_produces_different_batch_ids() -> None:
    first = await _rewrite([_fork_call(call_id="call_x")])
    second = await _rewrite([_fork_call(call_id="call_y")])
    assert {c["id"] for c in first.calls}.isdisjoint(c["id"] for c in second.calls)


async def test_an_expansion_that_fully_falls_back_still_yields_four_calls() -> None:
    from daintree_assistant_server.errors import upstream_unavailable

    provider = ScriptedProvider(default=upstream_unavailable("down"))
    result = await _rewrite([_fork_call()], provider)
    assert len(result.calls) == 4
    assert result.rewritten is True
    # Nothing reported a completion, so there is no usage — which is different
    # from reporting zero.
    assert result.added_usage is None


# ---------------------------------------------------------------------------
# The shapes a provider can send that the wire does not describe.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [None, "", 7, {"nested": True}])
def test_a_fragment_with_no_usable_name_is_not_mistaken_for_the_call(name: Any) -> None:
    assert mentions_fork_tool({"function": {"name": name}}) is False


async def test_arguments_sent_as_an_object_rather_than_a_string_are_a_typed_502() -> None:
    # The wire specifies `arguments` as a JSON-ENCODED STRING. A provider that
    # sends the decoded object instead used to reach `.strip()` and surface as a
    # 500 about a Daintree bug rather than the documented upstream failure.
    call = {
        "id": "call_internal_1",
        "type": "function",
        "function": {"name": FORK_TOOL_WIRE_NAME, "arguments": {"sharedGoal": "x"}},
    }
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([call])
    assert exc.value.code == "fork_plan_invalid"
    assert exc.value.status_code == 502


async def test_absent_arguments_are_a_typed_502_rather_than_a_crash() -> None:
    call = {
        "id": "call_internal_1",
        "type": "function",
        "function": {"name": FORK_TOOL_WIRE_NAME},
    }
    with pytest.raises(DaintreeError) as exc:
        await _rewrite([call])
    assert exc.value.code == "fork_plan_invalid"


async def test_a_target_tool_with_no_usable_schema_fails_rather_than_guessing() -> None:
    # The runtime schema is the authority for validating an expansion. A target
    # entry carrying no object schema cannot serve as one, and fabricating four
    # calls we could not validate is the one wrong answer.
    with pytest.raises(DaintreeError) as exc:
        await rewriter().rewrite(
            [_fork_call()],
            base_request=ProviderChatRequest(
                model="m",
                messages=[{"role": "user", "content": "go"}],
                tools=[{"type": "function", "function": {"name": SPAWN_TOOL_WIRE_NAME}}],
                max_tokens=1024,
            ),
            request_id="req-1",
        )
    assert exc.value.code == "fork_target_unavailable"


# ---------------------------------------------------------------------------
# The direct-spawn shape metric.
#
# Withholding the spawn tool is not available as a fix: a hard rule instructs
# it, it is the only way to spawn ONE agent, and `decide_fork_feature` refuses
# to offer the fork tool without it. So whether the model reaches for the fork
# is a prompt property — and this is the upper bound on how often it did not.
# ---------------------------------------------------------------------------


def _offered() -> Any:
    return _decide()


def test_four_hand_written_spawns_are_a_candidate() -> None:
    # The exact shape the fork exists to replace: four full task prompts written
    # one after another on the critical path, with the compact alternative
    # sitting unused in the tool list.
    assert is_direct_spawn_batch([SPAWN_TOOL_WIRE_NAME] * 4, fork=_offered()) is True


def test_five_spawns_are_not_a_candidate() -> None:
    # The fork is fixed at four and must be called alone, so it cannot replace
    # four calls inside a five-call batch without adding a round and delaying
    # the fifth spawn. The runbook sends every other count down the direct path,
    # so counting this would file correct behaviour as a candidate.
    assert is_direct_spawn_batch([SPAWN_TOOL_WIRE_NAME] * 5, fork=_offered()) is False


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_fewer_than_four_spawns_is_not_a_candidate(count: int) -> None:
    names = [SPAWN_TOOL_WIRE_NAME] * count
    assert is_direct_spawn_batch(names, fork=_offered()) is False


def test_a_turn_that_was_never_offered_the_tool_did_nothing_wrong() -> None:
    disabled = _decide(mode="disabled")
    assert is_direct_spawn_batch([SPAWN_TOOL_WIRE_NAME] * 4, fork=disabled) is False


def test_other_tools_do_not_dilute_the_count() -> None:
    names = [SPAWN_TOOL_WIRE_NAME] * 4 + ["fs__read", "git__status"]
    assert is_direct_spawn_batch(names, fork=_offered()) is True


def test_the_predicate_is_an_upper_bound_not_a_verdict() -> None:
    """Four rivals in a competition are four correct direct spawns.

    The predicate cannot tell them from four independent units — the names are
    identical — which is exactly why the event is named for the SHAPE it saw and
    not for a judgement about it. A test that expected `False` here would be
    asserting a distinction the data does not carry.
    """
    competition = [SPAWN_TOOL_WIRE_NAME] * 4
    assert is_direct_spawn_batch(competition, fork=_offered()) is True
