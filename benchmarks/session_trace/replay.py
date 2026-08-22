"""A scripted (replay) ChatProvider.

Lets ``session_trace`` drive the REAL ``ChatOrchestrator.complete`` deterministically
— no network — while controlling exactly what the selector and the main model
"return" each turn. The provider distinguishes the two calls the orchestrator
makes the same way production does: the selector call carries
``response_format={"type": "json_object"}`` (hardcoded in ``Selector._build_request``);
any other ``complete`` is the main model.
"""

from __future__ import annotations

import json
from typing import Any

from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)

_USAGE = ProviderUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120, cached_tokens=0)


def _result(message: dict[str, Any], finish_reason: str) -> ProviderChatResult:
    return ProviderChatResult(
        id="replay",
        created=0,
        model="replay",
        choices=[{"message": message, "finish_reason": finish_reason}],
        usage=_USAGE,
        finish_reason=finish_reason,
    )


def selector_result(skill_ids: list[str]) -> ProviderChatResult:
    payload = {
        "task_type": "scripted",
        "reason": "replayed selection for the benchmark trace",
        "skill_ids": list(skill_ids),
        "confidence": 0.9,
    }
    return _result({"role": "assistant", "content": json.dumps(payload)}, "stop")


def main_final(text: str) -> ProviderChatResult:
    return _result({"role": "assistant", "content": text}, "stop")


def main_tool_call(name: str, arguments: str, call_id: str) -> ProviderChatResult:
    return _result(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": arguments}}
            ],
        },
        "tool_calls",
    )


class ReplayProvider:
    """Serves the per-turn scripted selector/main responses set by the runner."""

    def __init__(self) -> None:
        self.next_main: ProviderChatResult | None = None
        self.main_calls = 0
        # The slots `ScriptedSelector` reads and records into. Selection makes no
        # provider call any more, so a selector "call" is an entry in this list
        # rather than a `complete()` that carried `response_format=json_object` —
        # the discriminator this class used before, which now matches nothing.
        self.selection_requests: list[Any] = []
        self.selection_result: ProviderChatResult | None = None
        self.selection_error: Exception | None = None

    @property
    def selector_calls(self) -> int:
        return len(self.selection_requests)

    def script_selection(self, skill_ids: list[str]) -> None:
        self.selection_result = selector_result(skill_ids)

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:  # noqa: ARG002
        # `request` is unread now that every `complete` here is the MAIN call:
        # it was only ever inspected to tell a selector call apart from one.
        self.main_calls += 1
        assert self.next_main is not None, "no scripted main response set for this turn"
        return self.next_main

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        raise NotImplementedError("session_trace drives the non-streaming path only")

    async def structured_tool_call(
        self, request: ProviderChatRequest, *, function_name: str, function_schema: dict[str, Any]
    ) -> ProviderToolCallResult:
        raise NotImplementedError

    async def verify_key(self, api_key: Any = None) -> dict[str, Any]:
        # Part of ``ChatProvider``, so it exists; a replayed session never signs in,
        # so scripting a key record would be inventing a fixture nothing reads.
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


