"""Enforced tool projection, end to end through /respond.

The unit tests prove the gate DECIDES correctly. They say nothing about whether
the orchestrator acts on the decision, whether the filtered array is the one that
reaches the provider, or whether a failure anywhere in the chain still produces a
normal turn. These close that gap by asserting on the request the fake provider
actually received.

Every fail-open case below asserts the same thing — the FULL inventory arrived
and the turn returned 200. That repetition is the point: the failures are
different, and the answer to all of them has to be identical, because a filter
that is wrong in a new way must still degrade in the one old way.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog.testing
from conftest import respond_body
from fakes import FakeProvider, make_result, selection_result

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.skills import tool_projection

URL = "/v1/daintree/respond"

# `forge__getPR` is DECLARED by skills in the real catalog but needed by none of
# them on a skill-less turn, so it is genuinely withheld. A name no skill declares
# would pass through as unclassified and prove nothing.
CORE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "context__snapshot",
        "description": "Snapshot the workspace.",
        "parameters": {"type": "object", "properties": {}},
    },
}
WITHHELD_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "forge__getPR",
        "description": "Read a PR.",
        "parameters": {
            "type": "object",
            "properties": {"prNumber": {"type": "integer"}},
            "required": ["prNumber"],
        },
    },
}
TOOLS = [CORE_TOOL, WITHHELD_TOOL]


def _pinned_filler() -> dict[str, Any]:
    """A third DECLARED tool no rule here admits.

    With only `CORE_TOOL` and `WITHHELD_TOOL`, a turn that pins or continues
    `forge__getPR` has a legitimate projection equal to the whole inventory — so
    a membership assertion passes identically whether enforcement ran or was
    skipped. This gives those tests something that must be absent.
    """
    return {
        "type": "function",
        "function": {
            "name": "recipe__run",
            "description": "Run a recipe.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _sent_tool_names(provider: FakeProvider) -> list[str]:
    """The tool names on the last request that actually reached the provider.

    The two response paths record into different lists, and reading only the
    non-streaming one would make every streaming assertion here pass against a
    provider that was never called.
    """
    sent = provider.requests + provider.stream_requests
    assert sent, "the provider was never called"
    return [t["function"]["name"] for t in sent[-1].tools or []]


def _respond_records(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in logs if r.get("event") == "respond"]


@pytest.fixture
def enforced_client(settings: Settings, fake_provider: FakeProvider):  # type: ignore[no-untyped-def]
    """A client whose settings enforce for every session.

    Settings are frozen and the app takes one at construction, so the mode is set
    by building the app around a modified copy rather than by mutating anything —
    the same route `tests/conftest.py` already uses to inject a fake provider.
    """

    from conftest import detach_docs_client, scripted_selector

    from daintree_assistant_server.main import build_container

    def build(**overrides: Any) -> Any:
        merged = settings.model_copy(
            update={
                "tool_projection_mode": "enforced",
                "tool_projection_rollout_percent": 100,
                **overrides,
            }
        )
        seam = scripted_selector(fake_provider, merged)
        app = create_app(settings=merged, provider=fake_provider, selector=seam)
        container = build_container(merged, fake_provider, seam)
        detach_docs_client(container)
        app.state.container = container
        app.state.ready = True
        return app

    return build


def _post(app: Any, extra: dict[str, Any] | None = None, **body: Any) -> Any:
    """One turn against `app`, through whichever path the body asks for.

    A stream has to be CONSUMED for the upstream call to happen at all, so a
    streaming assertion written like a non-streaming one would assert against a
    provider that was never reached — and pass or fail for the wrong reason.
    """
    from conftest import detach_docs_client
    from fastapi.testclient import TestClient

    payload = respond_body(**body)
    payload.update(extra or {})
    streaming = bool(payload.get("generation", {}).get("stream"))
    headers = {"Authorization": "Bearer test-api-key"}
    with TestClient(app) as client:
        detach_docs_client(app.state.container)
        if not streaming:
            return client.post(URL, headers=headers, json=payload)
        with client.stream("POST", URL, headers=headers, json=payload) as response:
            "".join(response.iter_text())
            return response


def _turn(**body: Any) -> dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": "what's open?"}],
        "session_id": "enf-1",
        "turn_id": "t1",
        "tools": TOOLS,
        **body,
    }


# ---------------------------------------------------------------------------
# The change itself.
# ---------------------------------------------------------------------------


def test_a_healthy_enforced_turn_sends_only_the_projection(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="nothing open")
    response = _post(enforced_client(), **_turn())
    assert response.status_code == 200
    sent = _sent_tool_names(fake_provider)
    assert sent == ["context__snapshot"], "the withheld tool still reached the provider"


def test_streaming_enforces_the_same_filter(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """Both response paths share one prepared request, and this proves it.

    Filtering happens before the first `meta` event, so a stream cannot be half
    enforced: whatever the gate decided is already fixed when the first byte goes
    out.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"content": "nothing open"}, "finish_reason": "stop"}]}
    ]
    response = _post(
        enforced_client(), **_turn(generation={"stream": True}, session_id="enf-stream")
    )
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot"]


