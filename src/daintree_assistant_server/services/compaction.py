"""The seam a context compactor plugs into, and the span it is allowed to see.

Cerebras gives no discount on cached tokens, so every round of a turn pays full
price for the whole conversation it resends. Daintree's history is unusually safe
to compact — most of it is a stale snapshot of a mutable world, and snapshots
supersede — so replacing settled history with reconciled CURRENT STATE is real
money in a long orchestration session.

This module is the CONSUMER half. The producer — the second provider and the task
that actually reconciles a conversation — is issue #60 and does not exist yet;
until it does, ``NullCompactor`` is what the container holds and every turn takes
the same path it took before this landed. Splitting it this way is what makes the
transport testable now: a fake compactor exercises marker detection, the
speculative kick-off, the delivery gate and the wire shape end to end without a
second provider or a single upstream call.

**Choosing the span is deliberately NOT the compactor's job.** Compaction is
append-only, and a frozen segment that gets fed back through a compactor loses a
little more each time with nothing to show it happened. So the span is picked
here, from the conversation's own shape, and the compactor is handed the slice
rather than the conversation: a producer that never SEES an earlier block cannot
re-compact one, whatever it does with what it is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..contracts.daintree_api import COMPACTION_BLOCK_NAME
from ..contracts.openai import ChatMessage


@dataclass(frozen=True, slots=True)
class CompactionSource:
    """The half-open message range eligible for compaction on this turn."""

    #: Index into the turn's ``input.messages`` where the span starts.
    start: int
    #: Exclusive end. Always greater than ``start``.
    end: int
    #: The messages themselves — exactly the slice, nothing around it.
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    """One conversation prefix, handed to a compactor for reconciliation."""

    request_id: str
    turn_id: str
    messages: tuple[ChatMessage, ...]
    #: The largest block worth returning, in UTF-8 bytes. A producer that cannot
    #: reconcile within it should return ``None`` rather than truncate: the
    #: identifiers compaction exists to preserve are exactly what a truncation
    #: cuts, and a block that cannot name the agent it is supervising has made
    #: the turn worse than no block at all.
    max_output_bytes: int


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Reconciled state standing in for the messages that were handed over."""

    content: str


@runtime_checkable
class Compactor(Protocol):
    """Turns a conversation prefix into a block that can replace it.

    A pure computation: it persists nothing and freezes nothing. Whether the
    result is ever used is decided after the model has finished speaking, because
    a declaration made before generation ended can still turn out to be wrong.

    ``compact`` should not raise — every failure is a ``None`` — but the caller
    catches anyway, because "compaction must never be able to fail a turn" is a
    promise the orchestrator owes the user and cannot delegate.
    """

    @property
    def available(self) -> bool:
        """Whether this compactor can actually do the work right now.

        False when the producer is unconfigured (no key, no provider). The
        orchestrator reads it BEFORE spending anything and advertises it on
        ``/v1/daintree/capabilities`` so a client can gate its own handling.
        """
        ...

    async def compact(self, request: CompactionRequest) -> CompactionResult | None:
        """Reconcile the prefix, or ``None`` for "not this turn"."""
        ...


class NullCompactor:
    """The compactor a container holds when no producer is configured.

    Not a test double — this is the shipped implementation until #60 lands, and
    it is what makes "no compaction" a first-class state rather than a scatter of
    ``None`` checks. Nothing is started, nothing is awaited, no event is emitted,
    and the turn is byte-identical to one from before this feature existed.
    """

    available = False

    async def compact(
        self,
        request: CompactionRequest,  # noqa: ARG002 - the Protocol's shape, not ours
    ) -> CompactionResult | None:
        return None


def select_source(messages: Sequence[ChatMessage]) -> CompactionSource | None:
    """The span of settled history this turn may compact, if any.

    Two boundaries, each of which the append-only rule depends on:

    * the START resumes immediately after the last block the client has already
      spliced in, so a frozen segment is never handed back to a compactor;
    * the END stops at the user's current instruction, so the LIVE turn — the ask
      itself and every tool round it has spawned — stays raw. Reconciling a task
      that is still running would summarise a story whose ending has not been
      written, which is exactly the state a model still needs verbatim.

    Returns ``None`` when nothing sits between the two, which is every short
    conversation and every turn immediately after a block was delivered.
    """
    start = 0
    for index, message in enumerate(messages):
        if message.name == COMPACTION_BLOCK_NAME:
            start = index + 1

    end = -1
    for index in range(len(messages) - 1, start - 1, -1):
        if messages[index].role == "user" and messages[index].name != COMPACTION_BLOCK_NAME:
            end = index
            break

    if end <= start:
        return None
    return CompactionSource(start=start, end=end, messages=tuple(messages[start:end]))
