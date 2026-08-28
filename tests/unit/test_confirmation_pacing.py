"""Typing the confirmation out into the executor's gap.

The gate that decides WHICH confirmation is in `test_runbook_confirmations.py`;
what is tested here is the emission — the shape of the frames on the wire, and
the two bounds that make pacing safe to do at all.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from daintree_assistant_server.config import (
    CONFIRMATION_LEAD_IN_SECONDS,
    CONFIRMATION_MIN_PACED_WORDS,
    CONFIRMATION_PACING_SECONDS,
)
from daintree_assistant_server.contracts.daintree_api import RunbooksBlock
from daintree_assistant_server.services.chat_orchestrator import _RespondMeta, _Timings

TEXT = (
    "Diagnosing a failing check. The aim is to get from the symptom to the actual "
    "error output before proposing anything, since a check name says what failed "
    "and not why."
)


def _meta(text: str | None) -> _RespondMeta:
    return _RespondMeta(
        request_id="req_abc123",
        state="dst1.x.y",
        catalog_revision="sha256:rev",
        prompt_version="main",
        runbooks=RunbooksBlock(),
        confirmation=text,
        confirmation_runbook_id="a.one",
    )


@pytest.fixture
def paced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the typed path on.

    It is off in the shipped config while the panel's vendored CLI catches up
    (`test_pacing_is_off_in_the_shipped_config` says why). The mechanism is live
    and measured, and letting it rot untested until then is how it stops working
    before anyone can turn it back on.
    """
    from daintree_assistant_server.services import chat_orchestrator as orch

    monkeypatch.setattr(orch, "CONFIRMATION_PACING_ENABLED", True)


async def _collect(orchestrator: Any, text: str | None, open_task: Any = None) -> list[str]:
    frames: list[str] = []
    async for chunk in orchestrator._emit_confirmation(
        meta=_meta(text),
        timings=_Timings(),
        budget=None,
        trace=_NullTrace(),
        open_task=open_task,
    ):
        assert chunk.startswith("event: preamble\n")
        payload = json.loads(chunk.split("data: ", 1)[1])
        assert payload["provisional"] is True
        assert payload["commit_on"] == "done"
        frames.append(payload["content"])
    return frames


