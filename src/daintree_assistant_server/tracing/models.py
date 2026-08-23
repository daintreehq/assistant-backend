"""Typed records for the training-trace plane.

These are DURABLE DATA, not wire contracts, and that difference decides one thing
the repository is otherwise strict about: ``schema_version`` is real here.

The project's versioning policy (CLAUDE.md, "Versioning (pre-release)") says
prompts, tasks and prompt families carry no version — everything on disk is
simply "the current version", because the only client moves in lockstep. A trace
written today and read by an exporter six months and forty commits later has no
such luxury: nothing can move in lockstep with a file that already exists. So the
persisted schema is versioned, and it is the ONE thing in this service that is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: Bumped when a change would make an already-written trace unreadable, or would
#: change the meaning of a field an exporter reads. Additive optional fields do
#: not need a bump; a removal or a re-interpretation does.
TRACE_SCHEMA_VERSION = 1

#: The stage that made a model call. Stable strings — an exporter groups on them.
InvocationStage = Literal["fast_response", "executor", "utility", "compaction"]


@dataclass(frozen=True)
class ContentRef:
    """A pointer to stored bytes, never the bytes themselves.

    Events carry these instead of payloads so that the event stream stays small
    and append-only while the large, repeated things (a conversation replayed on
    every tool round) are stored once and referenced many times.
    """

    sha256: str
    byte_length: int
    media_type: str
    encoding: str = "utf-8"
    compression: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "encoding": self.encoding,
        }
        if self.compression:
            out["compression"] = self.compression
        return out


@dataclass(frozen=True)
class TraceHeader:
    """Written once, first, and enough on its own to decide whether to export.

    Identity is PSEUDONYMOUS by construction: ``session_key`` and ``turn_id_hash``
    are HMACs under a dedicated trace key, never the raw ids and never derived
    from the state-signing key. ``request_id`` is the exception and is stored raw,
    deliberately — it already appears in operational logs and is the intended
    handle for correlating a trace with them.
    """

    trace_id: str
    request_id: str
    session_key: str
    turn_id_hash: str | None
    instruction_revision: int
    round_index: int
    profile: str
    started_at: str
    environment: str
    build_sha: str
    server_version: str
    protocol_version: int
    prompt_family: str
    runbook_catalog_revision: str
    training_eligible: bool
    eligibility_reason: str
    schema_version: int = TRACE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "session_key": self.session_key,
            "turn_id_hash": self.turn_id_hash,
            "instruction_revision": self.instruction_revision,
            "round": self.round_index,
            "profile": self.profile,
            "started_at": self.started_at,
            "environment": self.environment,
            "build_sha": self.build_sha,
            "server_version": self.server_version,
            "protocol_version": self.protocol_version,
            "prompt_family": self.prompt_family,
            "runbook_catalog_revision": self.runbook_catalog_revision,
            "training_eligible": self.training_eligible,
            "eligibility_reason": self.eligibility_reason,
        }


@dataclass(frozen=True)
class TraceEvent:
    """One ordered occurrence.

    ``monotonic_offset_ms`` is the authority on ORDER and DURATION within a
    process; ``occurred_at`` is a wall clock for correlating across services. The
    two are kept separately because a wall clock can step backwards (NTP) and an
    ordering derived from one that did is silently wrong.
    """

    event_id: str
    sequence: int
    event_type: str
    occurred_at: str
    monotonic_offset_ms: int
    parent_event_ids: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    content_ref: ContentRef | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "monotonic_offset_ms": self.monotonic_offset_ms,
            "attributes": self.attributes,
        }
        if self.parent_event_ids:
            out["parent_event_ids"] = list(self.parent_event_ids)
        if self.content_ref is not None:
            out["content_ref"] = self.content_ref.as_dict()
        return out


@dataclass(frozen=True)
class InvocationUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class ModelInvocationTrace:
    """One provider call, in the same shape whichever stage made it.

    ``request_ref`` is the NORMALIZED ``ProviderChatRequest``; ``wire_request_ref``
    is what the adapter actually emitted. They differ in ways that matter to
    anyone replaying a trace — the normalized form says ``max_tokens`` while a
    Cerebras body says ``max_completion_tokens``, Together adds its own
    ``reasoning`` object, and ``cache_session_id`` is dropped on the way out. A
    trace holding only one of the two cannot answer "what did the provider see".

    **``wire_request_ref`` is not populated yet**, and is left None rather than
    filled with the normalized bytes: a field that claims to be the wire body and
    is not would make a replay confidently wrong, which is worse than a gap a
    reader can see. Capturing it needs an observer hook on the provider boundary
    rather than the orchestrator reaching into a private ``_build_body``;
    ``TODO.md`` carries that work.

    ``accepted`` is what separates a generation the user got from one the server
    threw away (a bare-intent re-roll, an invalid fast response). Both are
    recorded; only one is the turn.
    """

    invocation_id: str
    stage: InvocationStage
    provider: str
    model: str
    started_at: str
    completed_at: str | None = None
    first_event_at: str | None = None
    finish_reason: str | None = None
    usage: InvocationUsage | None = None
    request_ref: ContentRef | None = None
    wire_request_ref: ContentRef | None = None
    response_ref: ContentRef | None = None
    raw_response_ref: ContentRef | None = None
    context_manifest_ref: ContentRef | None = None
    attempt: int = 0
    accepted: bool = True
    discard_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        def ref(value: ContentRef | None) -> dict[str, Any] | None:
            return value.as_dict() if value is not None else None

        return {
            "invocation_id": self.invocation_id,
            "stage": self.stage,
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "first_event_at": self.first_event_at,
            "completed_at": self.completed_at,
            "finish_reason": self.finish_reason,
            "usage": self.usage.as_dict() if self.usage else None,
            "request_ref": ref(self.request_ref),
            "wire_request_ref": ref(self.wire_request_ref),
            "response_ref": ref(self.response_ref),
            "raw_response_ref": ref(self.raw_response_ref),
            "context_manifest_ref": ref(self.context_manifest_ref),
            "attempt": self.attempt,
            "accepted": self.accepted,
            "discard_reason": self.discard_reason,
        }


@dataclass(frozen=True)
class TraceOutcome:
    """How the turn ended, recorded whether or not it ended well."""

    status: Literal["completed", "failed", "cancelled", "incomplete"]
    finish_reason: str | None = None
    error_code: str | None = None
    incomplete_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "finish_reason": self.finish_reason,
            "error_code": self.error_code,
            "incomplete_reason": self.incomplete_reason,
        }
