"""The CLI's stream watchdog must never fire on a turn the server is still working.

The client aborts a respond stream after ``sseIdleTimeout`` with no read progress
(``cli/internal/backend/sse.go``). The server's answer to that is
``_stream_with_heartbeat``: an SSE comment every ``HEARTBEAT_INTERVAL_SECONDS``
whenever the inner stream is quiet — through selection, the provider open, its
retries, and any generation gap. Comment lines are invisible to SSE event parsing
and DO count as read progress, so the watchdog resets on them.

That makes heartbeat cadence, not any deadline, the cross-repo invariant. It is
worth pinning because the failure is silent from both sides: the client kills a
turn the server was still recovering and replays it, charging a second model call,
with nothing in the server's logs implicating the server.

An earlier version of this file pinned the WRONG invariant — it asserted that
``open_stall × attempts + backoff`` cleared the client window, on the premise that
the server writes nothing between ``meta`` and the first delta. The heartbeat had
made that false since before the deadline work started. It would have rejected a
safe increase to the open allowance while protecting nothing, which is why the
second test here proves the heartbeat behaviourally rather than trusting a
constant to mean the stream is alive.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fakes import FakeProvider

from daintree_assistant_server.services.chat_orchestrator import (
    HEARTBEAT_INTERVAL_SECONDS,
)
from daintree_assistant_server.services.deadlines import BudgetPolicy

# The vendored CLI is the authority, but it is a submodule that may not be
# checked out; skip rather than hardcode a second copy of the number. CI checks
# it out explicitly (`submodules: true`), so this does not quietly stop running
# where it matters.
_SSE_SOURCE = (
    Path(__file__).resolve().parents[2] / "cli" / "internal" / "backend" / "sse.go"
)

# How many heartbeats may be lost before the client is entitled to call the stream
# dead. One missed beat is a flush hiccup; several in a row is a real silence.
_MIN_TOLERATED_MISSES = 3


def _cli_sse_idle_timeout_seconds() -> float:
    if not _SSE_SOURCE.exists():  # pragma: no cover - submodule not checked out
        pytest.skip("cli/ submodule not checked out; run git submodule update --init cli")
    match = re.search(
        r"sseIdleTimeout\s*=\s*(\d+)\s*\*\s*time\.Second", _SSE_SOURCE.read_text()
    )
    if match is None:  # pragma: no cover - the constant was renamed or restructured
        pytest.fail(
            f"could not find sseIdleTimeout in {_SSE_SOURCE}; the CLI's stream idle "
            "window moved or was renamed — re-derive the heartbeat cadence against it"
        )
    return float(match.group(1))


def test_the_heartbeat_cadence_clears_the_clients_idle_window() -> None:
    window = _cli_sse_idle_timeout_seconds()
    assert window >= HEARTBEAT_INTERVAL_SECONDS * _MIN_TOLERATED_MISSES, (
        f"the server heartbeats every {HEARTBEAT_INTERVAL_SECONDS}s but the CLI "
        f"aborts a stream after {window:.0f}s without read progress "
        "(sseIdleTimeout in cli/internal/backend/sse.go), leaving no room for a "
        "missed beat. Speed the cadence up or raise the CLI's window in lockstep."
    )


def test_no_deadline_can_outlast_the_clients_window_unheartbeaten() -> None:
    """A sanity check on the DIRECTION of the coupling.

    The deadlines are free to exceed the client window only because the heartbeat
    keeps the connection demonstrably alive. If the heartbeat were ever removed,
    this records which numbers would immediately become client-side aborts instead
    of the attributable server errors they are meant to be.
    """
    policy = BudgetPolicy()
    window = _cli_sse_idle_timeout_seconds()
    unheartbeaten = [
        name
        for name in ("open_stall", "first_output", "total")
        if getattr(policy, name) > window
    ]
    assert unheartbeaten == ["first_output", "total"], (
        f"which budgets outlast the client's {window:.0f}s window changed: "
        f"{unheartbeaten}. That is only safe while _stream_with_heartbeat runs — "
        "if the heartbeat is gone, these are client-side aborts now."
    )


@pytest.mark.asyncio
async def test_a_stalled_open_still_heartbeats(
    settings, fake_provider: FakeProvider, monkeypatch
) -> None:
    """The behavioural half, and the one that would have caught the false premise.

    A provider that takes the request and goes quiet is exactly when the client's
    watchdog is closest to firing, so it is exactly when a heartbeat has to be on
    the wire. Asserted end-to-end because a constant cannot prove a byte was
    written.
    """
    from daintree_assistant_server.main import build_container
    from daintree_assistant_server.services import chat_orchestrator as orch

    monkeypatch.setattr(orch, "HEARTBEAT_INTERVAL_SECONDS", 0.05)
    policy = BudgetPolicy(
        preparation=5.0, open_stall=0.4, first_output=8.0, stream_idle=5.0, total=10.0
    )
    monkeypatch.setattr(orch, "RequestBudget", _budget_factory(policy))

    class _Silent:
        usage = None
        endpoint = None

        async def prefetch_first_event(self):  # type: ignore[no-untyped-def]
            await asyncio.sleep(30)
            return None

        def __aiter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __anext__(self):  # type: ignore[no-untyped-def]
            raise StopAsyncIteration

        async def aclose(self) -> None:
            return None

    async def _open(request):  # type: ignore[no-untyped-def]
        return _Silent()

    fake_provider.open_stream = _open  # type: ignore[method-assign]
    container = build_container(settings, fake_provider)

    from pydantic import SecretStr

    from daintree_assistant_server.contracts.daintree_api import RespondRequest

    body = RespondRequest.model_validate(
        {
            "protocol_version": 3,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
            "startup": {},
            "generation": {"stream": True},
        }
    )
    outcome = await container.orchestrator.complete(
        body, api_key=SecretStr("k"), request_id="req_hb"
    )
    assert outcome.stream is not None
    parts: list[str] = []
    async with asyncio.timeout(10.0):
        async for chunk in outcome.stream:
            parts.append(chunk)
    joined = "".join(parts)

    assert ": hb" in joined, (
        "the server went silent while the upstream stalled — the CLI's idle "
        "watchdog would abort and replay the turn, and every deadline here that "
        "exceeds its window would become a client-side timeout instead"
    )
    await container.aclose()


def _budget_factory(policy):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.services.deadlines import RequestBudget

    def _make(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.pop("policy", None)
        return RequestBudget(*args, policy=policy, **kwargs)

    return _make
