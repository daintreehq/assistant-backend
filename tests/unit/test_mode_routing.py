"""A turn stays on ONE route — asserted with a distinct fake per mode.

This is the central design claim of the mode split, and until this file it had
no coverage at all: every other test injects one fake under BOTH modes (via
``modes.uniform_registry``), so a bug that re-resolved to ``DEFAULT_MODE``
halfway through a Pro turn would reach the same object and pass silently. The
suite would have been green through the exact regression the design exists to
prevent.

What "one route" has to mean, and each clause is separately load-bearing:

* the provider that serves the first attempt serves every retry of it;
* a JSON repair goes back to the provider whose output it is repairing;
* a bare-intent re-roll — a whole second completion — does not switch vendors;
* a stream that re-opens re-opens on the same host;
* the model id sent is the one the route names, not another mode's.

The consequence of getting it wrong is not a crash. Both adapters answer the
same calls, so a mis-routed turn returns a perfectly good answer from the wrong
model, billed to the wrong vendor, under the label the user chose — and nothing
reports it.
"""

from __future__ import annotations

import pytest
from fakes import FakeProvider, make_result
from pydantic import SecretStr

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError, upstream_error
from daintree_assistant_server.modes import (
    MODE_SPECS,
    ModeRegistry,
    OperationMode,
    ResolvedMode,
)
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.task_runner import TaskRunner


@pytest.fixture
def per_mode_fakes() -> dict[OperationMode, FakeProvider]:
    """One recording fake per mode — the whole point of this module."""
    return {mode: FakeProvider() for mode in MODE_SPECS}


@pytest.fixture
def split_registry(per_mode_fakes: dict[OperationMode, FakeProvider]) -> ModeRegistry:
    return ModeRegistry(
        {mode: ResolvedMode(spec, per_mode_fakes[mode]) for mode, spec in MODE_SPECS.items()}
    )


def _orchestrator(settings, catalog_manager, selector, state_codec, assembler, registry):
    return ChatOrchestrator(
        settings=settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        modes=registry,
    )


def _request(**generation: object) -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s-route", "turn_id": "t1"},
            "startup": {},
            "input": {"messages": [{"role": "user", "content": "hello"}]},
            "generation": generation or {"stream": False},
        }
    )


#: A reply that announces an action and emits no tool call — the shape the
#: orchestrator discards and re-rolls. Needs a tool ON OFFER, or there is nothing
#: the model could have called and the reply is just an answer.
_DEAD_ROUND = "All five votes are in. Let me read them, one read per terminal, batched."
_INTENT_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal__extract",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["terminalIds"],
            "properties": {"terminalIds": {"type": "array", "items": {"type": "string"}}},
        },
    },
}


def _intent_request() -> RespondRequest:
    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s-route-intent", "turn_id": "t1"},
            "startup": {},
            "input": {
                "messages": [{"role": "user", "content": "read the votes"}],
                "tools": [_INTENT_TOOL],
            },
            "generation": {"stream": False},
        }
    )


def _other(mode: OperationMode) -> OperationMode:
    return OperationMode.PRO if mode is OperationMode.STANDARD else OperationMode.STANDARD


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODE_SPECS))
async def test_a_turn_reaches_only_its_own_route(
    mode: OperationMode,
    settings: Settings,
    catalog_manager,
    selector,
    state_codec,
    assembler,
    split_registry: ModeRegistry,
    per_mode_fakes: dict[OperationMode, FakeProvider],
) -> None:
    """The mode's provider is called, the other's is not touched at all.

    The negative half is the half that matters: `len(mine) == 1` passes even if
    the other provider was also called, and "we sent the turn to both vendors"
    is a worse bug than sending it to neither.
    """
    mine, theirs = per_mode_fakes[mode], per_mode_fakes[_other(mode)]
    mine.complete_result = make_result(content="done")
    theirs.complete_result = make_result(content="done")

    orchestrator = _orchestrator(
        settings, catalog_manager, selector, state_codec, assembler, split_registry
    )
    await orchestrator.complete(_request(), route=split_registry.resolve(mode))

    assert mine.requests, f"{mode.value} never reached its own provider"
    assert not theirs.requests, (
        f"a {mode.value} turn also called the {_other(mode).value} provider — both "
        "vendors were billed and only one label was shown"
    )
    # And the model is the route's, not merely "some model this fake accepted".
    assert {r.model for r in mine.requests} == {MODE_SPECS[mode].upstream_model}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODE_SPECS))
