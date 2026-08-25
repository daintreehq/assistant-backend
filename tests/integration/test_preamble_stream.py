"""The `preamble` event on a real stream: order, terminality, and staying off.

Doc section 15.4. These drive the actual endpoint rather than the orchestrator's
internals, because the properties that matter here are properties of the BYTES a
client receives — the order they arrive in, and what arrives when the feature is
not switched on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import structlog.testing
from conftest import parse_sse, respond_body
from fakes import FakeStream, selection_result

from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.services.fast_response import (
    FastResponseResult,
    FastResponseService,
)

#: In the COMPREHENSION register the stage now requires, not the announcement
#: register — an "I'll …" fixture is refused by `validate_preamble` these days,
#: which is the point: the executor owns that opening and two of them in two
#: seconds is the duplicate this whole design exists to avoid.
PREAMBLE = (
    "A build that fails only some of the time usually means state left over "
    "between runs rather than the code itself."
)


#: Every Cerebras request the fake transport saw. `"preamble" not in names` is
#: satisfied identically by "the stage ran and declined" and "the stage never ran
#: at all", so a test asserting the first needs this to tell them apart.
CEREBRAS_CALLS: list[httpx.Request] = []


def _cerebras(text: str = PREAMBLE, *, status: int = 200) -> CerebrasProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        CEREBRAS_CALLS.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "no"}})
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "created": 1,
                "model": "gpt-oss-120b",
                "choices": [
                    {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    # A reported cost, so the turn's accounting is OBSERVABLE.
                    # Without one `turn_cost` returns None ("we do not know"),
                    # which is correct but says nothing about the wiring.
                    "cost": 0.000123,
                },
            },
        )

    from pydantic import SecretStr

    return CerebrasProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key=SecretStr("c")
    )


def _enable(
    client: Any,
    fake_provider: Any,
    mode: str = "visible",
    **provider_kwargs: Any,
) -> None:
    """Turn the stage on for the container the route actually reads.

    Also scripts a real runbook into the active set: the eligibility policy skips
    a turn with none loaded, so without this every test here would pass by not
    running the feature at all.
    """
    CEREBRAS_CALLS.clear()
    fake_provider.selection_result = selection_result(["daintree.foundation"])
    container = client.app.state.container
    # `Settings` is a frozen Pydantic model, not a dataclass.
    settings = container.settings.model_copy(
        update={"fast_response_mode": mode, "fast_response_rollout_percent": 100}
    )
    container.orchestrator.settings = settings
    container.orchestrator.fast_response = FastResponseService(
        settings=settings,
        prompt_loader=container.prompt_loader,
        provider=_cerebras(**provider_kwargs),
    )


def _executor_reports_cost(fake_provider: Any, cost: float = 0.004) -> None:
    """Give the EXECUTOR a known cost.

    Without it `main` is unknown and `cost.complete` is False whatever the fast
    stage did — so a test asserting `complete` would be measuring the fake rather
    than the wiring.
    """
    from fakes import make_text_chunks

    # Via the chunks, not `FakeStream(usage=...)`: the stream's final usage chunk
    # overwrites that attribute, so the constructor argument would be silently
    # discarded and the test would measure the fake rather than the wiring.
    fake_provider.stream_chunks = make_text_chunks("hello world", cost=cost)


def _done_event(client: Any, auth: dict[str, str]) -> dict[str, Any]:
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "fix the build"}], generation={"stream": True}
        ),
    ) as response:
        events = parse_sse("".join(response.iter_text()))
    return next(data for name, data in events if name == "done")


def _stream(client: Any, auth: dict[str, str], **body_kwargs: Any) -> list[str]:
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "fix the failing build"}],
            generation={"stream": True},
            **body_kwargs,
        ),
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    return [name or "" for name, _ in parse_sse(text)]


def test_the_stage_is_off_by_default_and_emits_no_preamble(
    client: Any, auth: dict[str, str]
) -> None:
    """The property that makes this safe to merge: a deployment that configured
    nothing sees exactly the stream it saw before."""
    names = _stream(client, auth)
    assert "preamble" not in names
    assert names[0] == "meta"
    assert names[-1] == "done"


def test_meta_stays_first_and_done_stays_terminal_with_a_preamble(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """`meta` carries the runbook state the client needs to initialise the bubble,
    so it must precede any visible text. And `done` is terminal because readers
    modelled on the `data: [DONE]` convention break their loop on one.
    """
    _enable(client, fake_provider)
    names = _stream(client, auth)

    assert names[0] == "meta"
    assert "preamble" in names
    assert names[-1] == "done"
    assert names.count("done") == 1


def test_the_preamble_arrives_before_any_executor_delta(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The whole point: visible text before the executor has produced anything."""
    _enable(client, fake_provider)
    names = _stream(client, auth)

    assert names.index("preamble") < names.index("delta")
    # EXACTLY one. The gate is polled from four places and the bare-intent
    # re-roll re-runs the open race, so "at least one" would pass a double emit.
    assert names.count("preamble") == 1
    # ...and it is bracketed by the two phase markers, so a client can label the
    # bubble without guessing.
    status_positions = [i for i, n in enumerate(names) if n == "status"]
    assert status_positions[0] < names.index("preamble") < status_positions[1]


