"""OpenAI's Responses API — the serving host for gpt-6-luna.

Everything above the provider boundary speaks the chat-completions shape: a
``messages`` array in, ``choices[0].message`` / ``choices[0].delta`` chunks out.
This adapter keeps that true and translates at the wire, in both directions.
Every claim below was measured against the live API before this file was
written.

**Why the Responses API and not Chat Completions, which OpenAI also serves.**
On ``/v1/chat/completions`` this model refuses function tools combined with any
``reasoning_effort`` other than ``none`` ("Function tools with reasoning_effort
are not supported for gpt-6-luna in /v1/chat/completions"). Every assistant turn
carries tools, so on that endpoint the configured reasoning posture
(``config.EXECUTOR_REASONING_EFFORT``) would be unreachable on exactly the turns
it exists for. ``/v1/responses`` accepts the combination.

**The host is STRICT.** Unknown fields answer ``400``, and this model also
refuses ``temperature`` (other than the default 1) and ``stop``. So the body is
BUILT from the chat body rather than patched: only the fields listed in
``_build_body`` are ever sent, and anything the parent writes that this host
does not speak simply never reaches it.

**Function tools are sent ``strict: false`` explicitly.** Omitted, the Responses
API defaults a function tool to strict mode (measured: the echoed tool came back
``"strict": true`` with ``additionalProperties: false`` injected), and strict
mode requires every property to be listed in ``required`` — which the CLI's
schemas, with their optional fields, are not.

**Reasoning is a single fixed posture.** ``config.EXECUTOR_REASONING_EFFORT`` is
sent on every call, including the ``thinking_disabled`` paths: one posture is the
product decision, and a caller-reachable second one would be a mode by another
name. Reasoning tokens share ``max_output_tokens`` with visible output.

**Reasoning is not replayed across turns.** Calls are ``store: false`` (nothing
is retained at OpenAI), and the CLI round-trips only visible content and tool
calls, so each round starts from the transcript rather than the prior round's
reasoning items. Carrying ``reasoning.encrypted_content`` through the wire
contract would restore that and is a deliberate follow-up (``TODO.md``).

**It reports no cost** — ``usage`` carries token counts (input, cached input,
output, reasoning) but no price, so ``ProviderUsage.cost`` stays ``None``.

**Prompt caching is automatic on the literal prefix**, and this host takes an
affinity hint: ``prompt_cache_key``. ``cache_session_id`` already names calls
sharing a growing prefix, so it is sent under that name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from ..errors import (
    upstream_protocol_error,
    upstream_timeout,
    upstream_unavailable,
)
from .base import ProviderChatRequest, ProviderChatResult, ProviderStream, ProviderUsage
from .openrouter import OpenRouterProvider, OpenRouterStream, map_stream_error
from .sse import SseProtocolError

DEFAULT_BASE_URL = "https://api.openai.com/v1"

_RESPONSES_PATH = "/responses"

#: OpenAI has no ``/key`` route. ``/models`` answers 200 for a usable key and
#: 401 for a dead one, which is the only distinction ``verify_key`` draws.
_KEY_PROBE_PATH = "/models"

#: The Responses API rejects ``max_output_tokens`` below this.
_MIN_OUTPUT_TOKENS = 16

#: The Responses API rejects a longer ``prompt_cache_key`` (measured: 400
#: "Expected a string with maximum length 64").
_MAX_CACHE_KEY_CHARS = 64


def _cache_key(value: str) -> str:
    """``cache_session_id`` as a key this host accepts.

    The main path's key is longer than the limit, so an over-long one is sent as
    its SHA-256 hex digest — exactly 64 characters, and stable for the same
    input, which is the only property affinity needs.
    """
    if len(value) <= _MAX_CACHE_KEY_CHARS:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Outbound: chat messages → Responses input items
# ---------------------------------------------------------------------------


def _text_of(content: Any) -> str:
    """Flatten a content value to text — a string, or the text parts of a list."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _input_content(content: Any) -> Any:
    """A user/system content value as Responses input content.

    A string passes through. A list of chat parts becomes ``input_text`` /
    ``input_image`` parts; the image URL (a validated data URL, see
    ``services/validation.py``) is carried unchanged.
    """
    if not isinstance(content, list):
        return _text_of(content)
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str):
                parts.append({"type": "input_image", "image_url": url})
    return parts


