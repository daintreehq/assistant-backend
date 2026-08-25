"""Turn accounting — what a turn produced, and what it cost.

Every upstream call is funded by the SERVER's own credential. That inverted at the
move off caller-supplied keys, and this module's rules were written for the old
world where the figure was the caller's own bill — they survive the inversion
unchanged, because the figure is now Daintree's spend ATTRIBUTED to an account,
which is a number that has to be just as honest. Each rule exists because its
opposite is a plausible-looking lie:

* an unreported cost stays ``None``, never ``0.0`` — a zero claims the turn was
  free, and free is a claim about money nobody has evidence for;
* a partial sum is marked ``complete=False`` rather than presented as a total,
  and that qualifier is LOGGED beside the amount now that the amount sits next to
  an account and a plan;
* a discarded generation — a re-rolled round, a losing speculative stream — is
  still summed, because it was billed whether or not anybody saw a byte of it.

Extracted from ``chat_orchestrator`` unchanged. These were already pure static
methods; the arithmetic is easier to check, and to test, away from the streaming
control flow it happens to be called from.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from ..contracts.daintree_api import RespondUsage, TurnCost
from ..observability import get_logger
from ..providers.base import ProviderUsage
from .account_context import AccountContext

if TYPE_CHECKING:  # pragma: no cover - typing-only, no runtime cycle
    from .chat_orchestrator import _RespondMeta

logger = get_logger("daintree.respond")


def turn_cost(
    main: ProviderUsage,
    meta: _RespondMeta,
    *,
    complete: bool = True,
    fast_response: ProviderUsage | None = None,
) -> TurnCost | None:
    """Total USD this request charged the caller, across every upstream call.

    Runbook selection is LOCAL and unbilled, so an ordinary turn is one billed
    call. A turn that showed a preamble is TWO — the fast stage and the executor —
    and a re-rolled round adds a generation nobody ever saw. All of it lands on
    the server's key, so reporting only the executor would understate the bill by
    the parts that are hardest to notice.

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

    The third way it clears is the least visible: either component may itself
    be a PARTIAL SUM. A bare-intent re-roll bills twice, a malformed selector
    response bills twice, and if only one of those attempts reported, the
    surviving number is non-``None`` and short. ``ProviderUsage.cost_complete``
    (and ``meta.selector_cost_complete``) carry that fact out of the summing
    helpers, because by the time the float arrives here it is unrecoverable.
    """
    selector_usage = meta.runbooks.selector.usage
    selector = selector_usage.cost if selector_usage is not None else None
    # The preamble stage, on exactly the same terms as the selector: a call that
    # RAN and did not report is unknown, not free. `fast_response is None` means
    # the stage did not run at all, and its absent cost is a truthful zero.
    fast_ran = fast_response is not None
    fast_cost = fast_response.cost if fast_response is not None else None
    # "Ran" is what makes a missing cost UNKNOWN rather than genuinely zero.
    # A turn that skipped selection entirely (continuation round, no-task
    # fast path) has no selector usage at all, and its absent cost is a
    # truthful zero rather than a gap.
    selector_ran = selector_usage is not None
    if main.cost is None and selector is None and fast_cost is None:
        # NOTHING anywhere reported a cost, so there is nothing to report. The
        # absence says "we do not know", which a zero would misreport as "free" —
        # and that stays true whether or not a silent call happened, because a
        # block of all-unknowns tells a reader less than no block at all.
        return None
    known_complete = complete and main.cost is not None and main.cost_complete
    if selector_ran and selector is None:
        known_complete = False
    if fast_ran and fast_cost is None:
        known_complete = False
    if fast_response is not None and not fast_response.cost_complete:
        known_complete = False
    if not meta.selector_cost_complete:
        known_complete = False
    return TurnCost(
        total=(main.cost or 0.0) + (selector or 0.0) + (fast_cost or 0.0),
        main=main.cost,
        fast_response=fast_cost,
        selector=selector,
        complete=known_complete,
    )


def cache_ratio(prompt_tokens: int | None, cached_tokens: int | None) -> float | None:
    """Share of a prompt that the serving endpoint answered from its prefix cache.

    ``None`` rather than ``0.0`` whenever EITHER half is unreported, for the same
    reason an unreported cost stays ``None``: a zero here reads as "the cache did
    nothing", which is a claim about cache health that a missing number cannot
    support — and unlike a missing cost, it does not merely sit there. It lands
    in whatever average the dashboard takes and drags it down, so the one number
    this work exists to watch would be moved by the absence of a measurement.

    An explicit ``0`` is a different fact and stays ``0.0``: the endpoint counted
    its hits and there were none, which is a real cold prefix worth seeing.

    One gap this cannot close. ``sum_usage`` adds token counts across attempts
    and treats ``None`` as the identity, so a silent attempt summed with a
    reporting one yields a figure that looks exact and is short — the same
    partial-sum problem ``cost_complete`` exists for, which is not tracked for
    tokens. A ratio here is therefore honest about what it was GIVEN, not about
    whether the sum behind it was whole.
    """
    if prompt_tokens is None or cached_tokens is None or prompt_tokens <= 0:
        return None
    return round(cached_tokens / prompt_tokens, 3)


def log_turn_usage(
    usage: ProviderUsage,
    *,
    request_id: str,
    session_id: str,
    turn_id: str,
    session_round: int,
    stream: bool,
    finish_reason: str | None,
    endpoint: str | None,
    observed_endpoints: Collection[str | None],
    selector_ran: bool,
    selector_usage: ProviderUsage | None,
    selector_endpoint: str | None,
    turn_cost: TurnCost | None = None,
    estimated_prompt_tokens: int | None = None,
    account: AccountContext | None = None,
) -> None:
    """What this turn cost, as metadata, and which account it is attributed to.

    The response body carries the same numbers to the CLI; this is the half that
    survives the request, so "what did this account spend last week" is a query
    rather than a reconstruction.

    The attribution arrives already reduced to its safe projection
    (``AccountContext.log_fields``): a pseudonymous subject, an OAuth client and a
    plan. There is deliberately no raw user id, email or session uuid in it — see
    ``services/account_context.py`` — and it is absent entirely rather than nulled
    on an unattributed turn, because five empty fields on every open-door request
    identify nobody and cost a line's width on every record.

    ``cache_ratio`` is the number the prompt-cache work exists to move, and
    the one most worth watching regress: the assembler's whole layout is
    built to keep the tool schemas byte-stable, and a drop here is the first
    symptom that something started re-encoding them.

    A ratio alone cannot say WHY it dropped, which is why the endpoint that
    produced it is logged beside it. OpenRouter fronts ~24 endpoints for these
    weights and each keeps its OWN node-local prefix cache, so under
    throughput-sorted routing with fallbacks a re-rolled round can be answered
    by a host that has never seen this prefix. Without attribution, a prompt
    layout regression and a routing switch are the same number.

    ``endpoint`` names the host that served the generation these figures
    describe — the attempt finally ACCEPTED, not merely the last one tried.
    ``distinct_endpoints`` counts the named hosts among the attempts whose usage
    is folded into these figures, so a blended ratio identifies itself: ``1`` is
    the ordinary case, ``>1`` says the ratio averages caches that were never the
    same cache, and ``0`` says no attempt reported a name at all.

    It is a FLOOR, not a census. An attempt that named no host cannot be told
    apart from one that repeated a name already counted, so the count can only
    understate a switch — which is the right direction to be wrong in for a
    signal whose job is to raise suspicion about a ratio.

    Two exclusions from that count are deliberate:

    * the SELECTOR's endpoint, which gets its own ``selector_`` fields. It sends
      a different prompt prefix, so it shares no cache with the main call and
      routinely lands elsewhere; folding it in would make ``>1`` fire on nearly
      every turn and mean nothing;
    * a LOSING speculative stream, which was billed but whose tokens are not in
      ``usage``. The count describes the ratio it sits beside, not the bill —
      ``turn_cost_usd`` is where that spend is already accounted for.

    ``session_id``/``turn_id``/``round`` are what make the count answerable
    beyond this request. A turn outlives one call: its tool-continuation rounds
    are separate requests sharing a ``turn_id``, and this server is stateless by
    design, so it cannot count across them itself. Logging the key hands that job
    to the query instead of to a counter no Cloud Run instance could hold.

    Both halves of that key are needed. ``turn_id`` is a client-supplied string
    unique only WITHIN a session, so grouping on it alone merges unrelated turns
    from different callers that happened to pick the same name — and reports
    their two endpoints as a switch inside one turn.

    The query is also both fields, not just the cardinality::

        group by (session_id, turn_id):
            count(distinct endpoint) > 1  or  max(distinct_endpoints) > 1

    The second half is not redundant: a round that re-rolled from one host onto
    another logs only the ACCEPTED name, so a turn whose split happened inside a
    single round is invisible to the first half alone.
    """
    prompt = usage.prompt_tokens or 0
    # The attribution, when there is one. Absent under the open door and on any
    # unattributed path, which is why it is spread rather than defaulted: five
    # null fields on every turn would identify nothing and cost a line's width on
    # every record. `AccountContext.log_fields` is the ONLY projection read here —
    # it has nowhere to put the raw subject, the email or a credential.
    attribution = account.log_fields() if account is not None else {}
    logger.info(
        "turn_usage",
        request_id=request_id,
        # Both halves of the turn key: `turn_id` is unique only within a session.
        session_id=session_id,
        turn_id=turn_id,
        round=session_round,
        stream=stream,
        finish_reason=finish_reason,
        prompt_tokens=prompt,
        completion_tokens=usage.completion_tokens or 0,
        cached_tokens=usage.cached_tokens or 0,
        # Raw values, not the `or 0` projections above: those cannot express
        # "unreported", and the ratio is the field that has to.
        cache_ratio=cache_ratio(usage.prompt_tokens, usage.cached_tokens),
        # WHICH of the two documented shapes the count came out of — Together
        # nests it for some models and puts it at the top level for others, and
        # `providers/base.py` reads both. Logged because the two failure modes it
        # separates look identical from `cached_tokens` alone: `None` here with a
        # zero count says the host reported no reuse, while a shape that silently
        # changed under us would show up as this field flipping. It is also the
        # field the live contract suite will assert against once it exists
        # (`TODO.md`), so the production answer is already on record when it does.
        cached_tokens_source=usage.cached_tokens_source,
        # Which of the ~24 hosts produced the ratio above, and whether that
        # ratio is a single cache's or a blend of several.
        endpoint=endpoint,
        distinct_endpoints=len({name for name in observed_endpoints if name}),
        cost_usd=usage.cost,
        # The selector's own cache health, alongside the main call's. It is a
        # separate prefix on a separately-routed call, so it regresses (and is
        # fixed) independently — reading it off the main ratio would be reading
        # the wrong number.
        #
        # Read from the PROVIDER's record, not the wire projection in the
        # `runbooks` block: that one floors every count to 0 for the client, which
        # would resurrect the fabricated cold-cache 0.0 on this side too.
        # `selector_ran` is what keeps a silent call distinguishable from no call
        # once those counts are allowed to be `None`.
        selector_ran=selector_ran,
        selector_endpoint=selector_endpoint,
        selector_prompt_tokens=(
            selector_usage.prompt_tokens if selector_usage is not None else None
        ),
        selector_cached_tokens=(
            selector_usage.cached_tokens if selector_usage is not None else None
        ),
        selector_cache_ratio=(
            cache_ratio(selector_usage.prompt_tokens, selector_usage.cached_tokens)
            if selector_usage is not None
            else None
        ),
        # The figure that answers "what did this request cost me?" — every
        # upstream call the turn made, including the selector.
        turn_cost_usd=turn_cost.total if turn_cost else None,
        # Whether that total is a WHOLE one. `TurnCost` has computed this all
        # along and only the amount was ever logged, which was survivable while
        # nothing correlated a cost with an account — a short sum was merely
        # imprecise. It is not survivable now: a partial figure recorded beside a
        # plan and a subject hash is a claim about what somebody's turn cost, and
        # it reads as exact.
        turn_cost_complete=turn_cost.complete if turn_cost else None,
        selector_cost_usd=turn_cost.selector if turn_cost else None,
        # What `services/context_budget.py` estimated for this same prompt,
        # beside what the upstream actually charged for it. The pair is the whole
        # point: the budgeter runs on characters/4 against an UNVERIFIED window,
        # and this is the only place both numbers exist for one request, so it is
        # the log line that says how wrong the estimate is and in which direction.
        # `None` when the measurement failed, which must stay distinguishable
        # from a genuine zero.
        estimated_prompt_tokens=estimated_prompt_tokens,
        **attribution,
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

    ``None`` for a whole SIDE is the identity — an empty accumulator, not a call.
    That distinction is load-bearing: an all-``None`` ``ProviderUsage`` is exactly
    what a real call that reported nothing looks like, and the two must not be
    confused. Callers accumulate from ``None``, and pass ``ProviderUsage()`` only
    when a call genuinely happened and told us nothing.

    ``cost_complete`` rides along because ``add_cost`` cannot say what it lost:
    when one attempt reports and the other does not, the returned ``cost`` is a
    real number that is nonetheless short of the bill, and nothing downstream can
    recover that fact from the float. Prior partials propagate, so a three-way
    sum stays honest.
    """

    def add(a: int | None, b: int | None) -> int | None:
        if a is None:
            return b
        return a if b is None else a + b

    def add_cost(a: float | None, b: float | None) -> float | None:
        if a is None:
            return b
        return a if b is None else a + b

    if first is None:
        return second if second is not None else ProviderUsage()
    if second is None:
        return first
    return ProviderUsage(
        prompt_tokens=add(first.prompt_tokens, second.prompt_tokens),
        completion_tokens=add(first.completion_tokens, second.completion_tokens),
        total_tokens=add(first.total_tokens, second.total_tokens),
        cached_tokens=add(first.cached_tokens, second.cached_tokens),
        # A discarded attempt was still billed to the caller, so its cost is
        # part of what this turn cost them — the same reason the tokens are
        # summed rather than replaced.
        cost=add_cost(first.cost, second.cost),
        # Exactly one side reporting is the partial case: a sum that looks exact
        # and is not. Both silent is not a partial — the result is ``None``,
        # which already reads as unknown.
        cost_complete=(
            first.cost_complete
            and second.cost_complete
            and (first.cost is None) == (second.cost is None)
        ),
    )

