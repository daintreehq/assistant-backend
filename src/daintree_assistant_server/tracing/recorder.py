"""The entry point the orchestrator holds, and the pseudonymisation it applies.

``TraceRecorder`` is what the container injects. There are two implementations and
the orchestrator cannot tell them apart: ``NullTraceRecorder`` for every ordinary
deployment, and the real one where an operator has turned capture on.

**Identity never reaches storage in the clear.** Session and turn ids are HMACed
under a DEDICATED key (``TRACE_IDENTITY_KEY``), never the state-signing key and
never the state salt. Sharing a key across two purposes means a compromise of
either is a compromise of both, and it also means rotating one silently
re-pseudonymises the other — which would split one person's traces into two
identities mid-corpus and quietly corrupt the very grouping the key exists to
provide.

``request_id`` IS stored raw, and that is the deliberate exception: it is already
in the operational logs, and correlating a trace with a log line is the whole
reason it is there.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any, Protocol

from ..observability import get_logger
from .eligibility import TraceEligibility
from .models import TraceHeader, TraceOutcome
from .scope import TraceScope
from .sinks.base import TraceSink
from .sinks.null import NullTraceSink

logger = get_logger(__name__)


class TraceRecorder(Protocol):
    """What the orchestrator holds. Two implementations, indistinguishable to it.

    The signature is spelled out rather than taking ``**kwargs``: a Protocol that
    accepts anything checks nothing, and the header fields are exactly the part a
    caller can silently get wrong — a missing ``session_id`` would pseudonymise
    every trace to the same key and merge a whole corpus into one subject.
    """

    @property
    def enabled(self) -> bool: ...

    async def start_trace(
        self,
        *,
        request_id: str,
        session_id: str | None,
        turn_id: str | None,
        instruction_revision: int = 0,
        round_index: int = 0,
        profile: str = "assistant",
        environment: str = "development",
        build_sha: str = "",
        server_version: str = "",
        protocol_version: int = 0,
        prompt_family: str = "main",
        runbook_catalog_revision: str = "",
    ) -> TraceScope | NullTraceScope: ...

    async def aclose(self) -> None: ...


class NullTraceScope:
    """A scope that records nothing, matching ``TraceScope``'s surface.

    Duck-typed rather than a shared base class on purpose: the null path must not
    pay for a single attribute lookup it does not need, and there is no behaviour
    to share — every method is a return.
    """

    trace_id = ""
    failures = 0

    async def record_event(self, event_type: str, **kwargs: Any) -> str:
        return ""

    async def record_model_invocation(self, invocation: Any) -> str:
        return ""

    def store_content(self, content: Any, *, content_type: str | None = None) -> None:
        return None

    async def complete(self, outcome: TraceOutcome) -> None:
        return None

    async def abort(self, outcome: TraceOutcome) -> None:
        return None

    def log_fields(self) -> dict[str, Any]:
        return {}


#: Shared: it is stateless, and every request on an untraced deployment gets it.
NULL_SCOPE = NullTraceScope()


class NullTraceRecorder:
    """What every deployment holds unless tracing is deliberately configured."""

    @property
    def enabled(self) -> bool:
        return False

    async def start_trace(
        self,
        *,
        request_id: str,
        session_id: str | None,
        turn_id: str | None,
        instruction_revision: int = 0,
        round_index: int = 0,
        profile: str = "assistant",
        environment: str = "development",
        build_sha: str = "",
        server_version: str = "",
        protocol_version: int = 0,
        prompt_family: str = "main",
        runbook_catalog_revision: str = "",
    ) -> NullTraceScope:
        return NULL_SCOPE

    async def aclose(self) -> None:
        return None


def _new_trace_id() -> str:
    millis = int(datetime.now(UTC).timestamp() * 1000)
    return f"trc_{millis:013d}{secrets.token_hex(6)}"


class DefaultTraceRecorder:
    """Mints pseudonyms and scopes; owns the sink's lifetime."""

    def __init__(
        self,
        *,
        sink: TraceSink,
        identity_key: bytes,
        eligibility: TraceEligibility,
        max_event_bytes: int,
        max_pending_bytes: int,
        flush_grace_seconds: float,
    ) -> None:
        self._sink = sink
        self._identity_key = identity_key
        self._eligibility = eligibility
        self._max_event_bytes = max_event_bytes
        self._max_pending_bytes = max_pending_bytes
        self._flush_grace_seconds = flush_grace_seconds

    @property
    def enabled(self) -> bool:
        return self._eligibility.enabled

    def _hmac(self, domain: str, value: str) -> str:
        """Domain-separated, so a session id and a turn id can never collide.

        The NUL separator is what makes the domain unambiguous: without it,
        ``("session", "a\\0b")`` and ``("session\\0a", "b")`` hash identically, and
        two different subjects share one pseudonym.
        """
        material = f"{domain}\0{value}".encode()
        return "hmac256:" + hmac.new(self._identity_key, material, hashlib.sha256).hexdigest()

    async def start_trace(
        self,
        *,
        request_id: str,
        session_id: str | None,
        turn_id: str | None,
        instruction_revision: int = 0,
        round_index: int = 0,
        profile: str = "assistant",
        environment: str = "development",
        build_sha: str = "",
        server_version: str = "",
        protocol_version: int = 0,
        prompt_family: str = "main",
        runbook_catalog_revision: str = "",
    ) -> TraceScope | NullTraceScope:
        if not self._eligibility.enabled:
            return NULL_SCOPE

        trace_id = _new_trace_id()
        header = TraceHeader(
            trace_id=trace_id,
            request_id=request_id,
            session_key=self._hmac("session", session_id or ""),
            # Scoped to the SESSION as well as the turn. A bare turn id is only
            # unique within its session, so hashing it alone would merge two
            # different people's turns under one pseudonym whenever their clients
            # happened to mint the same id.
            turn_id_hash=(
                self._hmac("turn", f"{session_id or ''}\0{turn_id}") if turn_id else None
            ),
            instruction_revision=instruction_revision,
            round_index=round_index,
            profile=profile,
            started_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            environment=environment,
            build_sha=build_sha,
            server_version=server_version,
            protocol_version=protocol_version,
            prompt_family=prompt_family,
            runbook_catalog_revision=runbook_catalog_revision,
            training_eligible=self._eligibility.training_eligible,
            eligibility_reason=self._eligibility.reason,
        )
        scope = TraceScope(
            trace_id=trace_id,
            header=header,
            sink=self._sink,
            capture_content=self._eligibility.capture_content,
            max_event_bytes=self._max_event_bytes,
            max_pending_bytes=self._max_pending_bytes,
            flush_grace_seconds=self._flush_grace_seconds,
        )
        try:
            # Opening storage is optional work on the first-byte path. Give it
            # the same bounded grace as the terminal flush; an outage must leave
            # the turn untraced rather than prevent generation from starting.
            async with asyncio.timeout(self._flush_grace_seconds):
                await self._sink.open(trace_id, header.as_dict())
        except Exception:
            # The opening call runs outside the scope's event-write guard.
            # Left unguarded it would turn a storage
            # outage — or, far more likely, an expired credential — into a failed
            # user turn, which is the single thing this subsystem promises never
            # to do. An untraced turn is the correct degradation.
            logger.warning("trace_write_failed", reason="open_failed")
            return NULL_SCOPE
        return scope

    async def aclose(self) -> None:
        await self._sink.aclose()


__all__ = [
    "NULL_SCOPE",
    "DefaultTraceRecorder",
    "NullTraceRecorder",
    "NullTraceScope",
    "TraceRecorder",
    "TraceSink",
    "NullTraceSink",
]
