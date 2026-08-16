"""In-memory fakes used across the test suite.

``FakeProvider`` implements the :class:`ChatProvider` protocol without any
network access. Behavior is configured by assigning canned results or callables;
every request is recorded for assertions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)


def make_result(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    finish_reason: str = "stop",
    model: str = "deepseek-v4-flash",
    usage: ProviderUsage | None = None,
    endpoint: str | None = None,
) -> ProviderChatResult:
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ProviderChatResult(
        id="chatcmpl-fake",
        created=1700000000,
        model=model,
        choices=[{"index": 0, "message": message, "finish_reason": finish_reason}],
        usage=usage or ProviderUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason=finish_reason,
        endpoint=endpoint,
        raw={},
    )


def make_text_chunks(
    text: str,
    *,
    model: str = "deepseek-v4-flash",
    with_usage: bool = True,
    cost: float | None = None,
) -> list[dict[str, Any]]:
    """Split text into role + content chunks plus a final stop (and usage) chunk.

    ``cost`` mirrors the real usage object's field. Left off by default because
    most tests do not care, but a stream that reports tokens and NO cost is a
    real shape (and the one that makes a summed total a floor), so it has to be
    expressible.
    """
    chunks: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    for piece in text.split(" "):
        chunks.append(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": piece + " "}, "finish_reason": None}
                ],
            }
        )
    final: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    chunks.append(final)
    if with_usage:
        # A REAL DeepSeek stream reports usage in its own trailing chunk with an
        # EMPTY choices list, not bolted onto the finish_reason chunk. Bolting it on
        # made the fake too clean: the orchestrator's `if not choices` guards in
        # _chunk_finish_reason/_chunk_to_delta never executed under any test, so a
        # regression there would have been invisible. Emitting the real shape means
        # every existing streaming test covers both guards.
        usage: dict[str, Any] = {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        }
        if cost is not None:
            usage["cost"] = cost
        chunks.append(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 1700000000,
                "model": model,
                "choices": [],
                "usage": usage,
            }
        )
    return chunks


class FakeStream(ProviderStream):
    def __init__(
        self,
        chunks: list[dict[str, Any]],
        *,
        usage: ProviderUsage | None = None,
        error_at: int | None = None,
        error: Exception | None = None,
        endpoint: str | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self.usage = usage
        # The real stream reads this off the first chunk naming it; a fake sets it
        # up front. Consumers only ever read it, so the distinction is invisible.
        self.endpoint = endpoint
        self._error_at = error_at
        self._error = error
        self.closed = False

    async def _advance(self) -> dict[str, Any] | None:
        if self._error_at is not None and self._index == self._error_at and self._error:
            raise self._error
        if self._index >= len(self._chunks):
            return None
        chunk = self._chunks[self._index]
        self._index += 1
        if isinstance(chunk, dict) and chunk.get("usage"):
            self.usage = ProviderUsage.from_wire(chunk["usage"])
        return chunk

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        return await self._advance()

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        chunk = await self._advance()
        if chunk is None:
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class FakeProvider:
    """A configurable in-memory :class:`ChatProvider`."""

    def __init__(self) -> None:
        self.requests: list[ProviderChatRequest] = []
        self.tool_call_requests: list[ProviderChatRequest] = []
        self.selection_requests: list[ProviderChatRequest] = []
        self.stream_requests: list[ProviderChatRequest] = []
        self.closed = False

        # Configurable behavior.
        self.complete_result: (
            ProviderChatResult
            | Callable[[ProviderChatRequest], ProviderChatResult]
            | list[ProviderChatResult]
            | None
        ) = None
        self.complete_error: Exception | None = None
        # The selector now runs `complete()` with response_format json_object (not a
        # forced tool call). When a shared fake serves BOTH the selector and the main
        # call, these slots let a test drive the selector's response distinctly from the
        # main completion: a json_object request routes here whenever one is configured.
        self.selection_result: (
            ProviderChatResult
            | Callable[[ProviderChatRequest], ProviderChatResult]
            | list[ProviderChatResult]
            | None
        ) = None
        self.selection_error: Exception | None = None
        self.tool_call_result: (
            ProviderToolCallResult
            | Callable[[ProviderChatRequest], ProviderToolCallResult]
            | list[ProviderToolCallResult]
            | None
        ) = None
        self.tool_call_error: Exception | None = None
        self.stream_chunks: list[dict[str, Any]] | Callable[[ProviderChatRequest], list[dict[str, Any]]] | None = None
        self.stream_error: Exception | None = None
        self.stream_factory: Callable[[ProviderChatRequest], ProviderStream] | None = None
        self.last_stream: FakeStream | None = None
        self.verify_key_calls: list[Any] = []
        self.verify_key_error: Exception | None = None
        self.verify_key_result: dict[str, Any] = {"label": "test-key", "limit_remaining": 4.25}

    async def verify_key(self, api_key: Any = None) -> dict[str, Any]:
        """Stand-in for the provider's key-introspection probe.

        ``verify_key_error`` lets a test drive the two outcomes that matter and must
        stay distinguishable: a definite rejection (``invalid_api_key``) versus an
        unreachable provider (``upstream_error``).
        """
        self.verify_key_calls.append(api_key)
        if self.verify_key_error is not None:
            raise self.verify_key_error
        return self.verify_key_result

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        # Route a selector-shaped call (json_object output) to the selection slot when
        # one is configured, so a shared fake distinguishes it from the main call. This
        # discriminator assumes the MAIN call in the same test is not json_object — true
        # for every /respond test today (the CLI never asks the main model for JSON). If a
        # test ever drives a json_object main call while also setting selection_result,
        # give the fake an explicit selector-vs-main signal instead of widening this.
        if request.response_format == {"type": "json_object"} and (
            self.selection_result is not None or self.selection_error is not None
        ):
            self.selection_requests.append(request)
            if self.selection_error is not None:
                raise self.selection_error
            selection = self.selection_result
            if isinstance(selection, list):
                return selection.pop(0)
            if callable(selection):
                return selection(request)
            assert selection is not None
            return selection
        self.requests.append(request)
        if self.complete_error is not None:
            raise self.complete_error
        result = self.complete_result
        if result is None:
            return make_result(content="ok")
        if isinstance(result, list):
            # Queue: pop the next response (used for repair scenarios).
            return result.pop(0)
        if callable(result):
            return result(request)
        return result

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        self.tool_call_requests.append(request)
        if self.tool_call_error is not None:
            raise self.tool_call_error
        result = self.tool_call_result
        if result is None:
            return ProviderToolCallResult(
                name=function_name,
                arguments_json=json.dumps({"skill_ids": [], "confidence": 0.0, "task_type": ""}),
                usage=ProviderUsage(prompt_tokens=100, completion_tokens=10, total_tokens=110),
            )
        if isinstance(result, list):
            # Queue: pop the next response (used for repair scenarios).
            return result.pop(0)
        if callable(result):
            return result(request)
        return result

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        self.stream_requests.append(request)
        if self.stream_error is not None:
            raise self.stream_error
        if self.stream_factory is not None:
            stream = self.stream_factory(request)
            if isinstance(stream, FakeStream):
                self.last_stream = stream
            return stream
        chunks = self.stream_chunks
        if callable(chunks):
            chunks = chunks(request)
        if chunks is None:
            chunks = make_text_chunks("hello world")
        stream = FakeStream(chunks)
        self.last_stream = stream
        return stream

    async def aclose(self) -> None:
        self.closed = True


def tool_call_for(skill_ids: list[str], *, confidence: float = 0.9, task_type: str = "task") -> ProviderToolCallResult:
    return ProviderToolCallResult(
        name="select_skills",
        arguments_json=json.dumps(
            {"skill_ids": skill_ids, "confidence": confidence, "task_type": task_type}
        ),
        usage=ProviderUsage(
            prompt_tokens=3000, completion_tokens=50, total_tokens=3050, cached_tokens=2800
        ),
    )


def selection_result(
    skill_ids: list[str],
    *,
    confidence: float = 0.9,
    task_type: str = "task",
    docs_query: str | None = None,
    usage: ProviderUsage | None = None,
    endpoint: str | None = None,
) -> ProviderChatResult:
    """A selector completion result: the choice as a JSON object in ``content``.

    The selector runs response_format json_object (not a forced tool call), so its
    output is parsed from ``message.content``. Keys mirror the prompt's reason-first
    order (task_type/reason written before skill_ids — the non-thinking selector's
    inline scratchpad); parsing itself is order-agnostic.
    """
    return make_result(
        content=json.dumps(
            {
                "task_type": task_type,
                "reason": "matches the current task",
                "skill_ids": skill_ids,
                "confidence": confidence,
                # Omitted rather than null when unset, matching a selector that
                # simply did not answer the documentation question — the parser
                # treats an absent key as "no lookup".
                **({"docs_query": docs_query} if docs_query is not None else {}),
            }
        ),
        usage=usage
        or ProviderUsage(
            prompt_tokens=3000, completion_tokens=50, total_tokens=3050, cached_tokens=2800
        ),
        endpoint=endpoint,
    )
