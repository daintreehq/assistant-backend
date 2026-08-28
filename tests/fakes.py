"""In-memory fakes used across the test suite.

``FakeProvider`` implements the :class:`ChatProvider` protocol without any
network access. Behavior is configured by assigning canned results or callables;
every request is recorded for assertions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from daintree_assistant_server.runbooks.decision import SelectionDecision
from daintree_assistant_server.services.compaction import (
    CompactionRequest,
    CompactionResult,
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
                arguments_json=json.dumps({"runbook_ids": [], "confidence": 0.0, "task_type": ""}),
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


def tool_call_for(runbook_ids: list[str], *, confidence: float = 0.9, task_type: str = "task") -> ProviderToolCallResult:
    return ProviderToolCallResult(
        name="select_runbooks",
        arguments_json=json.dumps(
            {"runbook_ids": runbook_ids, "confidence": confidence, "task_type": task_type}
        ),
        usage=ProviderUsage(
            prompt_tokens=3000, completion_tokens=50, total_tokens=3050, cached_tokens=2800
        ),
    )


def selection_result(
    runbook_ids: list[str],
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
    order (task_type/reason written before runbook_ids — the non-thinking selector's
    inline scratchpad); parsing itself is order-agnostic.
    """
    return make_result(
        content=json.dumps(
            {
                "task_type": task_type,
                "reason": "matches the current task",
                "runbook_ids": runbook_ids,
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


class FakeCompactor:
    """A scriptable :class:`Compactor` for the declaration/delivery path.

    Deliberately NOT a fake OpenRouter provider. The producer half is issue #60;
    what this stands in for is the seam, so the transport — marker detection, the
    speculative start, the delivery gate, the wire shape — is testable before any
    of that exists.
    """

    def __init__(
        self,
        *,
        content: str | None = "agent-1 at /w/a; blocked on review",
        available: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
        observer: Callable[[], Any] | None = None,
    ) -> None:
        self.available = available
        self.content = content
        self.error = error
        self.delay = delay
        self._observer = observer
        self.calls: list[CompactionRequest] = []
        #: What ``observer`` returned at the moment ``compact`` was ENTERED —
        #: how a test proves the call started while generation was still running.
        self.observed: list[Any] = []
        self.cancellations = 0

    async def compact(self, request: CompactionRequest) -> CompactionResult | None:
        self.calls.append(request)
        if self._observer is not None:
            self.observed.append(self._observer())
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancellations += 1
            raise
        if self.error is not None:
            raise self.error
        if self.content is None:
            return None
        return CompactionResult(content=self.content)


# ---------------------------------------------------------------------------
# The scripted selection seam.
# ---------------------------------------------------------------------------
#
# Production selection is the LOCAL two-stage ONNX classifier, which decides for
# real — exactly wrong for orchestration tests that need to SCRIPT the outcome
# ("returns these ids", "fails open", "carries this cost") to drive one specific
# orchestrator branch.
#
# That seam used to be the prompt-based `Selector`, kept alive in `src/` purely
# because the suite was written against it: ninety-odd assertions set
# `FakeProvider.selection_result` and read `selection_requests`. Keeping a whole
# billed-model-call implementation in the shipped package to serve a test fixture
# is the wrong trade, and the class is gone. What survives is this — the same
# interface, the same FakeProvider slots, and none of it in the product.
#
# The JSON parsing below is a deliberate SIMPLIFICATION of what the deleted
# parser did, and the difference is worth stating: it validates ids against the
# catalog, dedups, and clamps to the active-set cap, because tests depend on
# those. It does not reproduce the malformed-output repair round-trip — that
# behaviour belonged to a model that could return junk, and a test that hands
# this fake junk is scripting a degraded selection, which it can say directly.

_SELECTOR_FAILED_OPEN = "runbook_selector_failed_open"


class ScriptedSelector:
    """A `RunbookSelector` whose answer comes from `FakeProvider`, not a model.

    Reads the same `selection_result` / `selection_error` slots the prompt-based
    selector consumed and appends to the same `selection_requests` list, so the
    assertions written against that seam keep meaning what they meant.
    """

    def __init__(self, provider: FakeProvider, catalog_manager: Any, settings: Any) -> None:
        self.provider = provider
        self.catalog_manager = catalog_manager
        self.settings = settings

    @property
    def catalog(self) -> Any:
        return self.catalog_manager.catalog

    async def select(
        self,
        conversation: list[Any],
        active_ids: list[str],
        *,
        api_key: Any = None,
        turn_context: str | None = None,
        available_tools: list[str] | None = None,
        catalog: Any = None,
    ) -> SelectionDecision:
        catalog = catalog if catalog is not None else self.catalog
        # Recorded so tests can assert a selection RAN and what it saw. The
        # object is a stand-in for the upstream request the old seam built; only
        # its presence and count are ever asserted.
        self.provider.selection_requests.append(
            SimpleNamespace(
                conversation=list(conversation),
                active_ids=list(active_ids),
                turn_context=turn_context,
                available_tools=list(available_tools or []),
                # The catalog the selection actually consulted — a
                # sub-agent narrows it, and the server-pinned runbooks are
                # already filtered out of `selectable_ids`, so tests
                # asserting what a selection could NAME assert on this.
                catalog=catalog,
                catalog_revision=catalog.revision,
            )
        )

        if self.provider.selection_error is not None:
            # Fail OPEN, like production: a selection that cannot be made must
            # not take the turn down with it.
            return SelectionDecision.degraded_open([_SELECTOR_FAILED_OPEN])

        result = self.provider.selection_result
        if result is None:
            return SelectionDecision.degraded_open([_SELECTOR_FAILED_OPEN])
        if isinstance(result, list):
            result = result[0] if result else None
            if result is None:
                return SelectionDecision.degraded_open([_SELECTOR_FAILED_OPEN])

        parsed = _parse_scripted_selection(
            result.text, catalog, self.settings.max_active_runbooks
        )
        if parsed is None:
            return SelectionDecision.degraded_open(
                [_SELECTOR_FAILED_OPEN],
                usage=result.usage,
                endpoint=result.endpoint,
                calls=1,
            )
        ids, confidence, task_type, reason, docs_query = parsed
        return SelectionDecision(
            runbook_ids=ids,
            ran=True,
            degraded=False,
            confidence=confidence,
            task_type=task_type,
            reason=reason,
            # The PRODUCTION shape. `LocalSelector` runs in process: it reports
            # no endpoint of its own beyond the literal "local", and zero calls,
            # because it makes none. A seam that reported `calls=1` and a
            # provider endpoint would let orchestration tests assert telemetry
            # no real turn can produce — which is the whole hazard of a seam
            # living in test code.
            #
            # ALWAYS None, exactly as `LocalSelector` reports. Forwarding the
            # scripted result's usage meant "a billed call happened", which in
            # `turn_cost` is what clears `cost.complete` — so a seam that did it
            # would floor the reported total of every turn in the suite while
            # production floored none, and no test could see the difference.
            #
            # A test that wants to model a selection which COST something is
            # modelling a thing this product cannot do; it should build a
            # `SelectionDecision` directly and say so, rather than reaching it
            # through a seam that pretends otherwise.
            usage=None,
            endpoint="local",
            calls=0,
            warnings=[],
            # Every selected id at the scripted `confidence`. The real head
            # scores each candidate independently, so this is a simplification —
            # but it is the RIGHT one for a seam: it makes `confidence` the
            # single dial a test turns to drive the opener gate
            # (`runbooks/openers.py`), instead of asking every existing fixture
            # to grow a per-id score map it does not care about. A test that
            # needs runbooks at DIFFERENT scores is testing the gate rather than
            # the orchestration around it, and should build a
            # `SelectionDecision` directly.
            scores=dict.fromkeys(ids, confidence),
            docs_query=docs_query,
        )


def _parse_scripted_selection(
    raw_json: str, catalog: Any, max_active: int
) -> tuple[list[str], float, str, str, str | None] | None:
    """Read a scripted selection out of the JSON a test wrote."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_ids = data.get("runbook_ids")
    if not isinstance(raw_ids, list):
        return None

    clean: list[str] = []
    seen: set[str] = set()
    for item in raw_ids:
        if len(clean) >= max_active:
            break
        if not isinstance(item, str):
            continue
        sid = item.strip()
        if not sid or sid in seen or sid not in catalog.selectable_ids:
            continue
        seen.add(sid)
        clean.append(sid)
    if raw_ids and not clean and max_active > 0:
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    docs_query = data.get("docs_query")
    if not isinstance(docs_query, str) or not docs_query.strip():
        docs_query = None
    else:
        docs_query = docs_query.strip()[:120]
    return (
        clean,
        confidence,
        str(data.get("task_type") or "")[:80],
        str(data.get("reason") or "")[:300],
        docs_query,
    )
