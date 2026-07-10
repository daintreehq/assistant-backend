"""The OpenAI provider adapter (GPT-5.5), used only for skill learning.

Skill building/improving runs on OpenAI's GPT-5.5 reasoning model rather than the
DeepSeek chat provider, so this adapter translates the normalized
:class:`ProviderChatRequest` to the OpenAI Chat Completions API via the official
``openai`` SDK and maps SDK failures back to :class:`DaintreeError` so the shared
``retry_upstream`` / fail-open paths behave identically to the DeepSeek provider.

GPT-5.5 is a reasoning model, and its request shape differs from DeepSeek's in
three ways the adapter is responsible for:

* reasoning is a flat ``reasoning_effort`` string (``high``/``xhigh`` here), NOT a
  ``thinking`` object;
* the token cap is ``max_completion_tokens`` (it bounds reasoning + output), and
  the legacy ``max_tokens`` is rejected with a 400;
* any non-default ``temperature`` is rejected with a 400, so a custom temperature
  from the normalized request is dropped.

This provider is intentionally build/improve-only: skill learning calls
:meth:`complete` exclusively. Streaming and forced tool calls are not implemented
(the main ``/respond`` path never uses this provider) — they raise rather than
pretend to work.
"""

from __future__ import annotations

from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from ..errors import DaintreeError, upstream_error, upstream_rate_limited, upstream_timeout
from ..observability import get_logger
from .base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)

logger = get_logger("daintree.openai")

# Cap on how much of an upstream error reason we surface — enough to carry OpenAI's
# own complaint ("Unsupported parameter: 'max_tokens'...") without an unbounded dump.
_ERROR_DETAIL_CAP = 400


class OpenAIProvider:
    """A :class:`ChatProvider` backed by OpenAI's Chat Completions API (GPT-5.5).

    Used solely by the skill-learning builder/improver. ``max_retries=0`` on the
    client: retries are owned by the shared ``retry_upstream`` wrapper, keyed off
    the :class:`DaintreeError` ``retryable`` flag this adapter sets.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        default_timeout: float = 300.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._default_timeout = default_timeout
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            max_retries=0,
            http_client=http_client,
        )

    # ---- helpers --------------------------------------------------------

    def _timeout(self, request: ProviderChatRequest) -> float:
        return request.timeout if request.timeout is not None else self._default_timeout

    def _build_params(self, request: ProviderChatRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
        }
        if request.tools:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            params["response_format"] = request.response_format
        if request.stop:
            params["stop"] = request.stop
        # GPT-5.5 rejects the legacy `max_tokens`; the cap is `max_completion_tokens`,
        # which bounds reasoning + visible output together.
        if request.max_tokens is not None:
            params["max_completion_tokens"] = request.max_tokens
        # Reasoning control is a flat effort string (no `thinking` object). The caller
        # only sets reasoning_effort when thinking is enabled; honor that.
        reasoning = None if request.thinking_disabled else request.reasoning_effort
        if reasoning:
            params["reasoning_effort"] = reasoning
        # GPT-5.5 (the GPT-5 reasoning family) accepts ONLY the default temperature;
        # any custom value 400s. Forward temperature only when it is the default, so a
        # normalized request that carries a low temperature (as the skill learner does)
        # does not break the call — determinism rides on the prompt + JSON mode instead.
        if request.temperature is not None and request.temperature == 1.0:
            params["temperature"] = request.temperature
        return params

    def _map_status_error(self, exc: APIStatusError) -> DaintreeError:
        status = exc.status_code
        if status == 429:
            return upstream_rate_limited(retry_after=exc.response.headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout()
        if status >= 500:
            # Transient upstream fault — retry while no model output is exposed.
            return upstream_error(retryable=True)
        # A 4xx means OpenAI rejected OUR request as malformed: permanent (never
        # retried). Carry its own reason through so the failure is diagnosable.
        return upstream_error(f"OpenAI rejected the request (HTTP {status}).{self._detail(exc)}")

    @staticmethod
    def _detail(exc: APIStatusError) -> str:
        body = exc.body
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("message"):
                return f" {str(err['message'])[:_ERROR_DETAIL_CAP]}"
        message = getattr(exc, "message", "")
        return f" {message[:_ERROR_DETAIL_CAP]}" if message else ""

    @staticmethod
    def _build_result(data: dict[str, Any]) -> ProviderChatResult:
        choices = data.get("choices") or []
        finish = choices[0].get("finish_reason") if choices else None
        return ProviderChatResult(
            id=data.get("id", ""),
            created=data.get("created") or 0,
            model=data.get("model", ""),
            choices=choices,
            usage=ProviderUsage.from_wire(data.get("usage")),
            finish_reason=finish,
            raw=data,
        )

    # ---- ChatProvider ---------------------------------------------------

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        params = self._build_params(request)
        try:
            completion = await self._client.chat.completions.create(
                timeout=self._timeout(request), **params
            )
        except APITimeoutError as exc:
            raise upstream_timeout() from exc
        except APIConnectionError as exc:
            raise upstream_error("Could not reach the OpenAI provider.", retryable=True) from exc
        except RateLimitError as exc:
            raise upstream_rate_limited(
                retry_after=exc.response.headers.get("retry-after")
            ) from exc
        except APIStatusError as exc:
            detail = self._detail(exc)
            logger.warning(
                "upstream_http_error",
                phase="complete",
                model=request.model,
                status=exc.status_code,
                detail=detail.strip() or None,
            )
            raise self._map_status_error(exc) from exc
        except OpenAIError as exc:
            # A non-HTTP SDK error (bad config, serialization, unexpected shape).
            # Treat as a non-retryable upstream error rather than leaking the raw type.
            raise upstream_error(f"OpenAI provider error: {exc}") from exc
        return self._build_result(completion.model_dump())

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        raise NotImplementedError(
            "OpenAIProvider is build/improve-only; forced tool calls are not supported."
        )

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        raise NotImplementedError(
            "OpenAIProvider is build/improve-only; streaming is not supported."
        )

    async def aclose(self) -> None:
        await self._client.close()
