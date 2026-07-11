"""The DeepSeek provider adapter (OpenAI-compatible wire format).

Translates the normalized :class:`ProviderChatRequest` to DeepSeek's
``/chat/completions`` API and maps upstream failures to Daintree errors. A
single process-wide ``httpx.AsyncClient`` is injected; a client is never built
per request.

DeepSeek speaks the OpenAI wire shape, so most of this mirrors any
OpenAI-compatible adapter. The one provider-specific detail is reasoning control:
DeepSeek uses a ``thinking`` object (``{"type": "disabled"}`` /
``{"type": "enabled"}``) plus an optional ``reasoning_effort`` tier, NOT a flat
``reasoning_effort`` field. Thinking must be DISABLED for ANY forced tool_choice
("Thinking mode does not support this tool_choice") — json_object output and
non-forced (auto/none) tool calling are both fine with thinking on — and DeepSeek
defaults thinking ON when the field is absent, so the directive is sent explicitly
on every call. ``send_thinking=False`` drops it entirely for a provider/model that
does not understand the field. See ``_build_body``.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import time
from typing import Any

import httpx

from ..errors import DaintreeError, upstream_error, upstream_rate_limited, upstream_timeout
from ..observability import get_logger
from .base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from .sse import ChunkShapeError, iter_sse_data, parse_chunk

logger = get_logger("daintree.deepseek")

_CHAT_PATH = "/chat/completions"

# Cap on how much of an upstream error body we log / surface — enough to carry the
# provider's reason ("temperature must be between 0.0 and 2.0", a schema complaint)
# without dumping an unbounded payload into logs or the client error message.
_ERROR_DETAIL_CAP = 400


def _extract_upstream_error(body: bytes | str | None) -> str | None:
    """Pull a short human-readable reason out of an upstream error body.

    DeepSeek returns ``{"error": {"message": "...", ...}}`` on a 4xx/5xx. That
    message is the single most useful diagnostic for a malformed request, so we
    surface it rather than discard it. Best-effort: any parse failure falls back to
    the raw (capped) text, and an empty body yields ``None``.
    """
    if not body:
        return None
    raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:_ERROR_DETAIL_CAP]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err.get("type")
        return str(msg)[:_ERROR_DETAIL_CAP] if msg else raw[:_ERROR_DETAIL_CAP]
    if isinstance(err, str) and err:
        return err[:_ERROR_DETAIL_CAP]
    return raw[:_ERROR_DETAIL_CAP]


def _looks_rate_limited(err: dict[str, Any]) -> bool:
    blob = " ".join(str(err.get(k, "")) for k in ("code", "type", "message")).lower()
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
            # A dropped connection is transient. Retrying is only acted on during
            # open/prefetch, before any model delta is exposed; the mid-stream
            # consumer ignores the flag and surfaces a terminal error instead.
            raise upstream_error(
                "The upstream model stream was interrupted.", retryable=True
            ) from exc

        try:
            chunk = parse_chunk(data)
        except json.JSONDecodeError as exc:
            self._done = True
            raise upstream_error("Malformed SSE payload from upstream.") from exc
        except ChunkShapeError as exc:
            # Valid JSON, unusable structure (list root, non-dict choice/delta, …):
            # an upstream protocol violation, surfaced here as a DaintreeError so
            # the consumer never crashes indexing into the chunk.
            self._done = True
            raise upstream_error(f"Malformed stream chunk from upstream ({exc}).") from exc

        if chunk is None:  # [DONE]
            self._saw_done = True
            self._done = True
            return None
        # parse_chunk guarantees the shape: chunk is a dict, and error/usage, when
        # present, are dicts.
        if chunk.get("error"):
            self._done = True
            raise map_stream_error(chunk["error"])
        if chunk.get("usage"):
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
        dump_dir: str | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
        self._send_prompt_cache_key = send_prompt_cache_key
        self._send_thinking = send_thinking
        # Dev-only diagnostic (Settings.dump_upstream_dir, gated out of production
        # by readiness): when set, every upstream request body is written there as
        # numbered pretty-printed JSON. This is THE tool for prompt-cache forensics
        # — diff two dumps to find the exact byte where two "identical" requests
        # diverge (a cache miss is always a prefix divergence). The dump contains
        # full prompts.
        self._dump_dir = dump_dir or None
        self._dump_seq = itertools.count(1)

    def _maybe_dump_body(self, body: dict[str, Any]) -> None:
        if not self._dump_dir:
            return
        try:
            os.makedirs(self._dump_dir, exist_ok=True)
            n = next(self._dump_seq)
            path = os.path.join(self._dump_dir, f"{time.strftime('%H%M%S')}-{n:04d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=1, ensure_ascii=False, sort_keys=False)
        except OSError:  # pragma: no cover - diagnostic only, never break a request
            pass

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
        if self._send_thinking:
            if request.thinking_disabled:
                # An explicit disable is mandatory: DeepSeek defaults thinking ON when
                # the field is absent, which would 400 any forced tool_choice call.
                body["thinking"] = {"type": "disabled"}
            else:
                # Thinking mode: the model emits a chain-of-thought (returned via
                # reasoning_content) before the answer. reasoning_effort tunes the
                # depth; DeepSeek remaps low/medium -> high and xhigh -> max. When it
                # is unset we omit the tier and let the provider default apply.
                body["thinking"] = {"type": "enabled"}
                if request.reasoning_effort:
                    body["reasoning_effort"] = request.reasoning_effort
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
        self._maybe_dump_body(body)
        return body

    def _timeout(self, request: ProviderChatRequest) -> httpx.Timeout:
        """Per-request timeout preserving the client's structured components.

        The per-call budget applies to READ only (for a streaming response httpx
        applies it per chunk gap, and a thinking model can legitimately sit far
        longer between chunks than any sane connect/write/pool bound). A bare
        float here would override ALL components, collapsing connect/write/pool
        to the huge main budget — exactly what the structured client timeout in
        ``main._build_provider`` exists to prevent.
        """
        seconds = request.timeout if request.timeout is not None else self._default_timeout
        base = self._client.timeout
        return httpx.Timeout(connect=base.connect, read=seconds, write=base.write, pool=base.pool)

    def _map_http_error(
        self, status: int, headers: httpx.Headers, detail: str | None = None
    ) -> DaintreeError:
        if status == 429:
            return upstream_rate_limited(retry_after=headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout()
        if status >= 500:
            # Transient upstream fault — retry while no model output is exposed.
            return upstream_error(retryable=True)
        # A 4xx means the upstream rejected OUR request as malformed: permanent
        # (never retried) and almost always a server-side bug in what we sent. Carry
        # the upstream's own reason through so the failure is diagnosable instead of
        # an opaque "status 400" — the CLI and logs then name the offending field.
        suffix = f" {detail}" if detail else ""
        return upstream_error(f"Upstream rejected the request (HTTP {status}).{suffix}")

    def _build_result(self, data: dict[str, Any]) -> ProviderChatResult:
        choices = data.get("choices") or []
        # Structural guard mirroring the stream path: consumers index straight
        # into choices[0]["message"], so a non-list / non-dict shape must fail as
        # an upstream error here, not as an AttributeError later.
        if not isinstance(choices, list) or not all(isinstance(c, dict) for c in choices):
            raise upstream_error("Upstream returned a malformed completion response.")
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
            detail = _extract_upstream_error(resp.content)
            logger.warning(
                "upstream_http_error",
                phase="complete",
                model=request.model,
                status=resp.status_code,
                detail=detail,
            )
            raise self._map_http_error(resp.status_code, resp.headers, detail)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise upstream_error("Upstream returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise upstream_error("Upstream returned a malformed completion response.")
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
            # A forced tool_choice is incompatible with thinking on DeepSeek
            # ("Thinking mode does not support this tool_choice") — pin it
            # non-thinking regardless of what the caller requested, so a forced call
            # can never 400 on a thinking directive.
            thinking_disabled=True,
            reasoning_effort=None,
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
            detail: str | None = None
            try:
                # Drain the error body best-effort: a transport failure mid-read
                # must not mask the mapped status error (e.g. a retryable 5xx). When
                # it IS readable, keep its reason — it's the whole diagnostic.
                with contextlib.suppress(httpx.HTTPError):
                    detail = _extract_upstream_error(await response.aread())
            finally:
                await ctx.__aexit__(None, None, None)
            logger.warning(
                "upstream_http_error",
                phase="stream_open",
                model=request.model,
                status=response.status_code,
                detail=detail,
            )
            raise self._map_http_error(response.status_code, response.headers, detail)
        return DeepSeekStream(ctx, response, request.model)

    async def aclose(self) -> None:
        await self._client.aclose()
