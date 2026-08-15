"""Resolving the turn's ACTIVE SKILL SET from a selector decision.

Which skills are live for a turn is decided here, and the rules are subtle
enough that they lived as a long comment block inside the orchestrator. They are
their own concern: pure functions over a catalog, a selector decision, and the
prior set, with no I/O and no dependency on how the turn is served.

The three rules worth knowing before editing:

* **A successful selector run is AUTHORITATIVE.** Its ids become the complete
  active set — a topic shift replaces rather than accumulates, and an empty
  result clears. A continuation round that did not run the selector, or a
  degraded one that failed open, reuses the prior set verbatim instead.
* **An authoritative run PAIRS each specific with its domain foundation**, so a
  foundation loads on the same turn as the specific that needs it rather than a
  turn late. Front-loading is deterministic here, not left to the classifier.
* **Reuse never re-pairs.** A prior set already carries its foundation, so a
  continuation round can never surprise the user with a new load.
* **A PINNED skill is admitted first, on both paths, unpaired.** It carries a need
  the server has already established rather than one the classifier inferred — the
  documentation runbook when the turn produced a ``docs_query``. It costs one slot
  and the rest of the set fills what remains.

``merge_active_skills`` stays ADDITIVE and is for the explicit ``/skills/resolve``
``load`` path only — never for ordinary selection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .catalog import Catalog
from .selector import SelectionDecision


@dataclass(frozen=True)
class ActiveSkillSet:
    """A resolved active set, plus what the ``max_active`` budget actually cost.

    ``dropped_over_cap`` is the observability half: ids a caller asked for that the
    cap refused. Unknown and duplicate ids are NOT reported there — they carry no
    capacity signal.

    One caveat if this field is ever wired to a metric or a warning: on the explicit
    ``skill.load`` path (:func:`merge_active_skills`) the candidate list is
    ``selected + prior``, so what lands here is *previously active skills evicted to
    make room*, not *requested skills the cap refused*. Same field, different meaning —
    split it before treating the two as one number.
    """

    active: list[str]
    newly_loaded: list[str]
    dropped_over_cap: list[str]


def filter_dedup_cap(
    candidate: Sequence[str], catalog: Catalog, max_active: int
) -> tuple[list[str], list[str]]:
    """Keep the first ``max_active`` known, de-duplicated ids in order.

    This is the VERBATIM path: a continuation round, a fail-open selector, and the
    explicit-load merge all reuse a set exactly as given. Foundation pairing belongs
    only to an authoritative selection (see :func:`_admit_with_domain_foundations`),
    so a mid-workflow round never surfaces a surprise load.

    Returns ``(active, dropped_over_cap)``.
    """
    active: list[str] = []
    seen: set[str] = set()
    dropped: list[str] = []
    for sid in candidate:
        if sid in seen or sid not in catalog:
            continue
        seen.add(sid)
        # Check the cap BEFORE admitting — appending first and breaking after would
        # always let one id through, even at max_active == 0. Unknown and duplicate
        # ids are filtered ABOVE this check so they never consume capacity, and a
        # refused id is RECORDED rather than breaking the scan, so the structured log
        # reports everything the cap actually cost instead of just the first casualty.
        if len(active) >= max_active:
            dropped.append(sid)
            continue
        active.append(sid)
    return active, dropped


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
    reserved-but-unclaimed foundation, returning two skills against a budget of
    three. Here no slot is ever reserved for a foundation that is then discarded.

    Every admitted skill's domain foundation is itself admitted, which holds even
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


def merge_active_skills(
    selected_ids: list[str],
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
) -> ActiveSkillSet:
    """Additive, new-first merge — the EXPLICIT-load path (``resolve`` action ``load``).

    A caller that already knows which skills to ADD on top of the prior set uses this;
    the prior set is preserved and the new ids land first. The automatic per-turn path
    uses :func:`resolve_active_skills` instead, where a selector run is authoritative.

    Foundation closure applies here TOO, via the same unit admission. This path signs a
    state token, and the reuse/degraded paths then replay that set verbatim on the
    strength of "a prior set already carries its foundation" — so an unclosed set minted
    here would propagate a foundation-less active set through every following round.
    """
    candidate = list(selected_ids) + [p for p in prior_active_ids if p not in selected_ids]
    active, dropped = _admit_with_domain_foundations(candidate, catalog, max_active)
    newly = [sid for sid in active if sid not in prior_active_ids]
    return ActiveSkillSet(active=active, newly_loaded=newly, dropped_over_cap=dropped)


def resolve_active_skills(
    decision: SelectionDecision,
    prior_active_ids: list[str],
    catalog: Catalog,
    max_active: int,
    pinned_id: str | None = None,
) -> ActiveSkillSet:
    """Per-turn active set from a selection decision (section 7.9).

    A SUCCESSFUL selector run is authoritative: its ``skill_ids`` ARE the complete
    active set for this instruction — a topic-shift replacement, so an empty list
    clears all prior skills. When the selector did NOT run (a tool-continuation round,
    ``SelectionDecision.reuse``) or failed OPEN (``degraded``), the prior active set is
    reused unchanged — a classifier hiccup never evicts an in-progress runbook. The
    paired selector prompt therefore instructs the model to re-list any active skill it
    wants to keep.

    On an authoritative run the selection is admitted as specific+foundation UNITS
    (:func:`_admit_with_domain_foundations`) so a foundation loads on the SAME turn its
    specific is selected, not a turn late, and can never be squeezed out by the cap.
    The reuse/degraded path is left untouched: a prior set resolved on a successful run
    already carries its foundation, so a continuation round never surfaces a surprise
    foundation load.

    ``pinned_id`` is a skill the SERVER has decided the turn needs, admitted unpaired
    into whatever slot the selection leaves — see the comment below the admission for
    why it goes last rather than first.
    """
    # Authority, not merely "did a call happen". A successful selector run is
    # authoritative; so is the no-task fast path, which reaches the same answer
    # without a call. Deriving authority from `ran` alone silently turned the
    # fast path's empty result into a REUSE of the prior set — the opposite of
    # the clear it advertised.
    authoritative = (decision.ran or decision.authoritative) and not decision.degraded
    candidates = [s for s in (decision.skill_ids if authoritative else prior_active_ids)
                  if s != pinned_id]

    if authoritative:
        # authoritative — replaces the prior set, admitted as foundation-paired units
        active, dropped = _admit_with_domain_foundations(candidates, catalog, max_active)
    else:
        # reuse (no run) / fail-open (degraded) — verbatim, never re-paired
        active, dropped = filter_dedup_cap(candidates, catalog, max_active)

    # The PIN — a skill whose need the SERVER already knows about, loaded without
    # asking the classifier to notice. Same argument as foundation pairing, one step
    # earlier: when the selector returns a `docs_query`, the turn IS a documentation
    # question by the classifier's own answer, so routing the matching runbook
    # through a second judgement it might get wrong is a coin flip over a fact
    # already in hand.
    #
    # It takes the LEFTOVER slot, and takes the whole set only when there is no work
    # to leave room for. Priority-0 (pin first, budget for the rest) reads more
    # faithful to "always load it" and is worse in every direction the cap is tight:
    # `max_active` is configurable and 2 is legal, where reserving a slot leaves one
    # — too few for a specific+foundation pair. The same turn would then resolve to
    # docs-only on a successful selector run and docs-plus-ORPHANED-specific on a
    # degraded one (the reuse path truncates verbatim, so it is the foundation that
    # falls off), making the coherence of the active set depend on whether a
    # classifier call happened to succeed. It would also evict an in-progress runbook
    # mid-workflow, which is precisely what the reuse path exists to prevent.
    #
    # Going last costs nothing where it matters. The case the pin exists for — the
    # standalone usage question, where the classifier correctly selects nothing — has
    # an empty remainder at every cap. The mid-workflow case fits at the shipped cap
    # of 3 (one specific + its foundation + the pin). Only a SATURATED set displaces
    # it, and there the base prompt still carries the citation rules the runbook
    # elaborates, so the turn degrades to the behaviour that shipped before the pin
    # existed rather than losing its workflow.
    #
    # Deliberately admitted WITHOUT foundation pairing. Pairing walks up the dotted
    # namespace, so `daintree.docs.*` would drag `daintree.foundation` in behind it
    # and cost two slots — and the foundation is live-state judgement a documentation
    # answer does not use. A standalone usage question should load one runbook, not
    # two.
    if pinned_id is not None and pinned_id in catalog:
        if len(active) < max_active:
            active = active + [pinned_id]
        else:
            # Recorded, not silent. A displaced pin is the one case where the turn
            # quietly reverts to pre-pin behaviour, and `dropped_over_cap` is where
            # the rest of the system already looks to see what the cap cost it.
            dropped = [*dropped, pinned_id]
    newly = [sid for sid in active if sid not in prior_active_ids]
    return ActiveSkillSet(active=active, newly_loaded=newly, dropped_over_cap=dropped)

