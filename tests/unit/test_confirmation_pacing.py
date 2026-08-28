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

#: Pacing is OFF in the shipped config until the CLI stops discarding every
#: frame after the first (`config.CONFIRMATION_PACING_ENABLED`). These tests
#: force it on: the mechanism is live and measured, and letting it rot untested
#: while it waits on another repo is how it would stop working before anyone
#: could switch it back on. `test_the_shipped_config_delivers_the_whole_text`
#: pins the DEFAULT.


@pytest.fixture(autouse=True)
def _paced(monkeypatch: pytest.MonkeyPatch) -> None:
    from daintree_assistant_server.services import chat_orchestrator as orch

    monkeypatch.setattr(orch, "CONFIRMATION_PACING_ENABLED", True)

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
async def test_frames_are_cumulative_because_the_client_replaces(
    orchestrator: Any,
) -> None:
    """The CLI sets `preamble = pre.Content` — REPLACE, never append.

    Word-DELTAS into a replacing consumer would leave the reader looking at the
    last word on its own, which is the one shape that looks like a bug rather
    than like typing. Every frame therefore carries the whole text so far.
    """
    frames = await _collect(orchestrator, TEXT)

    assert len(frames) == len(TEXT.split()), "one frame per word"
    assert frames[-1] == TEXT
    assert frames[0] == TEXT.split()[0]
    for earlier, later in zip(frames, frames[1:], strict=False):
        assert later.startswith(earlier), "frames must only grow"
        assert TEXT.startswith(later)


@pytest.mark.asyncio
async def test_typing_finishes_inside_the_budget(orchestrator: Any) -> None:
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
    orchestrator: Any,
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

    assert frames[-1] == TEXT, "the whole text still reaches the reader"
    assert len(frames) < len(TEXT.split()), "it must not keep typing word by word"
    assert elapsed < 0.2, "an executor that is ready must not be waited on"


@pytest.mark.asyncio
async def test_the_shipped_config_delivers_the_whole_text(
    orchestrator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a real client gets today: one frame, the whole sentence.

    The paced tests above force the flag on. This one pins the shipped default,
    because it is the only behaviour a user currently sees — and because the bug
    it exists to prevent is not a crash but a confirmation rendered as its first
    word, which every other test here would pass straight through.
    """
    from daintree_assistant_server.services import chat_orchestrator as orch

    monkeypatch.setattr(orch, "CONFIRMATION_PACING_ENABLED", False)
    frames = await _collect(orchestrator, TEXT)

    assert frames == [TEXT], "one frame, whole text — never a prefix"
