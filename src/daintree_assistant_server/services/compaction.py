"""The seam a context compactor plugs into, and the span it is allowed to see.

The serving host gives no measured discount on cached tokens, so every round of a turn pays full
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
    ``None`` checks. Nothing is started, nothing is awaited, and no event is
    emitted.

    It is also what switches the declaration rule OFF. ``available`` is False, and
    the container reads exactly that when building the assembler, so the base prompt
    omits ``15-response-declaration.md`` (646 bytes on every request, the partial
    plus its join separator) and no reply
    is asked for a control line. The rule used to ship regardless, to buy compliance
    telemetry — but a marker with no consumer is prompt tax, and the serving model
    was measured emitting it on 0 of 20 tool-calling turns, so the telemetry said
    only that an unread contract was also unhonoured.

    The scanner and the stripper stay wired anyway: they cost a prefix probe, and a
    model that emits the marker unprompted should still not show it to a user.
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
      spliced in — recognised only on a USER message, the shape the server
      actually emits — and is then advanced to the next user message, so a span
      can never open on a `tool` result whose assistant call sits outside it;
    * the END stops at the user's current instruction, so the LIVE turn — the ask
      itself and every tool round it has spawned — stays raw. Reconciling a task
      that is still running would summarise a story whose ending has not been
      written, which is exactly the state a model still needs verbatim.

    A span that would split a tool transaction is then refused outright
    (``_is_tool_closed``), because the request boundary permits an ordering these
    two rules alone do not survive.

    Returns ``None`` when nothing sits between the two, which is every short
    conversation and every turn immediately after a block was delivered.
    """
    # The reserved name only means anything on a USER message, because that is the
    # only shape the server ever emits. Honouring it on an assistant or tool
    # message would let a client — by accident or otherwise — move the boundary
    # into the middle of a tool transaction, and the span would then begin on an
    # orphaned `tool` result whose assistant call sits outside it. That is a 400
    # from the compactor's own upstream, and a splice that leaves the CLIENT's
    # next request malformed.
    start = 0
    for index, message in enumerate(messages):
        if message.role == "user" and message.name == COMPACTION_BLOCK_NAME:
            start = index + 1

    # Belt and braces on the same failure: the span must OPEN on a user message.
    # A prefix that begins mid-transaction is not a coherent unit to reconcile
    # anyway, and starting at a turn boundary makes orphaning structurally
    # impossible rather than merely unlikely.
    while start < len(messages) and messages[start].role != "user":
        start += 1

    end = -1
    for index in range(len(messages) - 1, start - 1, -1):
        if messages[index].role == "user" and messages[index].name != COMPACTION_BLOCK_NAME:
            end = index
            break

    if end <= start:
        return None
    if not _is_tool_closed(messages, start, end):
        return None
    return CompactionSource(start=start, end=end, messages=tuple(messages[start:end]))


def _is_tool_closed(messages: Sequence[ChatMessage], start: int, end: int) -> bool:
    """Whether every tool call in the span has its results there too, and vice versa.

    Ending the span on a user message keeps calls and results together in a
    well-ordered conversation, but the request boundary does not REQUIRE that
    ordering — a result may legally arrive after the next user message. Compacting
    such a span would delete an assistant's call while leaving its result behind
    (or the reverse), and the client's next request would carry a tool result
    answering nothing.

    Refusing outright rather than trimming to fit: this is a conversation shaped
    in a way nothing here expects, and skipping compaction for a turn costs
    nothing at all.
    """
    inside_calls: set[str] = set()
    outside_calls: set[str] = set()
    for index, message in enumerate(messages):
        if not message.tool_calls:
            continue
        target = inside_calls if start <= index < end else outside_calls
        target.update(call.id for call in message.tool_calls)

    for index, message in enumerate(messages):
        if message.role != "tool" or not message.tool_call_id:
            continue
        result_inside = start <= index < end
        if message.tool_call_id in inside_calls and not result_inside:
            return False
        if message.tool_call_id in outside_calls and result_inside:
            return False
    return True


def default_compactor() -> Compactor:
    """The compactor a deployment gets when nothing is injected.

    One function so the answer cannot be given twice. `main.build_container` and
    `tools/baseline.py` both need it — the container to decide whether the base
    prompt carries the declaration rule, the baseline to record how many bytes
    that prompt actually is — and if they answered it separately, the day a real
    producer ships would be the day production started sending a prompt the
    release baseline had never measured.
    """
    return NullCompactor()