async def test_a_bare_intent_reroll_stays_on_the_route(
    mode: OperationMode,
    settings: Settings,
    catalog_manager,
    selector,
    state_codec,
    assembler,
    split_registry: ModeRegistry,
    per_mode_fakes: dict[OperationMode, FakeProvider],
) -> None:
    """The re-roll is a SECOND completion, which is where a re-resolve would hide.

    The first reply announces an action and emits no tool call, so the
    orchestrator discards it and asks again. That second call is built from the
    prepared request rather than from the original arguments, so it is exactly
    the path where reading a registry instead of the carried route would be
    invisible — both calls succeed either way.
    """
    mine, theirs = per_mode_fakes[mode], per_mode_fakes[_other(mode)]
    mine.complete_result = [
        make_result(content=_DEAD_ROUND),
        make_result(content="Read five votes; here is the tally."),
    ]
    theirs.complete_result = make_result(content="wrong vendor")

    orchestrator = _orchestrator(
        settings, catalog_manager, selector, state_codec, assembler, split_registry
    )
    await orchestrator.complete(_intent_request(), route=split_registry.resolve(mode))

    assert len(mine.requests) >= 2, "the bare-intent re-roll did not happen"
    assert not theirs.requests, (
        f"the {mode.value} re-roll landed on the {_other(mode).value} provider"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODE_SPECS))
async def test_a_stream_opens_only_on_its_own_route(
    mode: OperationMode,
    settings: Settings,
    catalog_manager,
    selector,
    state_codec,
    assembler,
    split_registry: ModeRegistry,
    per_mode_fakes: dict[OperationMode, FakeProvider],
) -> None:
    mine, theirs = per_mode_fakes[mode], per_mode_fakes[_other(mode)]
    orchestrator = _orchestrator(
        settings, catalog_manager, selector, state_codec, assembler, split_registry
    )
    outcome = await orchestrator.complete(
        _request(stream=True), route=split_registry.resolve(mode)
    )
    assert outcome.stream is not None
    async for _event in outcome.stream:
        pass

    assert mine.stream_requests, f"{mode.value} never opened a stream on its own provider"
    assert not theirs.stream_requests, (
        f"a {mode.value} stream opened against the {_other(mode).value} provider"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODE_SPECS))
async def test_a_task_and_its_json_repair_stay_on_one_route(
    mode: OperationMode,
    settings: Settings,
    prompt_loader,
    catalog_manager,
    split_registry: ModeRegistry,
    per_mode_fakes: dict[OperationMode, FakeProvider],
) -> None:
    """A repair must go back to the provider whose output it is repairing.

    Repairing vendor A's malformed JSON by asking vendor B is not a repair — the
    second model never produced the text, and the retry silently becomes a
    different question answered by a different model at a different price.
    """
    mine, theirs = per_mode_fakes[mode], per_mode_fakes[_other(mode)]
    good = '{"matched": true, "confidence": 0.9, "reason": "the command finished"}'
    mine.complete_result = [make_result(content="not json at all"), make_result(content=good)]
    theirs.complete_result = make_result(content=good)

    runner = TaskRunner(
        settings=settings,
        modes=split_registry,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
    )
    request = TaskRequest(
        task="terminal_judge",
        input={"tail": "build finished", "question": "did the build finish?"},
    )
    result = await runner.run(request, route=split_registry.resolve(mode))

    assert len(mine.requests) == 2, "the JSON repair retry did not happen on this route"
    assert not theirs.requests, (
        f"the {mode.value} repair retry landed on the {_other(mode).value} provider"
    )
    # The REPORTED model must be the route's too, or a Pro task tells the client
    # it ran on Standard.
    assert result.model == MODE_SPECS[mode].upstream_model
    # ...and both calls carry the mode's own cache namespace, so the two tiers
    # never claim to share one upstream cache lineage.
    for sent in mine.requests:
        assert sent.cache_session_id is not None
        assert MODE_SPECS[mode].cache_namespace in sent.cache_session_id


