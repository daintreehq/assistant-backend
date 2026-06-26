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
    finish_reason: str = "stop",
    model: str = "deepseek-v4-flash",
    usage: ProviderUsage | None = None,
) -> ProviderChatResult:
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return ProviderChatResult(
        id="chatcmpl-fake",
        created=1700000000,
        model=model,
        choices=[{"index": 0, "message": message, "finish_reason": finish_reason}],
        usage=usage or ProviderUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason=finish_reason,
        raw={},
    )


def make_text_chunks(
    text: str, *, model: str = "deepseek-v4-flash", with_usage: bool = True
) -> list[dict[str, Any]]:
    """Split text into role + content chunks plus a final stop (and usage) chunk."""
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
    if with_usage:
        final["usage"] = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    chunks.append(final)
    return chunks


class FakeStream(ProviderStream):
    def __init__(
        self,
        chunks: list[dict[str, Any]],
        *,
        usage: ProviderUsage | None = None,
        error_at: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self.usage = usage
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
        self.stream_requests: list[ProviderChatRequest] = []
        self.closed = False

        # Configurable behavior.
        self.complete_result: ProviderChatResult | Callable[[ProviderChatRequest], ProviderChatResult] | None = None
        self.complete_error: Exception | None = None
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

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        self.requests.append(request)
        if self.complete_error is not None:
            raise self.complete_error
        if self.complete_result is None:
            return make_result(content="ok")
        if callable(self.complete_result):
            return self.complete_result(request)
        return self.complete_result

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