def test_the_preamble_payload_carries_its_commit_semantics(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """A client that renders it without holding it provisional commits a turn
    that may still end in an error."""
    _enable(client, fake_provider)
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "fix the build"}], generation={"stream": True}
        ),
    ) as response:
        events = parse_sse("".join(response.iter_text()))

    payload = next(data for name, data in events if name == "preamble")
    assert payload["content"] == PREAMBLE
    assert payload["provisional"] is True
    assert payload["commit_on"] == "done"
    assert payload["id"].startswith("pre_")
    # No provider or model identity anywhere in it.
    rendered = str(payload).lower()
    # The CURRENT fast model is on this list. It was not, which meant the one
    # name most likely to leak was the one name not checked for.
    for internal in ("cerebras", "gemma", "gpt-oss", "deepseek", "together"):
        assert internal not in rendered


def test_shadow_mode_runs_the_stage_and_shows_nothing(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The stage generates, validates and traces a real preamble — and the user's
    stream is byte-for-byte the one they would have got without it."""
    _enable(client, fake_provider, mode="shadow")
    names = _stream(client, auth)

    assert "preamble" not in names
    assert names[0] == "meta"
    assert names[-1] == "done"


def test_a_failing_fast_provider_does_not_disturb_the_turn(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The stage is best effort. A dead Cerebras costs a preamble, never a turn."""
    _enable(client, fake_provider, status=500)
    names = _stream(client, auth)

    assert names[0] == "meta"
    assert names[-1] == "done"
    assert "error" not in names
    # And NOTHING is shown. There is no deterministic fallback sentence any more:
    # the model is asked to judge whether a line is worth writing, and a
    # server-authored filler emitted on failure would contradict that judgement.
    # Without this assertion, reintroducing one would leave the test green.
    assert "preamble" not in names


def test_a_continuation_round_gets_no_second_preamble(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """A fresh preamble before every tool round is the behaviour that would make
    the feature obnoxious — and the user has already seen one for this
    instruction."""
    _enable(client, fake_provider)
    names = _stream(client, auth, session_id="s-cont", turn_id="t-cont", round=3)
    assert "preamble" not in names


def test_a_non_streaming_turn_never_produces_a_preamble(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """There is no first byte to improve; a second awaited call is pure added
    latency."""
    _enable(client, fake_provider)
    response = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": "fix it"}]),
    )
    assert response.status_code == 200
    body = response.json()
    assert PREAMBLE not in body["message"]["content"]


def test_the_executor_request_is_byte_identical_in_every_mode(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The strongest statement the concurrent design makes.

    The preamble is no longer part of the executor's input — the two calls leave
    together, so it CANNOT be. That turns what used to be "the system region is
    unchanged" into "the whole request is unchanged", which is also what makes
    `FAST_RESPONSE_MODE=off` a genuine no-op on the wire rather than a
    different-but-similar request. Rollback is a config-only revision.

    Compared across all three modes in one test rather than parametrized: the
    claim is that they AGREE, and three separate runs each asserting their own
    shape cannot say that.
    """
    sent: dict[str, Any] = {}
    for mode in ("off", "shadow", "visible"):
        _enable(client, fake_provider, mode=mode)
        # Streaming turns land in `stream_requests`, not `requests`.
        fake_provider.stream_requests.clear()
        _stream(client, auth)
        request = fake_provider.stream_requests[-1]
        # The WHOLE request, not just `messages`. The handoff test this replaces
        # compared `tools` explicitly, and dropping to messages-only would let a
        # mutation that varied the tool array, the model, the token cap or the
        # reasoning posture by mode go straight through. `api_key` is excluded
        # because it is `repr=False` and identical by construction.
        sent[mode] = {
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": request.tool_choice,
            "response_format": request.response_format,
            "model": request.model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": request.stream,
            "thinking_disabled": request.thinking_disabled,
            "reasoning_effort": request.reasoning_effort,
        }

    assert sent["off"] == sent["shadow"] == sent["visible"]
    assert len([m for m in sent["visible"]["messages"] if m.get("role") == "system"]) == 2
    assert PREAMBLE not in json.dumps(sent["visible"]["messages"])


def test_a_preamble_that_loses_the_race_is_dropped_rather_than_shown(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The case the concurrent design has to handle and the sequential one could
    not have.

    Cerebras is expected to win by a wide margin, but nothing enforces that. When
    the executor's first visible byte arrives first, the line is discarded
    unshown: the answer is already on screen, and prefixing it with a preview of
    work the reader can watch happening is worse than silence.
    """
    _enable(client, fake_provider)
    service = client.app.state.container.orchestrator.fast_response

    async def slow(*args: Any, **kwargs: Any) -> Any:
        # Long enough to lose to a fake provider that answers instantly, short
        # enough that the turn does not wait on it — which is the point: nothing
        # awaits this any more.
        await asyncio.sleep(0.2)
        return FastResponseResult(content=PREAMBLE, source="model")

    service.generate = slow  # type: ignore[method-assign]
    names = _stream(client, auth)

    assert "preamble" not in names, "a line that arrives after the answer must be dropped"
    assert names[-1] == "done", "and the turn is otherwise untouched"


def test_a_dropped_preamble_is_recorded_as_a_drop_and_not_as_a_failure(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """Losing the race is its own outcome. Folding it into the failure count
    would make a healthy Cerebras look broken during a rollout, and folding it
    into the success count would hide the one thing worth rolling back for."""
    _enable(client, fake_provider)
    service = client.app.state.container.orchestrator.fast_response

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return FastResponseResult(content=PREAMBLE, source="model")

    service.generate = slow  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as logs:
        _stream(client, auth)

    dropped = [e for e in logs if e.get("event") == "fast_response_dropped"]
    assert dropped, "the drop must be observable"
    assert dropped[0]["reason"] == "executor_output_first"


def test_shadow_mode_is_traced_even_though_nothing_is_shown(
    client: Any, auth: dict[str, str], fake_provider: Any, tmp_path: Any
) -> None:
    """Shadow exists to produce evidence, so a trace written only on the visible
    branch would capture nothing from the stage of the rollout that needs it."""
    from daintree_assistant_server.tracing import (
        DefaultTraceRecorder,
        FilesystemTraceSink,
        decide,
    )

    _enable(client, fake_provider, mode="shadow")
    client.app.state.container.orchestrator.trace_recorder = DefaultTraceRecorder(
        sink=FilesystemTraceSink(tmp_path),
        identity_key=b"k",
        eligibility=decide(mode="full_internal", is_hardened=False),
        max_event_bytes=8 * 1024 * 1024,
        max_pending_bytes=32 * 1024 * 1024,
        flush_grace_seconds=2.0,
    )

    names = _stream(client, auth)
    assert "preamble" not in names

    import json

    directory = next(p for p in tmp_path.iterdir() if p.is_dir())
    manifest = json.loads((directory / "manifest.json").read_text())
    fast = [i for i in manifest["invocations"] if i["stage"] == "fast_response"]
    assert fast, f"the shadow call was not traced: {manifest['invocations']}"
    # Generated and billed, but never delivered — and the record says which.
    assert fast[0]["accepted"] is False
    assert fast[0]["discard_reason"] == "shadow_mode"


def test_the_turn_completes_even_if_the_fast_call_never_returns(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The concurrency claim, asserted directly.

    Under the previous design the executor could not open until the preamble
    existed — its bytes were part of that request — so a hung Cerebras call
    stalled the turn up to `FAST_RESPONSE_TIMEOUT_SECONDS`. Nothing awaits it now,
    and a call that never returns at all costs the line and nothing else.

    A hang rather than a slow call on purpose: a sleep long enough to be
    convincing is also long enough to make this test flaky on a loaded machine,
    while "never" cannot be beaten by an unlucky scheduler. It is RELEASED at the
    end — `drain_background_tasks` waits up to 30s for outstanding work at
    shutdown, and a task that truly never returns would spend all of it. In
    production the call bounds itself at `FAST_RESPONSE_TIMEOUT_SECONDS`.
    """
    _enable(client, fake_provider)
    service = client.app.state.container.orchestrator.fast_response
    started = asyncio.Event()
    release = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return FastResponseResult(content=PREAMBLE, source="model")

    service.generate = hang  # type: ignore[method-assign]
    try:
        names = _stream(client, auth)
    finally:
        release.set()

    assert names[-1] == "done"
    assert "preamble" not in names
    # And it really was started — a policy skip would also produce the above.
    assert started.is_set(), "the fast call never left, so nothing was raced"


def test_a_chunk_carrying_prose_AND_a_tool_call_closes_the_window(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The ordering hole this rework exposed.

    A provider can put prose and a tool-call fragment in the SAME chunk. That
    branch used to yield the prose without marking first output — so the preamble
    window stayed open after visible text had already reached the screen, and a
    line arriving on the next chunk was rendered UNDER the answer it was supposed
    to precede. (It also meant `first_output_ms` reported the later moment the
    tool call completed.)

    Timed exactly rather than by sleeping. The fast call is released by the STREAM
    itself, as it hands over its second chunk: that puts the preamble's arrival
    strictly after the first chunk's prose has reached the client and strictly
    before the next poll — the one interval where the bug is visible, and one a
    sleep could not reliably hit.
    """
    _enable(client, fake_provider)
    release = asyncio.Event()

    class ReleasingStream(FakeStream):
        async def _advance(self) -> Any:
            chunk = await super()._advance()
            if self._index == 2:
                # The prose from chunk 1 is already on the wire.
                release.set()
                # Hand the loop over so the fast task actually resolves before
                # this chunk is processed.
                await asyncio.sleep(0)
            return chunk

    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "content": "Starting on it. ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "fs__read", "arguments": ""},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"path":"a"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    fake_provider.stream_factory = lambda _request: ReleasingStream(chunks)

    service = client.app.state.container.orchestrator.fast_response

    async def released_mid_stream(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return FastResponseResult(content=PREAMBLE, source="model")

    service.generate = released_mid_stream  # type: ignore[method-assign]
    try:
        names = _stream(client, auth)
    finally:
        release.set()

    assert "delta" in names, "the scripted chunks must actually have streamed"
    assert "preamble" not in names, "prose in a tool-call chunk must close the window"


def test_a_preamble_that_won_is_not_lost_when_the_stream_ends_first(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The false-LOSS case, which is the mirror of the drop and easier to miss.

    Polling at the top of the chunk loop is not enough. A stream can run to
    completion without a single visible delta — only reasoning, or a reply the
    bare-intent gate held — so nothing ever closed the window, and the fast call
    genuinely won. Without a final poll after the loop, the terminal flush closes
    the window and records a drop for a line that was sitting in hand.

    Timed off the stream's own end-of-iteration, so the fast call lands strictly
    after the last chunk and strictly before the terminal flush.
    """
    _enable(client, fake_provider)
    release = asyncio.Event()

    class ReleasingAtEnd(FakeStream):
        async def _advance(self) -> Any:
            chunk = await super()._advance()
            if chunk is None:
                # The stream is exhausted and nothing visible was ever yielded.
                release.set()
                await asyncio.sleep(0)
            return chunk

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "weighing it up"}}]},
        {"choices": [{"delta": {"reasoning_content": " some more"}, "finish_reason": "stop"}]},
    ]
    fake_provider.stream_factory = lambda _request: ReleasingAtEnd(chunks)

    service = client.app.state.container.orchestrator.fast_response

    async def released_at_end_of_stream(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        return FastResponseResult(content=PREAMBLE, source="model")

    service.generate = released_at_end_of_stream  # type: ignore[method-assign]
    try:
        names = _stream(client, auth)
    finally:
        release.set()

    assert "preamble" in names, "a line that beat every visible byte must be shown"
    assert names.index("preamble") < names.index("done")


def test_a_shown_preamble_reports_its_usage_and_its_latency(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The stage bills, so the turn has to say so.

    Two things that were silently disconnected. `done.cost` folds in the fast
    call's usage — drop it and the turn under-reports real spend on the server's
    own key. And `done.timings.fast_response_ms` was left unset entirely when the
    stage went concurrent: preparation used to fill it in while it awaited the
    call, and nothing replaced that.
    """
    _enable(client, fake_provider)
    _executor_reports_cost(fake_provider)
    done = _done_event(client, auth)

    assert done["timings"]["fast_response_ms"] is not None
    # The fake Cerebras reports an exact cost, so a usage that reached accounting
    # is distinguishable from one that quietly did not.
    assert done["cost"]["fast_response"] == pytest.approx(0.000123)
    assert done["cost"]["main"] == pytest.approx(0.004)
    assert done["cost"]["total"] == pytest.approx(0.004123)
    # Both components known, so the total is a real total rather than a floor.
    assert done["cost"]["complete"] is True


def test_a_fast_call_that_billed_uncountably_makes_the_total_a_floor(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The other half of the accounting contract, and the easier one to get wrong.

    `turn_cost` reads an absent fast usage as "the stage did not run" and sums an
    honest zero for it. That reading is wrong for a request that went out and came
    back uncountable — a provider error, a timeout the host may still have billed.
    The executor's cost is made known here precisely so `complete` can only be
    False because of the fast stage.
    """
    _enable(client, fake_provider, status=500)
    _executor_reports_cost(fake_provider)
    done = _done_event(client, auth)

    assert done["cost"]["main"] == pytest.approx(0.004)
    assert done["cost"].get("fast_response") is None
    assert done["cost"]["complete"] is False, "a billed-but-uncountable call must flag the total"


def test_the_decline_sentinel_shows_nothing_but_still_ran(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """End-to-end, not just at the validator.

    `"preamble" not in names` on its own proves nothing — a policy skip produces
    exactly the same stream — so this also asserts the call actually went out.
    """
    _enable(client, fake_provider, text="NONE")
    names = _stream(client, auth)

    assert "preamble" not in names
    assert names[-1] == "done"
    # The decisive assertion. Without it this test passes just as well when the
    # stage never runs — a policy skip produces a byte-identical stream — so it
    # would go green against a mutation that disabled the feature outright.
    assert len(CEREBRAS_CALLS) == 1, "the stage did not actually call Cerebras"


def test_the_history_budget_reaches_the_provider_end_to_end(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The recency-weighted crop, asserted on the BYTES that left the process.

    Unit tests cover the allocation; this covers the wiring — that what
    `_recent_user_messages` produced is what the Cerebras request actually
    carried, that the assistant's turns never joined it, and that the request the
    model is describing is the LAST row it reads.
    """
    _enable(client, fake_provider)
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [
                {"role": "user", "content": "Refactor the parser to stream its input."},
                {"role": "assistant", "content": "ASSISTANT-TURN-NEVER-SENT"},
                {"role": "user", "content": "actually do that now"},
            ],
            generation={"stream": True},
        ),
    ) as response:
        assert response.status_code == 200
        "".join(response.iter_text())

    assert len(CEREBRAS_CALLS) == 1
    body = json.loads(CEREBRAS_CALLS[0].content)
    rows = [m["content"] for m in body["messages"]]
    assert "ASSISTANT-TURN-NEVER-SENT" not in json.dumps(rows)
    # The short follow-up is the request, and the message that makes it legible
    # is above it — which is the entire reason history is sent at all.
    assert rows[-1] == "actually do that now"
    assert "Refactor the parser to stream its input." in rows[-2]
    # Only user rows reach the fast model: system prompt, runbooks, then history.
    assert [m["role"] for m in body["messages"]] == ["system", "user", "user", "user"]