@pytest.mark.asyncio
async def test_a_transient_retry_does_not_change_vendors(
    settings: Settings,
    catalog_manager,
    selector,
    state_codec,
    assembler,
    split_registry: ModeRegistry,
    per_mode_fakes: dict[OperationMode, FakeProvider],
) -> None:
    """Retries are bounded to the resolved route, never a fallback across it.

    Cross-mode fallback is the one recovery this design forbids outright: it
    would change quality, cost and cache lineage under a label the user chose,
    and do it invisibly. A retry that reached the other provider would look like
    a successful recovery in every log line.
    """

    pro, standard = per_mode_fakes[OperationMode.PRO], per_mode_fakes[OperationMode.STANDARD]
    pro.complete_error = upstream_error("upstream is down")
    standard.complete_result = make_result(content="the wrong vendor answered")

    orchestrator = _orchestrator(
        settings, catalog_manager, selector, state_codec, assembler, split_registry
    )
    with pytest.raises(DaintreeError) as exc:
        await orchestrator.complete(
            _request(), route=split_registry.resolve(OperationMode.PRO)
        )
    # The caller is told the upstream failed, not handed a Standard answer.
    assert exc.value.code.startswith("upstream")

    assert not standard.requests, (
        "a failing Pro turn fell back to Standard. The turn must fail AS PRO — the "
        "caller chose a tier and is owed an error, not a quietly cheaper answer."
    )


def test_the_registry_refuses_a_spec_filed_under_the_wrong_mode() -> None:
    """The construction mistake that would make every routing test above lie.

    A registry mapping `PRO -> ResolvedMode(STANDARD_SPEC, cerebras)` sends Pro
    turns to the right provider with the wrong model, and every "did the right
    provider get called" assertion still passes.
    """
    fake = FakeProvider()
    with pytest.raises(ValueError, match="holds the"):
        ModeRegistry(
            {
                OperationMode.STANDARD: ResolvedMode(MODE_SPECS[OperationMode.STANDARD], fake),
                OperationMode.PRO: ResolvedMode(MODE_SPECS[OperationMode.STANDARD], fake),
            }
        )


def test_the_registry_refuses_an_incomplete_map() -> None:
    """Every mode must resolve, or a request for it raises KeyError at runtime."""
    fake = FakeProvider()
    with pytest.raises(ValueError, match="missing"):
        ModeRegistry(
            {OperationMode.STANDARD: ResolvedMode(MODE_SPECS[OperationMode.STANDARD], fake)}
        )


def test_closing_one_failing_provider_still_closes_the_other() -> None:
    """Best-effort teardown has to be per PROVIDER, not per registry.

    With one provider a raising `aclose` ended teardown either way. With two, an
    exception escaping the loop strands the second one's connection pool — and
    `Container.aclose` suppresses registry-level exceptions, so the leak is
    completely silent.
    """
    import asyncio

    class _Exploding(FakeProvider):
        async def aclose(self) -> None:
            raise RuntimeError("this provider's client is already gone")

    exploding, healthy = _Exploding(), FakeProvider()
    registry = ModeRegistry(
        {
            OperationMode.STANDARD: ResolvedMode(MODE_SPECS[OperationMode.STANDARD], exploding),
            OperationMode.PRO: ResolvedMode(MODE_SPECS[OperationMode.PRO], healthy),
        }
    )
    asyncio.run(registry.aclose())
    assert healthy.closed, (
        "the second provider was never closed because the first one raised"
    )


def test_a_shared_provider_is_closed_exactly_once() -> None:
    """`uniform_registry` binds one object under both modes; a double close is
    the kind of error that only appears during shutdown, where nobody looks."""
    import asyncio

    from daintree_assistant_server.modes import uniform_registry

    class _Counting(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    shared = _Counting()
    asyncio.run(uniform_registry(shared).aclose())
    assert shared.close_calls == 1


def test_an_unknown_provider_id_never_yields_a_credential(settings: Settings) -> None:
    """Credential lookup must fail CLOSED.

    The `together if x == "together" else cerebras` shape this replaced handed
    out a real Cerebras secret for any id it did not recognise — a typo, a new
    provider added to a spec before its settings fields, a value read back off
    the wire. A wrong key is not a degraded answer; it is a credential sent to a
    vendor that was never meant to see it.
    """
    with pytest.raises(KeyError):
        settings.provider_api_key("openai")
    with pytest.raises(KeyError):
        settings.provider_base_url("")
    assert SecretStr  # imported for the type-level contract above