def _input_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate a chat ``messages`` array into Responses ``input`` items.

    An assistant tool-call turn becomes its (optional) text message followed by
    one ``function_call`` item per call, and a ``tool`` message becomes a
    ``function_call_output`` keyed by the same ``call_id`` — the pairing the
    transcript validator already guarantees. Message ``name`` fields and any
    replayed chain of thought are not part of this wire and are dropped.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    # A string, or input parts when the result carries images.
                    "output": _input_content(message.get("content")),
                }
            )
            continue
        if role == "assistant":
            text = _text_of(message.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue
        items.append({"role": role, "content": _input_content(message.get("content"))})
    return items


def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat ``{"type": "function", "function": {...}}`` → flat Responses tools."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        entry: dict[str, Any] = {"type": "function", "name": fn.get("name", "")}
        if fn.get("description"):
            entry["description"] = fn["description"]
        entry["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        # See the module docstring: strict is the default here and the CLI's
        # schemas are not strict-compatible.
        entry["strict"] = False
        out.append(entry)
    return out


def _tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"type": "function", "name": name}
    return choice


def _text_format(response_format: Any) -> dict[str, Any] | None:
    """``json_object`` is the only structured format this server ever requests
    (utility tasks; ``generation.response_format``); ``text`` is the default."""
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        return {"type": "json_object"}
    return None


# ---------------------------------------------------------------------------
# Inbound: Responses output → chat-completions shape
# ---------------------------------------------------------------------------


def _chat_usage(usage: Any) -> dict[str, Any] | None:
    """Responses ``usage`` in the chat spelling ``ProviderUsage.from_wire`` reads."""
    if not isinstance(usage, dict):
        return None
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": input_details.get("cached_tokens", 0)},
        "completion_tokens_details": {
            "reasoning_tokens": output_details.get("reasoning_tokens", 0)
        },
    }


def _finish_reason(response: dict[str, Any], *, saw_tool_call: bool) -> str:
    if response.get("status") == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        return "length" if reason == "max_output_tokens" else "content_filter"
    return "tool_calls" if saw_tool_call else "stop"


