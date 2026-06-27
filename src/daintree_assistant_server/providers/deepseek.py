"""The DeepSeek provider adapter (OpenAI-compatible wire format).

Translates the normalized :class:`ProviderChatRequest` to DeepSeek's
``/chat/completions`` API and maps upstream failures to Daintree errors. A
single process-wide ``httpx.AsyncClient`` is injected; a client is never built
per request.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx

from ..errors import DaintreeError, upstream_error, upstream_rate_limited, upstream_timeout
from .base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from .sse import iter_sse_data, parse_chunk

_CHAT_PATH = "/chat/completions"


def _looks_rate_limited(err: dict[str, Any]) -> bool:
    blob = " ".join(
        str(err.get(k, "")) for k in ("code", "type", "message")
    ).lower()
    return any(
        token in blob
        for token in ("rate limit", "rate_limit", "429", "quota", "overloaded", "too many requests")
    )


def map_stream_error(err: dict[str, Any]) -> DaintreeError:
    if _looks_rate_limited(err):
        return upstream_rate_limited()
    return upstream_error("The upstream model stream reported an error.")


class DeepSeekStream(ProviderStream):
    """An open DeepSeek SSE stream yielding OpenAI-shaped chunk dicts."""

    def __init__(self, stream_ctx: Any, response: httpx.Response, model: str) -> None:
        self._ctx = stream_ctx
        self._response = response
        self._model = model
        self._events = iter_sse_data(response.aiter_lines())
        self._closed = False
        self._done = False
        self._saw_done = False
        self.usage: ProviderUsage | None = None

    async def _read_one(self) -> dict[str, Any] | None:
        if self._done:
            return None
        try:
            data = await self._events.__anext__()
        except StopAsyncIteration as exc:
            self._done = True
            # A clean end WITHOUT the upstream [DONE] marker means the stream was
            # truncated; never treat that as a successful finish.
            if not self._saw_done:
                raise upstream_error("Upstream stream ended before completion.") from exc
            return None
        except httpx.TimeoutException as exc:
            self._done = True
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            self._done = True
            # A dropped connection is transient. Retrying is only acted on before
            # any public bytes are emitted (a failed prefetch); the mid-stream
            # consumer ignores the flag and surfaces a terminal error instead.
            raise upstream_error(
                "The upstream model stream was interrupted.", retryable=True
            ) from exc

        try:
            chunk = parse_chunk(data)
        except json.JSONDecodeError as exc:
            self._done = True
            raise upstream_error("Malformed SSE payload from upstream.") from exc

        if chunk is None:  # [DONE]
            self._saw_done = True
            self._done = True
            return None
        if isinstance(chunk, dict) and chunk.get("error"):
            self._done = True
            raise map_stream_error(chunk["error"])
        if isinstance(chunk, dict) and chunk.get("usage"):
            self.usage = ProviderUsage.from_wire(chunk["usage"])
        return chunk

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        return await self._read_one()

    def __aiter__(self) -> DeepSeekStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        chunk = await self._read_one()
        if chunk is None:
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
            await self._ctx.__aexit__(None, None, None)


class DeepSeekProvider:
    """A :class:`ChatProvider` backed by the DeepSeek hosted API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        default_timeout: float = 60.0,
        send_prompt_cache_key: bool = True,
        send_thinking: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
        self._send_prompt_cache_key = send_prompt_cache_key
        self._send_thinking = send_thinking

    # ---- helpers --------------------------------------------------------

    @property
    def _url(self) -> str:
        return f"{self._base_url}{_CHAT_PATH}"

    def _headers(self, *, stream: bool) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.tools:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if request.stop:
            body["stop"] = request.stop
        if self._send_thinking and request.thinking_disabled:
            body["thinking"] = {"type": "disabled"}
        # prompt cache key is only valid for plain chat/stream (not json mode).
        if (
            self._send_prompt_cache_key
            and request.prompt_cache_key
            and request.response_format is None
        ):
            body["prompt_cache_key"] = request.prompt_cache_key
        if stream:
            body["stream"] = True
            if request.include_usage:
                body["stream_options"] = {"include_usage": True}
        return body

    def _timeout(self, request: ProviderChatRequest) -> float:
        return request.timeout if request.timeout is not None else self._default_timeout

    def _map_http_error(self, status: int, headers: httpx.Headers) -> DaintreeError:
        if status == 429:
            return upstream_rate_limited(retry_after=headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout()
        if status >= 500:
            return upstream_error(retryable=True)
        return upstream_error(f"Upstream returned status {status}.")

    def _build_result(self, data: dict[str, Any]) -> ProviderChatResult:
        choices = data.get("choices") or []
        finish = choices[0].get("finish_reason") if choices else None
        return ProviderChatResult(
            id=data.get("id", ""),
            created=data.get("created", 0),
            model=data.get("model", ""),
            choices=choices,
            usage=ProviderUsage.from_wire(data.get("usage")),
            finish_reason=finish,
            raw=data,
        )

    # ---- ChatProvider ---------------------------------------------------

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        body = self._build_body(request, stream=False)
        try:
            resp = await self._client.post(
                self._url,
                json=body,
                headers=self._headers(stream=False),
                timeout=self._timeout(request),
            )
        except httpx.TimeoutException as exc:
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            raise upstream_error("Could not reach the upstream provider.", retryable=True) from exc
        if resp.status_code >= 400:
            raise self._map_http_error(resp.status_code, resp.headers)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise upstream_error("Upstream returned a non-JSON response.") from exc
        return self._build_result(data)

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        sub = ProviderChatRequest(
            model=request.model,
            messages=request.messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": function_name, "parameters": function_schema},
                }
            ],
            tool_choice={"type": "function", "function": {"name": function_name}},
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
            thinking_disabled=request.thinking_disabled,
            timeout=request.timeout,
        )
        result = await self.complete(sub)
        calls = result.tool_calls
        if not calls:
            # No tool call: surface empty args so the caller's validation path
            # can attempt repair rather than crashing.
            return ProviderToolCallResult(
                name=function_name, arguments_json="", usage=result.usage, raw=result.raw
            )
        fn = calls[0].get("function", {}) or {}
        return ProviderToolCallResult(
            name=fn.get("name", ""),
            arguments_json=fn.get("arguments", "") or "",
            usage=result.usage,
            raw=result.raw,
        )

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        body = self._build_body(request, stream=True)
        ctx = self._client.stream(
            "POST",
            self._url,
            json=body,
            headers=self._headers(stream=True),
            timeout=self._timeout(request),
        )
        try:
            response = await ctx.__aenter__()
        except httpx.TimeoutException as exc:
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            raise upstream_error("Could not reach the upstream provider.", retryable=True) from exc
        if response.status_code >= 400:
            try:
                # Drain the error body best-effort: a transport failure mid-read
                # must not mask the mapped status error (e.g. a retryable 5xx).
                with contextlib.suppress(httpx.HTTPError):
                    await response.aread()
            finally:
                await ctx.__aexit__(None, None, None)
            raise self._map_http_error(response.status_code, response.headers)
        return DeepSeekStream(ctx, response, request.model)

    async def aclose(self) -> None:
        await self._client.aclose()