def test_the_turn_reports_what_it_enforced(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """`respond` is the once-per-turn record every rollout rate divides by."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        _post(enforced_client(), **_turn(session_id="enf-metrics"))
    (record,) = _respond_records(logs)
    assert record["tool_projection_enforced"] is True
    assert record["tool_projection_fail_open"] is False
    assert record["tool_projection_reason"] == tool_projection.REASON_ENFORCED
    assert record["tool_projection_mode"] == "enforced"
    assert record["tool_projection_cohort_selected"] is True
    assert 0 <= record["tool_projection_bucket"] < 100
    # The size of the prize, and what was actually paid.
    assert record["tools_offered"] == 2
    assert record["tools_proposed"] == 1
    assert record["tools_sent"] == 1
    assert record["tools_sent_bytes"] == record["tools_projected_bytes"]
    assert record["tools_full_bytes"] > record["tools_projected_bytes"] > 0


# ---------------------------------------------------------------------------
# Every way this declines, and the one answer it gives.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_the_non_enforcing_modes_send_everything(enforced_client, fake_provider, mode: str) -> None:  # type: ignore[no-untyped-def]
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    response = _post(enforced_client(tool_projection_mode=mode), **_turn(session_id=f"enf-{mode}"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]


def test_a_zero_percent_rollout_sends_everything(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """The kill switch that does not need the mode changed.

    An operator halting a rollout reaches for the percentage first; the mode is
    the bigger hammer. Both have to work, and this is the one that leaves the
    measurement running.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    response = _post(
        enforced_client(tool_projection_rollout_percent=0), **_turn(session_id="enf-zero")
    )
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]