def _chat_completion(response: dict[str, Any]) -> dict[str, Any]:
    """A complete Responses object rebuilt as a chat completion."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text_parts.append(part.get("text", ""))
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", ""),
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    completion: dict[str, Any] = {
        "id": response.get("id", ""),
        "created": response.get("created_at", 0),
        "model": response.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(response, saw_tool_call=bool(tool_calls)),
            }
        ],
    }
    usage = _chat_usage(response.get("usage"))
    if usage is not None:
        completion["usage"] = usage
    return completion


class OpenAIResponsesStream(OpenRouterStream):
    """A Responses SSE stream, yielding chat-completion chunks.

    Lifecycle events that carry no model output (``response.created``,
    ``response.in_progress``, part/item bookkeeping) are consumed silently, so
    the first chunk this yields is the first real output. That matters beyond
    tidiness: the failover window closes on the first event
    (``providers/failover.py``), and "the host acknowledged the request" is not
    "the host produced output".
    """

    def __init__(self, stream_ctx: Any, response: httpx.Response, model: str) -> None:
        super().__init__(stream_ctx, response, model)
        #: Responses ``item_id`` → the chat ``tool_calls[].index`` it streams as.
        self._tool_index: dict[str, int] = {}
        self._chunk_id = ""

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self._chunk_id,
            "object": "chat.completion.chunk",
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    async def _read_one(self) -> dict[str, Any] | None:
        while True:
            if self._done:
                return None
            try:
                data = await self._events.__anext__()
            except StopAsyncIteration as exc:
                self._done = True
                raise upstream_protocol_error("Upstream stream ended before completion.") from exc
            except httpx.TimeoutException as exc:
                self._done = True
                raise upstream_timeout() from exc
            except httpx.TransportError as exc:
                self._done = True
                raise upstream_unavailable("The upstream model stream was interrupted.") from exc
            except SseProtocolError as exc:
                self._done = True
                raise upstream_protocol_error("Malformed SSE stream from upstream.") from exc

            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                self._done = True
                raise upstream_protocol_error("Malformed SSE payload from upstream.") from exc
            if not isinstance(event, dict):
                self._done = True
                raise upstream_protocol_error("Malformed stream event from upstream.")

            chunk = self._translate(event)
            if chunk is not None:
                return chunk

    def _translate(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "response.created":
            response = event.get("response") or {}
            self._chunk_id = str(response.get("id", ""))
            return None
        if kind == "response.output_text.delta":
            text = event.get("delta")
            if isinstance(text, str) and text:
                return self._chunk({"content": text})
            return None
        if kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") != "function_call":
                return None
            index = len(self._tool_index)
            self._tool_index[str(item.get("id", ""))] = index
            return self._chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": item.get("call_id", ""),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "") or "",
                            },
                        }
                    ]
                }
            )
        if kind == "response.function_call_arguments.delta":
            slot = self._tool_index.get(str(event.get("item_id", "")))
            fragment = event.get("delta")
            if slot is None or not isinstance(fragment, str) or not fragment:
                return None
            return self._chunk(
                {"tool_calls": [{"index": slot, "function": {"arguments": fragment}}]}
            )
        if kind in ("response.completed", "response.incomplete"):
            response = event.get("response") or {}
            self._done = True
            self._saw_done = True
            usage = _chat_usage(response.get("usage"))
            chunk = self._chunk(
                {}, _finish_reason(response, saw_tool_call=bool(self._tool_index))
            )
            if usage is not None:
                chunk["usage"] = usage
                self.usage = ProviderUsage.from_wire(usage)
            return chunk
        if kind == "response.failed":
            self._done = True
            error = (event.get("response") or {}).get("error") or {}
            raise map_stream_error(error if isinstance(error, dict) else {})
        if kind == "error":
            self._done = True
            raise map_stream_error(event)
        return None


class OpenAIProvider(OpenRouterProvider):
    """The chat-shaped adapter, speaking OpenAI's Responses API underneath.

    Error mapping, retries, redaction, the key probe and the dev-only request
    dump are inherited. The URL, the body, the result and the stream are
    translated.
    """

    default_base_url: ClassVar[str] = DEFAULT_BASE_URL
    key_probe_path: ClassVar[str] = _KEY_PROBE_PATH
    #: OpenAI runs no public app ranking, so the parent's attribution headers
    #: would be bytes on every request buying nothing.
    attribution_headers: ClassVar[Mapping[str, str]] = {}

    def __init__(self, *args: Any, reasoning_effort: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Injected rather than read from `config`, so the product decision stays
        # beside the model id and this module keeps its one job. Required: a
        # silent default here is how a benchmark ends up pricing a body
        # production does not send.
        self._reasoning_effort = reasoning_effort

    @property
    def _url(self) -> str:
        return f"{self._base_url}{_RESPONSES_PATH}"

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "input": _input_items(request.messages),
            "reasoning": {"effort": self._reasoning_effort},
            # Nothing is retained at OpenAI; every round is sent whole.
            "store": False,
        }
        if request.tools:
            body["tools"] = _tools(request.tools)
        if request.tool_choice is not None:
            body["tool_choice"] = _tool_choice(request.tool_choice)
        if request.max_tokens is not None:
            body["max_output_tokens"] = max(request.max_tokens, _MIN_OUTPUT_TOKENS)
        text_format = _text_format(request.response_format)
        if text_format is not None:
            body["text"] = {"format": text_format}
        if request.cache_session_id:
            body["prompt_cache_key"] = _cache_key(request.cache_session_id)
        if stream:
            body["stream"] = True
        return body

    def _build_result(self, data: dict[str, Any]) -> ProviderChatResult:
        return super()._build_result(_chat_completion(data))

    def _make_stream(self, ctx: Any, response: httpx.Response, model: str) -> ProviderStream:
        return OpenAIResponsesStream(ctx, response, model)

    def _decode_key_record(self, payload: Any) -> dict[str, Any]:
        """``/models`` answers ``{"object": "list", "data": [...]}`` — a LIST,
        which the parent's OpenRouter-shaped decoder would reject as a protocol
        error on every valid key."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise upstream_protocol_error("The provider returned no model listing.")
        return {
            "models": [
                m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        }
