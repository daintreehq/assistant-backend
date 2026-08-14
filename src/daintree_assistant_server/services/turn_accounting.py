"""Turn accounting — what a turn produced, and what it cost the caller.

Every upstream call is funded by the CALLER's own OpenRouter key, so these
figures are their bill rather than an internal metric. That shapes every rule
here, and each one exists because its opposite is a plausible-looking lie:

* an unreported cost stays ``None``, never ``0.0`` — a zero claims the turn was
  free, which is a statement about someone's money we have no basis for;
* a partial sum is marked ``complete=False`` rather than presented as a total;
* a discarded generation — a re-rolled round, a losing speculative stream — is
  still summed, because the caller was charged for it whether or not they ever
  saw a byte of it.

Extracted from ``chat_orchestrator`` unchanged. These were already pure static
methods; the arithmetic is easier to check, and to test, away from the streaming
control flow it happens to be called from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..contracts.daintree_api import RespondUsage, TurnCost
from ..observability import get_logger
from ..providers.base import ProviderUsage

if TYPE_CHECKING:  # pragma: no cover - typing-only, no runtime cycle
    from .chat_orchestrator import _RespondMeta

logger = get_logger("daintree.respond")


def turn_cost_with_speculation(
    usage: ProviderUsage,
    meta: _RespondMeta,
    *,
    discarded_usage: ProviderUsage,
    speculation_billed: bool,
) -> TurnCost | None:
    """``turn_cost`` with a losing speculation's spend folded in.

    One home for the arithmetic, called identically by both response paths —
    the streaming path cannot see the ``_PreparedTurn`` itself, so the two
    pieces it needs are passed explicitly rather than duplicated inline.
    """
    if not speculation_billed:
        return turn_cost(usage, meta)
    return turn_cost(
        sum_usage(usage, discarded_usage),
        meta,
        # A losing speculation is only fully accounted for if it reported a
        # cost before we cancelled it. Otherwise real spend happened that
        # nothing can measure, and the total is a floor.
        complete=discarded_usage.cost is not None,
    )


def turn_cost(
    main: ProviderUsage, meta: _RespondMeta, *, complete: bool = True
) -> TurnCost | None:
    """Total USD this request charged the caller, across every upstream call.

    A turn is normally TWO billed calls — the skill selector and the main
    completion — plus, on a re-rolled round, a generation the user never saw.
    All of it is charged to the caller's own key, so reporting only the main
    call would understate their bill by the part they cannot see.

    Both figures are OpenRouter's own reported ``cost``, not a token-price
    estimate we compute: the router knows which of ~24 endpoints served the
    request and what cache discount applied, and we do not.

    Returns ``None`` when NOTHING reported a cost — the absence says "we do
    not know", which a zero would misreport as "free".

    The subtler case is a PARTIAL answer, and it is why ``complete`` exists.
    A selector that ran but whose cost went unreported would otherwise
    contribute 0.0 to a field named ``total``, quietly turning a partial sum
    into a claimed complete one. So a component is treated as unknown only
    when its call actually happened, and any unknown clears ``complete``.

    ``complete=False`` may also be passed by the caller for a cost we know
    was incurred and cannot observe at all — a losing speculative stream that
    was cancelled before its usage arrived.
    """
    selector_usage = meta.skills.selector.usage
    selector = selector_usage.cost if selector_usage is not None else None
    # "Ran" is what makes a missing cost UNKNOWN rather than genuinely zero.
    # A turn that skipped selection entirely (continuation round, no-task
    # fast path) has no selector usage at all, and its absent cost is a
    # truthful zero rather than a gap.
    selector_ran = selector_usage is not None
    if main.cost is None and selector is None and not selector_ran:
        return None
    if main.cost is None and selector is None:
        return None
    known_complete = complete and main.cost is not None
    if selector_ran and selector is None:
        known_complete = False
    return TurnCost(
        total=(main.cost or 0.0) + (selector or 0.0),
        main=main.cost,
        selector=selector,
        complete=known_complete,
    )

def log_turn_usage(
    usage: ProviderUsage,
    *,
    request_id: str,
    stream: bool,
    finish_reason: str | None,
    turn_cost: TurnCost | None = None,
) -> None:
    """What this turn cost the caller, as metadata.

    The response body carries the same numbers to the CLI; this is the half
    that survives the request, so a tester reporting "it burned through my
    credit" can be answered from logs rather than from memory.

    ``cache_ratio`` is the number the prompt-cache work exists to move, and
    the one most worth watching regress: the assembler's whole layout is
    built to keep the tool schemas byte-stable, and a drop here is the first
    symptom that something started re-encoding them.
    """
    prompt = usage.prompt_tokens or 0
    logger.info(
        "turn_usage",
        request_id=request_id,
        stream=stream,
        finish_reason=finish_reason,
        prompt_tokens=prompt,
        completion_tokens=usage.completion_tokens or 0,
        cached_tokens=usage.cached_tokens or 0,
        cache_ratio=(round((usage.cached_tokens or 0) / prompt, 3) if prompt else None),
        cost_usd=usage.cost,
        # The figure that answers "what did this request cost me?" — every
        # upstream call the turn made, including the selector.
        turn_cost_usd=turn_cost.total if turn_cost else None,
        selector_cost_usd=turn_cost.selector if turn_cost else None,
    )

def response_usage(usage: ProviderUsage) -> RespondUsage:
    return RespondUsage(
        prompt_tokens=usage.prompt_tokens or 0,
        completion_tokens=usage.completion_tokens or 0,
        total_tokens=usage.total_tokens or 0,
        cached_tokens=usage.cached_tokens or 0,
        # NOT `or 0`: an unreported cost must stay None. Zero would read as
        # "this turn was free", which is a claim about the caller's bill we
        # have no basis for.
        cost=usage.cost,
    )


def sum_usage(first: ProviderUsage | None, second: ProviderUsage | None) -> ProviderUsage:
    """Total tokens across both attempts.

    The caller's own key funded the discarded generation too, so reporting only
    the accepted attempt would under-bill what the turn actually cost them.
    ``None`` means "not reported" and stays ``None`` only if neither side had a
    number, so a provider that omits usage is not silently turned into a zero.
    """

    def add(a: int | None, b: int | None) -> int | None:
        if a is None:
            return b
        return a if b is None else a + b

    def add_cost(a: float | None, b: float | None) -> float | None:
        if a is None:
            return b
        return a if b is None else a + b

    first = first or ProviderUsage()
    second = second or ProviderUsage()
    return ProviderUsage(
        prompt_tokens=add(first.prompt_tokens, second.prompt_tokens),
        completion_tokens=add(first.completion_tokens, second.completion_tokens),
        total_tokens=add(first.total_tokens, second.total_tokens),
        cached_tokens=add(first.cached_tokens, second.cached_tokens),
        # A discarded attempt was still billed to the caller, so its cost is
        # part of what this turn cost them — the same reason the tokens are
        # summed rather than replaced.
        cost=add_cost(first.cost, second.cost),
    )

