"""Foundation-paired admission of a ranked runbook selection.

The serving path no longer resolves an active set — the MCP search returns a
ranking and the caller decides what to load. These rules survive because the
training corpus and the routing benchmark replay multi-turn conversations, and
the set a conversation carries forward is what feeds the ``[loaded: …]`` prefix
the classifier was trained on:

* **Each specific is admitted with its domain foundation** as one atomic unit,
  in rank order, only while the whole unit fits under the cap.
* **``merge_active_runbooks`` is additive**: new ids first, then the prior set.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .catalog import Catalog


@dataclass(frozen=True)
class ActiveRunbookSet:
    """A resolved active set, plus what the ``max_active`` budget actually cost.

    ``dropped_over_cap`` is the observability half: ids a caller asked for that the
    cap refused. Unknown and duplicate ids are NOT reported there — they carry no
    capacity signal.

    One caveat if this field is ever wired to a metric or a warning: on the explicit
    ``runbook.load`` path (:func:`merge_active_runbooks`) the candidate list is
    ``selected + prior``, so what lands here is *previously active runbooks evicted to
    make room*, not *requested runbooks the cap refused*. Same field, different meaning —
    split it before treating the two as one number.
    """

    active: list[str]
    newly_loaded: list[str]
    dropped_over_cap: list[str]


def _admit_with_domain_foundations(
    candidate: Sequence[str], catalog: Catalog, max_active: int
) -> tuple[list[str], list[str]]:
    """Budget-aware ATOMIC-UNIT admission of an authoritative selection.

    Pairing used to append every missing domain foundation and then truncate to
    ``max_active`` — which, at full capacity, was deterministically DEAD CODE: the
    selector already truncates its own output to ``max_active``, so whenever the
    classifier returned a full set without the foundation, the appended foundation
    was GUARANTEED to be the first id the cap discarded. Pairing was not "outranked
    by explicit picks"; it simply never ran when it mattered, leaving front-loading
    model-dependent — the exact opposite of why it exists. Worse, a new full
    selection silently EVICTED an already-active foundation mid-workflow (and, per
    the assembler's cache rules, forced a full tools+conversation re-encode). The
    selector prompt's own tie-break puts the domain foundation ABOVE the marginal
    specific; this now matches it.

    The fix is unit admission, not slot reservation. Each selected specific is
    walked in rank order and admitted as one atomic unit — itself plus its domain
    foundation when that foundation is not already admitted — and only while the
    remaining budget fits the WHOLE unit. A unit that does not fit is skipped and
    the next specific is tried: a later specific whose foundation is already
    admitted costs one slot and can still fill the gap. Naively reserving a slot
    for the foundation is wrong in both directions — with three cross-domain
    specifics and a cap of three it evicts the third specific AND then drops the
    reserved-but-unclaimed foundation, returning two runbooks against a budget of
    three. Here no slot is ever reserved for a foundation that is then discarded.

    Every admitted runbook's domain foundation is itself admitted, which holds even
    when the catalog resolves a foundation for EVERY id (a prefix walk-up).

    The policy is RANK RETENTION, not maximum cardinality, and the two can disagree
    once a catalog holds more than one foundation. With foundations ``a.base`` and
    ``b.base``, a selection of ``[a.one, b.one, b.two]`` and a cap of 3, this admits
    ``[a.one, a.base]`` and stops — both ``b.*`` units cost two at the moment they are
    considered. ``[b.one, b.base, b.two]`` is closed and fills all three slots, but
    reaching it means dropping the classifier's TOP-ranked pick to fit two lower-ranked
    ones. The selector returns best-match-first, so its first id is the strongest signal
    on the turn and keeping it wins. Deliberate — do not "fix" this with a subset search.

    Returns ``(active, dropped_over_cap)``; unknown and duplicate ids are dropped
    silently, as before.
    """
    active: list[str] = []
    admitted: set[str] = set()
    dropped: list[str] = []
    considered: set[str] = set()
    for sid in candidate:
        if sid in considered or sid not in catalog:
            continue
        considered.add(sid)
        if sid in admitted:
            continue
        unit = [sid]
        foundation_id = catalog.domain_foundation(sid)
        if (
            foundation_id is not None
            and foundation_id != sid
            and foundation_id not in admitted
            and foundation_id in catalog
        ):
            unit.append(foundation_id)
        if len(active) + len(unit) > max_active:
            # The unit does not fit. Skip it (never a partial admission) and try
            # the next specific, which may be cheaper because its foundation is
            # already in.
            dropped.append(sid)
            continue
        for uid in unit:
            admitted.add(uid)
            active.append(uid)
    return active, dropped


def merge_active_runbooks(
    selected_ids: list[str],
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> ActiveRunbookSet:
    """Additive, new-first merge: the new ids land first, then the prior set, with
    each admitted as a foundation-closed unit under ``max_active``.

    The training corpus uses this to carry a conversation's active set forward
    between turns, so it must match the set the classifier was trained to see.
    """
    candidate = list(selected_ids) + [p for p in prior_active_ids if p not in selected_ids]
    active, dropped = _admit_with_domain_foundations(candidate, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return ActiveRunbookSet(active=active, newly_loaded=newly, dropped_over_cap=dropped)