class _NullTrace:
    async def record_event(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_turn_with_no_confirmation_emits_nothing(orchestrator: Any) -> None:
    assert await _collect(orchestrator, None) == []


@pytest.mark.asyncio
async def test_frames_are_incremental_because_every_renderer_appends(
    orchestrator: Any, paced: None,
) -> None:
    """Fragments, not accumulations — and they must rebuild the text EXACTLY.

    Every renderer in the CLI appends: `host.Bridge` turns a preamble into an
    ordinary turn token and the console sink writes it, so a cumulative frame
    would stack quadratically in both. The client accumulates and commits the
    whole sentence.

    Byte-exact reassembly is the part that is easy to get wrong: splitting on
    whitespace and rejoining with a single space rewrites any authored text whose
    spacing differs, and the corruption would be invisible in a test that only
    compared word lists.
    """
    frames = await _collect(orchestrator, TEXT)

    assert len(frames) == len(TEXT.split()), "one fragment per word"
    assert "".join(frames) == TEXT, "fragments must rebuild the text byte for byte"
    assert frames[0] == TEXT.split()[0], "the first fragment carries no leading space"


@pytest.mark.asyncio
async def test_typing_finishes_inside_the_budget(orchestrator: Any, paced: None) -> None:
    """The whole point of the number: it fits in the gap it was measured against.

    `CONFIRMATION_PACING_SECONDS` is set from a live measurement of the
    executor's first delta (see `benchmarks/executor_window`), so an emission
    that overran it would be typing over the answer. A little slack above the
    budget covers scheduler jitter; the assertion that matters is that it is not
    unbounded.
    """
    start = time.perf_counter()
    await _collect(orchestrator, TEXT)
    elapsed = time.perf_counter() - start

    budget = CONFIRMATION_LEAD_IN_SECONDS + CONFIRMATION_PACING_SECONDS
    assert elapsed <= budget + 0.5
    assert elapsed >= budget * 0.5, (
        "it should actually pace, not dump everything at once"
    )


@pytest.mark.asyncio
async def test_a_short_confirmation_is_not_paced(orchestrator: Any) -> None:
    """Under the floor, typing reads as a stall rather than as typing.

    The LEAD-IN still applies — that beat is about the text reading as having
    arrived, which is true whether or not it then types — so the bound here is
    the lead-in, not zero. What must not happen is the pacing budget being spent.
    """
    short = " ".join(["word"] * (CONFIRMATION_MIN_PACED_WORDS - 1))
    start = time.perf_counter()
    frames = await _collect(orchestrator, short)
    elapsed = time.perf_counter() - start

    assert frames == [short], "one frame, whole text"
    assert elapsed < CONFIRMATION_LEAD_IN_SECONDS + 0.3
    assert elapsed < CONFIRMATION_PACING_SECONDS, "the pacing budget must not be spent"


@pytest.mark.asyncio
async def test_an_executor_that_gets_there_first_flushes_the_rest(
    orchestrator: Any, paced: None,
) -> None:
    """The backstop that makes a tight budget safe.

    Being slightly too slow must cost a confirmation that finishes instantly,
    never a delayed answer. With the open already complete, the remaining words
    land in one frame instead of being raced out one at a time.
    """
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    done.set_result(None)

    start = time.perf_counter()
    frames = await _collect(orchestrator, TEXT, open_task=done)
    elapsed = time.perf_counter() - start

    assert "".join(frames) == TEXT, "the whole text still reaches the reader"
    assert len(frames) < len(TEXT.split()), "it must not keep typing word by word"
    assert elapsed < 0.2, "an executor that is ready must not be waited on"


def test_pacing_is_off_in_the_shipped_config() -> None:
    """The default itself, asserted — not a monkeypatched stand-in for it.

    OFF, and deliberately: the desktop panel vendors the CLI rather than tracking
    this repo's submodule, so fragment accumulation has to be pinned there before
    typing renders. Against an older build a typed run shows its first word and
    nothing else, which is the bug this constant exists to avoid shipping.

    Pinned directly because every reassembly assertion in this file is satisfied
    by one whole frame — so nothing else here would notice the constant moving in
    either direction.
    """
    from daintree_assistant_server.config import CONFIRMATION_PACING_ENABLED

    assert CONFIRMATION_PACING_ENABLED is False


@pytest.mark.asyncio
async def test_the_default_path_delivers_one_whole_frame(orchestrator: Any) -> None:
    """No monkeypatching: this is what a real turn produces today.

    One frame carrying the whole sentence, which every CLI version renders
    correctly — the compatibility shape, and the reason the constant is off.
    """
    frames = await _collect(orchestrator, TEXT)

    assert frames == [TEXT], "one frame, whole text — never a prefix"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_readiness_cancels_the_timer_rather_than_waiting_it_out(
    orchestrator: Any, paced: None,
) -> None:
    """The flush must RACE the sleep, not be checked before it.

    Checking `open_task.done()` and then sleeping unconditionally means an
    executor that becomes ready mid-interval waits out the rest of it — the whole
    0.2s lead-in, or a third of a second at the four-word minimum. That is
    precisely the delay the concurrent design exists to avoid, and it is
    invisible to every other test here because the text still arrives complete.

    Here the executor becomes ready a little way into the lead-in. The
    confirmation must finish almost immediately after, not at the end of the
    budget.
    """
    loop = asyncio.get_running_loop()
    open_task: asyncio.Future[None] = loop.create_future()
    loop.call_later(0.05, lambda: open_task.set_result(None))

    start = time.perf_counter()
    frames = await _collect(orchestrator, TEXT, open_task=open_task)
    elapsed = time.perf_counter() - start

    assert "".join(frames) == TEXT, "the whole text still reaches the reader"
    # Well INSIDE the lead-in: racing finishes just after 0.05s, while a
    # pre-check plus an unconditional sleep finishes at the full 0.2s.
    assert elapsed < CONFIRMATION_LEAD_IN_SECONDS * 0.6, (
        f"took {elapsed:.3f}s — readiness at 0.05s should abandon the timer, not "
        f"wait out the {CONFIRMATION_LEAD_IN_SECONDS}s lead-in"
    )
