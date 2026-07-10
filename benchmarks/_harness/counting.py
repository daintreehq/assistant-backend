"""A transparent provider decorator that makes model-call structure observable.

The production ``TaskRunner``/``Selector`` sum usage across attempts and expose no
``repaired`` / ``attempts`` field, so a repaired-then-parsed result is byte-
indistinguishable from a clean first-try one in any saved artifact. Several
suites (workflow-intelligence first-try validity, utility repair rate) need that
distinction, so they wrap the real provider in this decorator, which:

- delegates every ``ChatProvider`` method to the inner provider **byte-identically**
  and re-raises its exceptions unchanged (retry keys off ``DaintreeError.retryable``;
  a swallowed/rewrapped error would alter attempt counts and corrupt the numbers);
- counts ``complete`` calls and captures the FIRST result of each logical
  ``run()`` (bracket a run with ``reset()`` → run → read ``complete_calls`` /
  ``first_result``: 1 call = first-try, 2 = one repair);
- aggregates reported ``cached_tokens`` / ``prompt_tokens`` for a cache view.

It changes nothing the model sees — the decorated provider builds the exact same
request bodies.
"""

from __future__ import annotations

from typing import Any

from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)


class CountingProvider:
    """Wraps a ``ChatProvider``; observes calls without altering behavior."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.complete_calls = 0
        self.stream_calls = 0
        self.tool_call_calls = 0
        self.total_complete_calls = 0
        self.first_result: ProviderChatResult | None = None
        self.prompt_tokens_sum = 0
        self.cached_tokens_sum = 0

    def reset(self) -> None:
        """Clear the per-run counters (call before each logical ``run``)."""
        self.complete_calls = 0
        self.stream_calls = 0
        self.tool_call_calls = 0
        self.first_result = None

    def _account(self, usage: ProviderUsage | None) -> None:
        if usage is None:
            return
        if usage.prompt_tokens:
            self.prompt_tokens_sum += usage.prompt_tokens
        if usage.cached_tokens:
            self.cached_tokens_sum += usage.cached_tokens

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        self.complete_calls += 1
        self.total_complete_calls += 1
        result = await self._inner.complete(request)
        if self.first_result is None:
            self.first_result = result
        self._account(result.usage)
        return result

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        self.stream_calls += 1
        return await self._inner.open_stream(request)

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        self.tool_call_calls += 1
        result = await self._inner.structured_tool_call(
            request, function_name=function_name, function_schema=function_schema
        )
        self._account(result.usage)
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()