def test_a_projection_that_raises_leaves_the_turn_whole(
    enforced_client, fake_provider, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The failure the whole design is arranged around.

    A bug in the projector must cost a larger prompt, never a turn. Note what is
    asserted: not merely a 200, but that the FULL inventory arrived — a fail-open
    that quietly sent a partial list would be the same bug with better manners.
    """

    def boom(**_: Any) -> Any:
        raise RuntimeError("projection exploded")

    monkeypatch.setattr("daintree_assistant_server.services.chat_orchestrator.project_tools", boom)
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-raise"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]
    (record,) = _respond_records(logs)
    assert record["tool_projection_enforced"] is False
    assert record["tool_projection_fail_open"] is True
    assert record["tool_projection_reason"] == tool_projection.REASON_PROJECTION_ERROR
    # The projection could not be computed, so there is no projected size to
    # report. `None`, never 0 — a zero would read as "it proposed nothing".
    assert record["tools_projected_bytes"] is None


def test_an_empty_projection_is_never_enforced(enforced_client, fake_provider, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An empty proposal means "withhold everything" — the worst possible request."""
    monkeypatch.setattr(
        "daintree_assistant_server.services.chat_orchestrator.project_tools",
        lambda **_: tool_projection.ToolProjection(
            proposed=frozenset(), offered=frozenset({"context__snapshot", "forge__getPR"})
        ),
    )
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-empty"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_EMPTY_PROJECTION
    assert record["tool_projection_fail_open"] is True


def test_a_projection_describing_another_request_is_never_enforced(
    enforced_client, fake_provider, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The assembled inventory and the judged one must be the same inventory.

    If they ever diverge — a normalization that renames, a reordering that drops —
    the proposal is an answer to a question nobody asked, and applying it would
    withhold by accident.
    """
    monkeypatch.setattr(
        "daintree_assistant_server.services.chat_orchestrator.project_tools",
        lambda **_: tool_projection.ToolProjection(
            proposed=frozenset({"context__snapshot"}),
            offered=frozenset({"context__snapshot", "something__else"}),
            reasons={"context__snapshot": "core"},
        ),
    )
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-mismatch"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_INVENTORY_MISMATCH
    assert record["tool_projection_fail_open"] is True


def test_a_pinned_tool_is_never_withheld(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """A `tool_choice` the request does not carry is a provider 400, not a saving.

    The projection admits a forced tool by construction, so this asserts the
    end-to-end consequence rather than the rule: whatever else was filtered, the
    pinned name arrived.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "index": 0,
                "function": {"name": "forge__getPR", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
    )
    # A THIRD declared tool, needed by nothing here. Without it the legitimate
    # projection equals the full inventory and this test passes just as well when
    # enforcement is skipped entirely — proving nothing about the pin.
    response = _post(
        enforced_client(),
        **_turn(
            session_id="enf-pinned",
            tools=[*TOOLS, _pinned_filler()],
            tool_choice={"name": "forge__getPR"},
        ),
    )
    assert response.status_code == 200
    sent = _sent_tool_names(fake_provider)
    assert "forge__getPR" in sent, "the pinned tool was withheld — a provider 400"
    assert "recipe__run" not in sent, "nothing was filtered, so the pin proves nothing"


def test_an_unanswered_call_keeps_its_tool_on_the_wire(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """A continuation round must always be able to finish work already started."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="done")
    response = _post(
        enforced_client(),
        **_turn(
            session_id="enf-unresolved",
            tools=[*TOOLS, _pinned_filler()],
            messages=[
                {"role": "user", "content": "check the PR"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "forge__getPR", "arguments": "{}"},
                        }
                    ],
                },
            ],
        ),
    )
    assert response.status_code == 200
    sent = _sent_tool_names(fake_provider)
    assert "forge__getPR" in sent, "a continuation could not finish work it started"
    assert "recipe__run" not in sent, "nothing was filtered, so this proves nothing"


def test_a_tool_no_skill_declares_survives_enforcement(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """The catalog cannot judge what it has never heard of.

    A client-side or integration-contributed tool has no `requiredTools` entry
    anywhere, so withholding it would be an argument from silence — and it would
    stay withheld until a user noticed their own tool had stopped working.
    """
    unknown = {
        "type": "function",
        "function": {
            "name": "acme__deploy",
            "description": "Deploy via the customer's own MCP server.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    response = _post(enforced_client(), **_turn(session_id="enf-unknown", tools=[*TOOLS, unknown]))
    assert response.status_code == 200
    # EXACT, not membership: asserting only that `acme__deploy` survived would
    # stay green if the pass-through had also swallowed the core tool, or if
    # enforcement had been skipped entirely and everything went out.
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "acme__deploy"]


def test_a_sub_agent_turn_is_held_out(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """Sub-agents choose from a narrowed menu that few skills survive, so most of
    their turns are skill-less — the shape where a core tuned for the ASSISTANT
    base prompt is the wrong core."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    body = _turn(session_id="enf-subagent")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), extra={"profile": "subagent"}, **body)
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"], (
        "held out of enforcement means the FULL inventory, not merely a log line"
    )
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_PROFILE_EXCLUDED
    assert record["tool_projection_fail_open"] is False
    assert record["profile"] == "subagent", (
        "without this a sub-agent shadow record is indistinguishable from an "
        "assistant one, and the first rollout stage promotes on evidence from a "
        "profile it does not cover"
    )


# ---------------------------------------------------------------------------
# The skill path, which none of the above reaches.
# ---------------------------------------------------------------------------


def _named(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_an_active_skill_puts_its_declared_tools_on_the_wire(
    enforced_client, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """The whole skill-admission path, end to end — and nothing else covers it.

    Every other enforced test here scripts `selection_result([])`, so a wiring
    regression that passed `[]` into `project_tools` instead of the resolved
    active set would leave this entire file green while withholding exactly the
    tools the selected runbook exists to call. That is the most expensive bug this
    feature can have and it was, until this test, invisible.
    """
    fake_provider.selection_result = selection_result(["daintree.terminals.rename"])
    fake_provider.complete_result = make_result(content="renamed")
    tools = [
        CORE_TOOL,  # core
        _named("terminal__rename"),  # the skill's own reason for existing
        _named("terminal__extract__json"),  # also declared by the skill
        _named("daintree__call"),  # via the paired domain foundation
        _named("skill__step__advance"),  # the scaffold, because a skill is active
        WITHHELD_TOOL,  # declared elsewhere, needed by nothing here
    ]
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-skill", tools=tools))
    assert response.status_code == 200
    sent = set(_sent_tool_names(fake_provider))
    assert {"terminal__rename", "terminal__extract__json"} <= sent, (
        "the selected skill's own tools were withheld"
    )
    assert "daintree__call" in sent, "the paired domain foundation's tools were withheld"
    assert "skill__step__advance" in sent, "the scaffold was withheld with a skill active"
    assert "context__snapshot" in sent
    # And the point of the exercise: something was actually dropped.
    assert "forge__getPR" not in sent
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_ENFORCED


def test_a_degraded_selector_sends_everything(enforced_client, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """The health gate, wired rather than merely decided.

    The pure gate is unit-tested, but a typo in the `decision.degraded` handoff
    inside `_prepare_inner` would enforce against whatever active set survived the
    failure — withholding tools for a task the selector never successfully read.
    """
    # Two unparseable selections in a row: the repair round-trip is exhausted and
    # the decision degrades. (A selector that RAISES is a different path, and one
    # the scripted seam deliberately does not swallow.)
    fake_provider.selection_result = [
        make_result(content="not json"),
        make_result(content="still not json"),
    ]
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(
            enforced_client(),
            **_turn(session_id="enf-degraded", selection={"force": True, "policy": "always"}),
        )
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_SELECTOR_DEGRADED
    assert record["tool_projection_fail_open"] is True
    assert record["tool_projection_enforced"] is False


# ---------------------------------------------------------------------------
# The remaining guards in _prepare_inner.
# ---------------------------------------------------------------------------


def test_a_filter_that_raises_leaves_the_turn_whole(
    enforced_client, fake_provider, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("filter exploded")

    monkeypatch.setattr(
        "daintree_assistant_server.services.chat_orchestrator.filter_provider_tools", boom
    )
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-filter-raise"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_FILTER_ERROR
    assert record["tool_projection_fail_open"] is True


def test_a_gate_that_raises_leaves_the_turn_whole(
    enforced_client, fake_provider, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    def boom(**_: Any) -> Any:
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(
        "daintree_assistant_server.services.chat_orchestrator.decide_enforcement", boom
    )
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    response = _post(enforced_client(), **_turn(session_id="enf-gate-raise"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot", "forge__getPR"]


def test_a_broken_metric_costs_the_metric_and_nothing_else(
    enforced_client, fake_provider, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """A lost measurement must never widen a request that already validated.

    The sizes block runs AFTER the decision, so a failure there says nothing about
    whether withholding was safe — reverting to the full inventory over it would
    be a fail-open triggered by the instrument rather than by the thing measured.
    """

    def boom(*_: Any, **__: Any) -> Any:
        raise ValueError("unpaired surrogate")

    monkeypatch.setattr("daintree_assistant_server.services.chat_orchestrator.tools_bytes", boom)
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="ok")
    with structlog.testing.capture_logs() as logs:
        response = _post(enforced_client(), **_turn(session_id="enf-metric-raise"))
    assert response.status_code == 200
    assert _sent_tool_names(fake_provider) == ["context__snapshot"], (
        "a metrics failure must not un-enforce a validated projection"
    )
    (record,) = _respond_records(logs)
    assert record["tool_projection_reason"] == tool_projection.REASON_ENFORCED
    assert record["tools_full_bytes"] is None
    assert any(r.get("event") == "tool_projection_metrics_failed" for r in logs)


def test_every_attempt_of_a_re_rolled_turn_carries_the_filtered_list(
    enforced_client, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """EVERY attempt, not just the last one.

    `_sent_tool_names` reads the final request, so on its own it would hide a
    first attempt that went out unfiltered. The bare-intent re-roll is exactly
    where a request gets rebuilt mid-turn, which makes "the last one was right"
    the weakest available statement about it — so this one walks all of them.

    A round that only ANNOUNCES a tool call without emitting one is re-rolled
    once, giving two genuine upstream attempts off the same prepared request.
    """
    fake_provider.selection_result = selection_result([])
    # A round that announces an action it never emitted (the shape issue #25
    # named), phrased as tests/unit/test_orchestrator.py phrases it.
    fake_provider.complete_result = [
        make_result(
            content="All five votes are in. Let me read them, one read per terminal, batched."
        ),
        make_result(content="Nothing is open."),
    ]
    response = _post(
        enforced_client(),
        **_turn(
            session_id="enf-reroll",
            messages=[{"role": "user", "content": "read the votes"}],
        ),
    )
    assert response.status_code == 200
    attempts = fake_provider.requests
    assert len(attempts) >= 2, "the re-roll never happened, so this proves nothing"
    for index, attempt in enumerate(attempts):
        names = [t["function"]["name"] for t in attempt.tools or []]
        assert names == ["context__snapshot"], f"attempt {index} sent {names}"


def test_the_result_record_stamps_enforced_rounds_distinctly(
    enforced_client, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """Enforced and shadow evidence must be separable in the log.

    An enforced turn cannot call a tool it was never offered, so its
    `outside_proposed` is clean by construction — which is fine as long as a query
    can tell the two populations apart. If it cannot, enforced traffic silently
    dilutes the shadow evidence and the rollout promotes itself.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "index": 0,
                "function": {"name": "context__snapshot", "arguments": "{}"},
            }
        ],
        finish_reason="tool_calls",
    )
    with structlog.testing.capture_logs() as logs:
        _post(enforced_client(), **_turn(session_id="enf-stamp"))
    (result,) = [r for r in logs if r.get("event") == "tool_projection_result"]
    assert result["enforced"] is True
    assert result["reason"] == tool_projection.REASON_ENFORCED
    assert result["outside_proposed_count"] == 0
    assert result["measured"] is True
    # The count the request actually carried, not the projection's view of it.
    assert result["offered"] == 1


def test_a_continuation_round_keeps_enforcing_against_the_reused_set(
    enforced_client, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """A real continuation: state token carried, selector deliberately not re-run.

    The gate reads `decision.degraded`, never `decision.ran`, and this is the only
    test that exercises the difference. The selector does not run on ordinary
    tool-continuation rounds, so a gate that had required a fresh run would send
    the full inventory on every round after the first — quietly giving back the
    saving exactly where a turn spends most of its rounds.

    It also pins the round-to-round STABILITY the cache argument rests on: the
    active set did not change, so neither may the tools.
    """
    app = enforced_client()
    tools = [
        CORE_TOOL,
        _named("terminal__rename"),
        _named("terminal__extract__json"),
        _named("daintree__call"),
        _named("skill__step__advance"),
        WITHHELD_TOOL,
    ]
    fake_provider.selection_result = selection_result(["daintree.terminals.rename"])
    fake_provider.complete_result = make_result(content="first round")
    first = _post(app, **_turn(session_id="enf-cont", tools=tools))
    assert first.status_code == 200
    state = first.json()["state"]
    first_sent = _sent_tool_names(fake_provider)
    selector_calls = len(fake_provider.selection_requests)

    # Same turn_id, next round, carrying the state token — the shape that makes
    # `_should_run_selector` decline.
    fake_provider.complete_result = make_result(content="second round")
    second = _post(
        app,
        **_turn(
            session_id="enf-cont",
            tools=tools,
            state=state,
            round=1,
            messages=[
                {"role": "user", "content": "what's open?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "terminal__rename", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "renamed"},
            ],
        ),
    )
    assert second.status_code == 200
    assert len(fake_provider.selection_requests) == selector_calls, (
        "the selector re-ran, so this is not a continuation round"
    )
    assert _sent_tool_names(fake_provider) == first_sent, (
        "an unchanged active set must produce an unchanged tools array — the whole "
        "cache argument for enforcement is that it is stable between topic shifts"
    )
    assert "forge__getPR" not in first_sent, "nothing was filtered, so this proves nothing"
