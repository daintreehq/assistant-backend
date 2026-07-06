"""Internal records and vocabulary for the durable run runtime.

These are storage/service types, not wire models (those live in
``contracts/runs_api.py``). Everything here is immutable: store methods return
fresh records, never mutate them.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# Status vocabularies.
# --------------------------------------------------------------------------

# A run parks in ``idle`` between turns; ``failed`` accepts a new turn (which
# resets it to ``running``). There is no terminal run state — a run is a durable
# conversation, not a job.
RunStatus = Literal["idle", "running", "awaiting_tools", "awaiting_approval", "failed"]

LeaseStatus = Literal[
    "awaiting_approval",  # parked until its approval resolves
    "pending",  # claimable by an executor
    "claimed",  # an executor holds the claim (until claim_expires_at)
    "completed",  # a real result was recorded (result_status ok/error)
    "declined",  # user/approver refused; synthesized tool result recorded
    "cancelled",  # turn cancelled; synthesized tool result recorded
    "expired",  # deadline passed; synthesized tool result recorded
]

LEASE_TERMINAL: frozenset[str] = frozenset({"completed", "declined", "cancelled", "expired"})

# Result statuses an executor may post; the synthesized ones are server-only.
ResultStatus = Literal["ok", "error", "declined", "cancelled", "expired"]
EXECUTOR_RESULT_STATUSES: frozenset[str] = frozenset({"ok", "error", "declined"})

TurnSource = Literal["user", "wake"]

# Tool risk classes considered safe to run unattended on a wake-sourced turn.
# Anything else — including an unknown/unadvertised risk — is treated as
# mutating and requires an explicit approval when no user is present.
READONLY_RISKS: frozenset[str] = frozenset({"read", "local", "ui"})

# --------------------------------------------------------------------------
# Persisted event types (see docs/RUN_SUPERVISOR.md for payloads).
# --------------------------------------------------------------------------

EVENT_RUN_CREATED = "run.created"
EVENT_TURN_SUBMITTED = "turn.submitted"
EVENT_SELECTION_COMPLETED = "selection.completed"
EVENT_ROUND_STARTED = "round.started"
EVENT_ASSISTANT_MESSAGE = "assistant.message"
EVENT_LEASE_CREATED = "lease.created"
EVENT_LEASE_CLAIMED = "lease.claimed"
EVENT_LEASE_RELEASED = "lease.released"
EVENT_TOOL_RESULT = "tool.result"
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_RESOLVED = "approval.resolved"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_TURN_CANCELLED = "turn.cancelled"
EVENT_ROUND_FAILED = "round.failed"
EVENT_RUN_STATUS = "run.status"
EVENT_WAKE_SCHEDULED = "wake.scheduled"
EVENT_WAKE_CANCELLED = "wake.cancelled"
EVENT_WAKE_FIRED = "wake.fired"

# Ephemeral (SSE-only, never persisted, no seq).
EVENT_MODEL_DELTA = "model.delta"
EVENT_MODEL_STATUS = "model.status"
EVENT_STREAM_RESET = "stream.reset"

# --------------------------------------------------------------------------
# Identifiers (same Crockford-ish sortable shape as observability ids).
# --------------------------------------------------------------------------

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode_base32(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def _sortable_id(prefix: str) -> str:
    millis = int(time.time() * 1000)
    return f"{prefix}_{_encode_base32(millis, 10)}{_encode_base32(secrets.randbits(40), 8)}"


def new_run_id() -> str:
    return _sortable_id("run")


def new_lease_id() -> str:
    return _sortable_id("lease")


def new_approval_id() -> str:
    return _sortable_id("appr")


def new_wakeup_id() -> str:
    return _sortable_id("wake")


# --------------------------------------------------------------------------
# Canonical hashing (turn idempotency, result dedup, prompt fingerprints).
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Records.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    status: str
    session_id: str | None
    project_id: str | None
    title: str | None
    active_skills: list[str]
    needs_selection: bool
    selection_policy: str
    selection_force: bool
    current_round: int
    current_turn_id: str | None
    current_turn_source: str
    context: dict[str, Any]
    last_error: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class EventRecord:
    """A persisted (seq is set) or ephemeral (seq is None) run event."""

    run_id: str
    type: str
    payload: dict[str, Any]
    ts: float
    seq: int | None = None


@dataclass(frozen=True)
class LeaseRecord:
    lease_id: str
    run_id: str
    round: int
    tool_call_id: str
    tool_name: str
    arguments: str
    risk: str | None
    status: str
    requires_approval: bool
    approval_id: str | None
    executor_id: str | None
    attempt: int
    claim_expires_at: float | None
    deadline: float
    idempotency_key: str
    result_status: str | None
    result_content: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class LeaseDraft:
    """What the supervisor knows about a tool call before it becomes a lease."""

    tool_call_id: str
    tool_name: str
    arguments: str
    risk: str | None
    requires_approval: bool


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    run_id: str
    lease_id: str
    status: str  # pending | approved | declined
    reason: str | None
    note: str | None
    requested_at: float
    resolved_at: float | None


@dataclass(frozen=True)
class ExecutorRecord:
    executor_id: str
    project_id: str | None
    client: dict[str, Any]
    tools: list[dict[str, Any]]
    capabilities: dict[str, Any]
    ttl_seconds: float
    last_seen: float


@dataclass(frozen=True)
class WakeupRecord:
    wakeup_id: str
    run_id: str
    fire_at: float
    note: str | None
    status: str  # scheduled | fired | cancelled
    created_at: float
    fired_at: float | None


# --------------------------------------------------------------------------
# Store operation outcomes (the store never raises for contended transitions;
# callers translate outcomes into HTTP responses or supervisor actions).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreOutcome:
    """A state-transition attempt: what happened, plus any appended events.

    ``events`` are already committed when the outcome is returned; the caller
    is responsible for publishing them to the live hub (publish-after-commit).
    """

    kind: str
    events: list[EventRecord] = field(default_factory=list)
    run: RunRecord | None = None
    lease: LeaseRecord | None = None
    approval: ApprovalRecord | None = None
    seq: int | None = None
