"""The `preamble` event on a real stream: order, terminality, and staying off.

Doc section 15.4. These drive the actual endpoint rather than the orchestrator's
internals, because the properties that matter here are properties of the BYTES a
client receives — the order they arrive in, and what arrives when the feature is
not switched on.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from conftest import parse_sse, respond_body
from fakes import selection_result

from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.services.fast_response import FastResponseService

PREAMBLE = "I'll look at the failing step and make the smallest change that fixes it."


def _cerebras(
    text: str = PREAMBLE, *, status: int = 200, delay: float = 0.0
) -> CerebrasProvider:
    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "no"}})
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "created": 1,
                "model": "gemma-4-31b",
                "choices": [
                    {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
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
    for internal in ("cerebras", "gemma", "deepseek"):
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


@pytest.mark.parametrize("mode", ["off", "shadow", "visible"])
def test_the_executor_prompt_prefix_is_unchanged_across_every_mode(
    client: Any, auth: dict[str, str], fake_provider: Any, mode: str
) -> None:
    """Rollback is a config-only revision, so `off` must reproduce today's request
    exactly — and `shadow` must too, since it shows nothing and hands off nothing.
    """
    _enable(client, fake_provider, mode=mode)
    # Streaming turns land in `stream_requests`, not `requests`.
    fake_provider.stream_requests.clear()
    _stream(client, auth)

    sent = fake_provider.stream_requests[-1]
    system_rows = [m for m in sent.messages if m.get("role") == "system"]
    assert len(system_rows) == 2
    if mode == "visible":
        assert sent.messages[-2]["content"] == PREAMBLE
        assert "server-owned instruction" in sent.messages[-1]["content"]
    else:
        assert sent.messages[-1]["role"] != "user" or PREAMBLE not in str(
            sent.messages[-1]["content"]
        )


def test_dropping_the_handoff_for_context_also_withholds_the_preamble(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The exactness contract, under the one path that can break it.

    When the assembled request only fails to fit once the handoff is on it, the
    pair is dropped and the turn proceeds. This runs during PREPARATION, before
    any byte is streamed — so the content must be dropped with it. Leaving it set
    would emit a preamble the executor never received, and the client would then
    commit a sentence the model has no knowledge of.
    """
    _enable(client, fake_provider)
    # Force the FIRST measurement to refuse — i.e. the one taken with the handoff
    # on — and let the re-measurement after it is dropped succeed.
    import daintree_assistant_server.services.chat_orchestrator as orch

    real_exceeds = orch.exceeds_window
    calls = {"n": 0}

    def only_the_first(measurement: Any) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    orch.exceeds_window = only_the_first  # type: ignore[assignment]
    try:
        names = _stream(client, auth)
    finally:
        orch.exceeds_window = real_exceeds  # type: ignore[assignment]

    assert "preamble" not in names, "a preamble the executor never saw must not be shown"
    assert names[-1] == "done", "and the turn still completes"
    sent = fake_provider.stream_requests[-1]
    assert PREAMBLE not in str(sent.messages[-1]["content"])


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


def test_a_late_preamble_is_dropped_rather_than_held_for(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The join deadline is a different question from the transport timeout, and
    one number used to answer both.

    A preamble that arrives after the executor was ready has already lost the
    argument it exists to win — perceived speed — and holding the substantive
    answer for it means the turn pays twice. Past the deadline it is dropped and
    the turn proceeds exactly as if the stage had not run."""
    _enable(client, fake_provider, mode="visible", delay=1.0)
    container = client.app.state.container
    container.orchestrator.settings = container.orchestrator.settings.model_copy(
        update={"fast_response_join_deadline_ms": 50}
    )
    started = time.perf_counter()
    names = _stream(client, auth)
    elapsed = time.perf_counter() - started
    assert "preamble" not in names
    assert names[-1] == "done"
    assert elapsed < 0.9, f"the turn waited {elapsed:.2f}s past the join deadline"


def test_a_prompt_preamble_still_arrives_under_the_join_deadline(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The deadline must not have quietly disabled the feature. A stage that
    answers in time is still shown, in the same position as before — otherwise
    the previous two tests would pass against a stage that never delivers."""
    _enable(client, fake_provider, mode="visible")
    names = _stream(client, auth)
    assert "preamble" in names
    assert names.index("meta") < names.index("preamble")
    assert names[-1] == "done"
