"""What a selection step decided, however it decided it.

Split out of the retired prompt-based selector when that was deleted. The type
outlived the class: ``resolve_active_runbooks`` consumes it, the orchestrator logs
it, and the LOCAL two-stage selector produces it — none of which ever cared how
the ids were arrived at, which is exactly why the shape survived a change of
mechanism from a billed model call to an in-process ONNX pass.

Several fields still carry the vocabulary of a model call — ``usage``,
``endpoint``, ``calls`` — and they are not vestigial: the orchestrator's turn
accounting and its first-byte telemetry read them, and the local selector
reports zero cost and no endpoint through the same fields rather than through a
second shape nothing else understands. "Selection cost nothing this turn" is a
fact worth being able to state in the same words as "selection cost this much".
"""

from __future__ import annotations

from dataclasses import dataclass

from ..providers.base import ProviderUsage


@dataclass
class SelectionDecision:
    """The outcome of a selection step."""

    runbook_ids: list[str]
    ran: bool
    degraded: bool
    confidence: float | None = None
    task_type: str | None = None
    reason: str | None = None
    usage: ProviderUsage | None = None
    # Diagnostics for the turn's first-byte path, logged by the orchestrator and
    # never sent to the client. ``endpoint`` is the upstream host that served the
    # classification (see ProviderChatResult.endpoint); ``calls`` is 1, or 2 when
    # the output failed to parse and the repair round-trip ran — which silently
    # doubles selector latency and is otherwise invisible in the timing.
    endpoint: str | None = None
    calls: int = 0
    warnings: list[str] = None  # type: ignore[assignment]
    # Whether ``runbook_ids`` REPLACES the prior active set rather than being
    # reused alongside it. A successful selector run is authoritative by
    # definition; the no-task fast path is authoritative WITHOUT having run, so
    # authority cannot be derived from ``ran`` alone. Keeping them separate is
    # what lets the fast path clear the set without claiming a selector call
    # happened — which would falsify the selector telemetry.
    authoritative: bool = False
    # The selector's SECOND answer: what to search the Daintree documentation
    # for, or None. Rides the same call because it is the same classification
    # question about the same evidence, and that call is already on the turn's
    # first-byte path — a dedicated one would double the latency floor to
    # recognize a question the same model just read. `None` on every degraded or
    # reused decision — such a decision has no answer of its own to give. The
    # orchestrator may still carry the query THIS TURN already ran (scoped to the
    # turn id and instruction revision, off the signed state token), so a usage
    # question that takes a tool call to answer keeps its sources on the
    # continuation round; nothing carries across a new instruction.
    docs_query: str | None = None

    def __post_init__(self) -> None:
        if self.warnings is None:
            self.warnings = []

    @classmethod
    def reuse(cls, active_ids: list[str]) -> SelectionDecision:
        return cls(runbook_ids=list(active_ids), ran=False, degraded=False)

    @classmethod
    def cleared(cls) -> SelectionDecision:
        """No task: clear the active set without running the selector.

        ``ran=False`` because none did — the timing and call counters must stay
        honest — but ``authoritative=True`` because this IS the answer for the
        turn, not a fallback to whatever was loaded before.
        """
        return cls(runbook_ids=[], ran=False, degraded=False, authoritative=True)

    @classmethod
    def degraded_open(
        cls,
        warnings: list[str],
        usage: ProviderUsage | None = None,
        *,
        endpoint: str | None = None,
        calls: int = 0,
    ) -> SelectionDecision:
        return cls(
            runbook_ids=[],
            ran=True,
            degraded=True,
            usage=usage,
            endpoint=endpoint,
            calls=calls,
            warnings=list(warnings),
        )
