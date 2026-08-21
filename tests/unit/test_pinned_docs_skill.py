"""The server-pinned documentation runbook.

`daintree.docs.answer-usage-question` is loaded off the turn's `docs_query`
rather than chosen by the classifier. By the time it matters the selector has
ALREADY answered "is this a documentation question", so routing the matching
runbook through a SECOND judgement over the same evidence is a coin flip about a
fact in hand — the same argument as foundation pairing, one step earlier.

Two halves, and both are needed. The skill is PINNED by
`resolve_active_skills`, and HIDDEN from `Catalog.selector_metadata` so the
classifier cannot also pick it — otherwise there are two decisions that can
disagree, and one of them costs selector prompt bytes on every call.

This file is where its coverage lives. The routing corpus cannot cover a skill
the selector cannot see, so `bench coverage` exempts it; a deterministic rule
belongs in a deterministic test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import respond_body
from fakes import make_result, selection_result
from pydantic import SecretStr

from daintree_assistant_server.config import DOCS_SKILL_ID
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.skills.active_set import resolve_active_skills
from daintree_assistant_server.skills.fast_path import can_skip_selection
from daintree_assistant_server.skills.selector import SelectionDecision, parse_select_args
from daintree_assistant_server.state.codec import StateCodec

FOUNDATION = "daintree.foundation"
WORKFLOW = "daintree.workflow.prep-branch-for-review"
OPERATIONAL = "daintree.terminals.rename"


def _authoritative(skill_ids: list[str]) -> SelectionDecision:
    return SelectionDecision(skill_ids=skill_ids, ran=True, degraded=False)


def _prompt_text(request: Any) -> str:
    """Every message of an upstream request, flattened — what the model was shown."""
    return json.dumps(
        [m if isinstance(m, dict) else m.model_dump() for m in request.messages]
    )


def test_the_pinned_skill_is_in_the_catalog_but_not_the_selector_menu(catalog: Any) -> None:
    """Both halves asserted together. Visible, the classifier could also pick it —
    a second decision that can disagree with the first. Absent from the catalog,
    the pin would be a silent no-op forever."""
    assert DOCS_SKILL_ID in catalog, "the pinned skill is missing from the catalog"
    assert DOCS_SKILL_ID not in {m["id"] for m in catalog.selector_metadata()}


def test_it_loads_when_the_selector_chose_nothing(catalog: Any) -> None:
    """The standalone documentation question. The classifier is RIGHT to return an
    empty set — there is no workflow here — and the runbook still loads."""
    result = resolve_active_skills(
        _authoritative([]), [], catalog, 3, pinned_id=DOCS_SKILL_ID
    )
    assert result.active == [DOCS_SKILL_ID]
    assert result.newly_loaded == [DOCS_SKILL_ID]


def test_it_does_not_drag_in_the_domain_foundation(catalog: Any) -> None:
    """Foundation pairing walks UP the dotted namespace, so `daintree.docs.*` would
    pull `daintree.foundation` in behind it and cost two of three slots — for
    live-state judgement a documentation answer never uses. The pin is admitted
    unpaired for exactly this reason.

    The first assertion is what stops this being a restatement of the test above:
    without it, an id with no foundation at all would pass identically and the
    exemption would be untested. Pairing genuinely APPLIES to this id — it is the
    admission path that is bypassed.
    """
    assert catalog.domain_foundation(DOCS_SKILL_ID) == FOUNDATION, (
        "precondition: pairing would otherwise fire for this id"
    )
    result = resolve_active_skills(
        _authoritative([]), [], catalog, 3, pinned_id=DOCS_SKILL_ID
    )
    assert result.active == [DOCS_SKILL_ID], "the pin pulled a foundation in with it"


# ---------------------------------------------------------------------------
# The pin NEVER displaces work, and never breaks an atomic unit.
#
# `max_active_skills` is configurable and 2 is a legal value. Reserving the pin's
# slot first would leave one — too few for a specific+foundation pair — so the
# SAME turn would resolve to docs-only when the classifier succeeded and
# docs-plus-ORPHANED-specific when it degraded (the reuse path truncates verbatim,
# so it is the foundation that falls off). Whether the active set was coherent
# would then depend on whether a model call happened to succeed.
# ---------------------------------------------------------------------------


def test_at_a_two_cap_a_paired_unit_beats_the_pin(catalog: Any) -> None:
    """Authoritative. The workflow keeps its foundation; the pin is what gives way.

    That is the right loser: the base prompt carries the citation rules the runbook
    elaborates, so dropping it degrades to the behaviour that shipped before the pin
    existed — while a foundation-less specific is a state nothing else in the system
    can produce."""
    result = resolve_active_skills(
        _authoritative([WORKFLOW]), [], catalog, 2, pinned_id=DOCS_SKILL_ID
    )
    assert set(result.active) == {WORKFLOW, FOUNDATION}, (
        f"the specific lost its foundation to the pin; active={result.active}"
    )
    assert DOCS_SKILL_ID not in result.active


def test_at_a_two_cap_a_reuse_round_keeps_its_pair_too(catalog: Any) -> None:
    """Degraded/reuse must reach the SAME answer as the authoritative path above.

    This is the half that made the defect visible: a selector hiccup would otherwise
    flip a turn from docs-only to docs-plus-orphan, so whether the active set was
    coherent depended on whether a classifier call happened to succeed."""
    result = resolve_active_skills(
        SelectionDecision.reuse([WORKFLOW, FOUNDATION]),
        [WORKFLOW, FOUNDATION],
        catalog,
        2,
        pinned_id=DOCS_SKILL_ID,
    )
    assert set(result.active) == {WORKFLOW, FOUNDATION}
    assert DOCS_SKILL_ID not in result.active


def test_at_a_two_cap_a_standalone_docs_question_still_pins(catalog: Any) -> None:
    """The case the pin exists for is unaffected at any cap. Nothing competes for
    the budget, so the leftover slot is the whole budget."""
    result = resolve_active_skills(
        _authoritative([]), [], catalog, 2, pinned_id=DOCS_SKILL_ID
    )
    assert result.active == [DOCS_SKILL_ID]


def test_at_a_one_cap_the_pin_fills_a_set_that_would_be_empty(catalog: Any) -> None:
    """One slot, and a unit that cannot fit in it. Going last is not the same as
    yielding: the specific is dropped by the cap either way, so refusing to pin here
    would trade a loaded runbook for nothing at all."""
    result = resolve_active_skills(
        _authoritative([OPERATIONAL]), [], catalog, 1, pinned_id=DOCS_SKILL_ID
    )
    assert result.active == [DOCS_SKILL_ID]


def test_a_saturated_selection_displaces_the_pin(catalog: Any) -> None:
    """No leftover slot, no pin. Two specifics sharing a foundation fill the cap,
    and the work keeps its runbooks — the citation rules survive in the base
    prompt, which is where they live for exactly this reason.

    Reserving a slot for the pin instead was tried and reverted. It cost more than
    it looked: with the top specific and its foundation taking both remaining slots,
    what a reservation drops at the shipped cap is the classifier's SECOND pick, not
    a marginal third. And it does not even buy mid-workflow safety — the selector
    instructs a mid-workflow documentation question to RE-LIST its active ids, so
    such a turn is authoritative and a reservation evicts the in-flight runbook on
    exactly the path that matters. The pin yielding here is the cheap loss: the
    retrieved pages still reach the model in the volatile tail, and the base prompt
    still carries the citation invariant the runbook elaborates."""
    result = resolve_active_skills(
        _authoritative([WORKFLOW, OPERATIONAL]), [], catalog, 3, pinned_id=DOCS_SKILL_ID
    )
    assert set(result.active) == {WORKFLOW, OPERATIONAL, FOUNDATION}
    assert DOCS_SKILL_ID not in result.active
    assert DOCS_SKILL_ID in result.dropped_over_cap, (
        "a displaced pin reverts the turn to pre-pin behaviour and must not do it silently"
    )


def test_it_takes_the_slot_the_selection_leaves(catalog: Any) -> None:
    """A documentation question asked MID-WORKFLOW: the workflow keeps its runbook,
    its foundation pairing survives, and the docs runbook arrives too — the shipped
    cap of 3 fits exactly one specific, its foundation, and the pin."""
    result = resolve_active_skills(
        _authoritative([WORKFLOW]), [WORKFLOW], catalog, 3, pinned_id=DOCS_SKILL_ID
    )
    assert DOCS_SKILL_ID in result.active
    assert WORKFLOW in result.active
    assert FOUNDATION in result.active, "the specific lost its foundation pairing"


def test_it_applies_on_a_reuse_round_too(catalog: Any) -> None:
    """A continuation round reuses the prior set verbatim and never re-pairs — but
    the pin still applies, because the query is carried across the round and the
    pages are rebuilt into the volatile tail every time."""
    result = resolve_active_skills(
        SelectionDecision.reuse([OPERATIONAL]), [OPERATIONAL], catalog, 3,
        pinned_id=DOCS_SKILL_ID,
    )
    assert DOCS_SKILL_ID in result.active
    assert OPERATIONAL in result.active


def test_a_saturated_degraded_round_also_displaces_the_pin(catalog: Any) -> None:
    """Fail-open is `ran=True, degraded=True`, not a reuse decision. Guarding pin
    admission on `ran` instead of authority would silently treat a degraded round as
    a fresh selection; this pins the behaviour to the same leftover rule."""
    prior = [WORKFLOW, FOUNDATION, OPERATIONAL]
    result = resolve_active_skills(
        SelectionDecision.degraded_open(["stale head"]),
        prior,
        catalog,
        3,
        pinned_id=DOCS_SKILL_ID,
    )
    assert set(result.active) == set(prior), "a degraded round evicted live work"
    assert DOCS_SKILL_ID in result.dropped_over_cap


def test_a_pin_already_in_a_saturated_prior_set_is_kept_not_dropped(catalog: Any) -> None:
    """The pin is filtered out of the candidates and re-appended, so a SATURATED prior
    set that already contains it must not lose it to its own cap — and must not
    re-announce it. The singleton case alone would not catch an off-by-one here."""
    prior = [DOCS_SKILL_ID, WORKFLOW, FOUNDATION]
    result = resolve_active_skills(
        SelectionDecision.reuse(prior), prior, catalog, 3, pinned_id=DOCS_SKILL_ID
    )
    assert set(result.active) == set(prior)
    assert result.newly_loaded == [], "an already-active pin was re-announced"
    assert DOCS_SKILL_ID not in result.dropped_over_cap


def test_an_unknown_pin_never_costs_a_slot(catalog: Any) -> None:
    """A pinned id absent from the catalog must not reserve, consume, or leak a slot:
    the selection keeps the full budget it would have had with no pin at all."""
    with_pin = resolve_active_skills(
        _authoritative([WORKFLOW, OPERATIONAL]), [], catalog, 3, pinned_id="daintree.nope"
    )
    without = resolve_active_skills(_authoritative([WORKFLOW, OPERATIONAL]), [], catalog, 3)
    assert with_pin.active == without.active
    assert "daintree.nope" not in with_pin.active + with_pin.dropped_over_cap


def test_it_is_not_reported_as_newly_loaded_twice(catalog: Any) -> None:
    """`newly_loaded` drives the CLI's skill-load prelude, so re-announcing an
    already-active skill on every round of a turn would show the user the same
    load repeatedly."""
    result = resolve_active_skills(
        SelectionDecision.reuse([DOCS_SKILL_ID]), [DOCS_SKILL_ID], catalog, 3,
        pinned_id=DOCS_SKILL_ID,
    )
    assert result.active == [DOCS_SKILL_ID]
    assert result.newly_loaded == []


def test_no_pin_leaves_the_active_set_exactly_as_before(catalog: Any) -> None:
    """The negative control, and the common case: an operational turn produces no
    docs query, so nothing about resolution changes."""
    result = resolve_active_skills(_authoritative([OPERATIONAL]), [], catalog, 3)
    assert DOCS_SKILL_ID not in result.active
    assert result.active[0] == OPERATIONAL


def test_an_unknown_pin_is_a_silent_no_op(catalog: Any) -> None:
    """Deleting the skill file must degrade to the previous behaviour rather than
    break every documentation turn."""
    result = resolve_active_skills(
        _authoritative([OPERATIONAL]), [], catalog, 3, pinned_id="daintree.nope.absent"
    )
    assert result.active[0] == OPERATIONAL
    assert len(result.active) >= 1


def test_a_zero_cap_admits_nothing_including_the_pin(catalog: Any) -> None:
    """The cap is the cap. A pin that ignored it would make `max_active_skills: 0`
    mean something other than what it says."""
    result = resolve_active_skills(
        _authoritative([OPERATIONAL]), [], catalog, 0, pinned_id=DOCS_SKILL_ID
    )
    assert result.active == []


# ---------------------------------------------------------------------------
# End to end, through the real orchestrator.
#
# The tests above drive `resolve_active_skills` directly. That is the wrong unit
# to stop at: the ORDERING is the part that was wrong before — `docs_query` was
# computed AFTER the active set, so a pin keyed on it could not have worked. These
# assert the wiring, not the rule.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_docs_question_loads_the_runbook_through_the_orchestrator(
    orchestrator: Any, fake_provider: Any
) -> None:
    """A turn the selector classifies as a documentation question loads the docs
    runbook, even though the classifier itself selected no skill."""
    fake_provider.selection_result = selection_result([], docs_query="what is a watcher")
    fake_provider.complete_result = make_result(content="A watcher is …")

    body = RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "what is a watcher?"}])
    )
    outcome = await orchestrator.complete(
        body, upstream_model="m", api_key=SecretStr("k"), request_id="req-docs"
    )
    active = [s["id"] for s in outcome.body["skills"]["active"]]
    assert DOCS_SKILL_ID in active, f"the docs runbook was not pinned; active={active}"


@pytest.mark.asyncio
async def test_an_operational_turn_does_not_load_the_docs_runbook(
    orchestrator: Any, fake_provider: Any
) -> None:
    """The negative control. Most turns are not documentation questions, and a
    runbook loaded on all of them would be ~4 KB of prompt for nothing."""
    fake_provider.selection_result = selection_result([OPERATIONAL], docs_query=None)
    fake_provider.complete_result = make_result(content="Renamed.")

    body = RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "rename my terminals"}])
    )
    outcome = await orchestrator.complete(
        body, upstream_model="m", api_key=SecretStr("k"), request_id="req-op"
    )
    active = [s["id"] for s in outcome.body["skills"]["active"]]
    assert DOCS_SKILL_ID not in active, f"the docs runbook loaded on an operational turn; {active}"
    assert OPERATIONAL in active


@pytest.mark.asyncio
async def test_the_body_reaches_the_main_prompt(
    orchestrator: Any, fake_provider: Any
) -> None:
    """The metadata block is not the deliverable — the RUNBOOK is. A pin that
    appeared in `skills.active` without its body reaching the upstream request
    would look right in every response assertion and change nothing about the
    answer."""
    fake_provider.selection_result = selection_result([], docs_query="what is a watcher")
    fake_provider.complete_result = make_result(content="A watcher is …")

    body = RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "what is a watcher?"}])
    )
    await orchestrator.complete(
        body, upstream_model="m", api_key=SecretStr("k"), request_id="req-body"
    )
    sent = _prompt_text(fake_provider.requests[0])
    assert "## Docs" in sent, "the pinned runbook's body never reached the main prompt"


@pytest.mark.asyncio
async def test_the_selector_is_never_told_a_pinned_skill_is_active(
    orchestrator: Any, fake_provider: Any
) -> None:
    """The prompt would otherwise contradict itself.

    Its step 3 says re-list an active id or it is EVICTED; its closing checklist
    says never invent an id and choose only from the catalog below. Naming a hidden
    skill on the active line instructs the classifier to do both at once.

    Driven through the ORCHESTRATOR, not `Catalog.selectable`, because the filter
    existing is not the point — the real call site using it is. Reverting
    `SkillSelector.select` to `filter_known` passes any assertion made against the
    projection in isolation.
    """
    fake_provider.selection_result = selection_result([OPERATIONAL], docs_query=None)
    fake_provider.complete_result = make_result(content="Renamed.")

    first = RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "what is a watcher?"}])
    )
    fake_provider.selection_result = selection_result([], docs_query="what is a watcher")
    outcome = await orchestrator.complete(
        first, upstream_model="m", api_key=SecretStr("k"), request_id="req-1"
    )
    assert DOCS_SKILL_ID in [s["id"] for s in outcome.body["skills"]["active"]]

    # A NEW instruction, carrying the prior set forward on the signed token. The
    # selector reruns, and the active-skills line it is shown is built from that set.
    fake_provider.selection_result = selection_result([OPERATIONAL], docs_query=None)
    second = RespondRequest.model_validate(
        respond_body(
            [{"role": "user", "content": "rename my terminals"}],
            turn_id="t2",
            state=outcome.body["state"],
        )
    )
    await orchestrator.complete(
        second, upstream_model="m", api_key=SecretStr("k"), request_id="req-2"
    )
    prompt = _prompt_text(fake_provider.selection_requests[-1])
    assert DOCS_SKILL_ID not in prompt, "the selector was shown a hidden skill id"


@pytest.mark.asyncio
async def test_the_pin_survives_a_tool_continuation_round(
    orchestrator: Any, fake_provider: Any
) -> None:
    """The state token is the only thing carrying the docs query across a round, so
    this is where the runbook would silently vanish: the selector does not rerun on
    a continuation, and a decision that did not run has no `docs_query` of its own."""
    fake_provider.selection_result = selection_result([], docs_query="what is a watcher")
    fake_provider.complete_result = make_result(content="A watcher is …")

    first = RespondRequest.model_validate(
        respond_body([{"role": "user", "content": "what is a watcher?"}])
    )
    outcome = await orchestrator.complete(
        first, upstream_model="m", api_key=SecretStr("k"), request_id="req-1"
    )

    # Same turn, next round — the continuation the selector deliberately skips.
    fake_provider.selection_result = None
    second = RespondRequest.model_validate(
        respond_body(
            [{"role": "user", "content": "what is a watcher?"}],
            round=1,
            state=outcome.body["state"],
        )
    )
    again = await orchestrator.complete(
        second, upstream_model="m", api_key=SecretStr("k"), request_id="req-1b"
    )
    active = [s["id"] for s in again.body["skills"]["active"]]
    assert DOCS_SKILL_ID in active, f"the runbook was lost on continuation; active={active}"
    assert again.body["skills"]["newly_loaded"] == [], "re-announced an already-loaded skill"


def test_a_pinned_skill_still_round_trips_through_a_real_state_token(catalog: Any) -> None:
    """Hidden from the CLASSIFIER, not from the system. The active set is SIGNED into
    the state token and replayed on the next round, so the hidden id has to survive
    an actual issue/verify cycle — a `filter_known` assertion alone never exercises
    the codec that carries it."""
    codec = StateCodec(current_key=b"k" * 16, current_kid="k1", salt=b"salt", ttl_seconds=3600)
    token = codec.issue(
        session_id="s",
        active_skills=[DOCS_SKILL_ID, OPERATIONAL],
        catalog_revision=catalog.revision,
        turn_id="t",
        instruction_revision=0,
        docs_query="what is a watcher",
    )
    verification = codec.verify(token, session_id="s")
    assert verification.valid and verification.payload is not None
    assert verification.payload.active_ids() == [DOCS_SKILL_ID, OPERATIONAL]
    assert verification.payload.docs_query == "what is a watcher"
    # ...and the catalog still resolves it to a real body on the way back in.
    assert catalog.filter_known([DOCS_SKILL_ID]) == [DOCS_SKILL_ID]
    assert [s.id for s in catalog.get_many([DOCS_SKILL_ID])] == [DOCS_SKILL_ID]


# ---------------------------------------------------------------------------
# Hidden from the prompt is not hidden from the model's OUTPUT.
# ---------------------------------------------------------------------------


def test_the_parser_rejects_a_hidden_id(catalog: Any) -> None:
    """The id is a public string. It appears in `/skills`, and it can appear in the
    conversation the classifier is reading — so "absent from the catalog block" is
    not a guarantee it never comes back. Naming it would load a pinned runbook,
    auto-paired with its foundation, on a turn the server never pinned it for.

    Returning it ALONE is treated as malformed (the model named skills and none
    resolved) — the caller repairs once, then fails open to the prior set."""
    raw = f'{{"skill_ids": ["{DOCS_SKILL_ID}"], "confidence": 0.9}}'
    assert parse_select_args(raw, catalog, 3) is None


def test_the_parser_keeps_the_valid_ids_beside_a_hidden_one(catalog: Any) -> None:
    """A partially-hallucinated answer is still usable. Dropping the hidden id must
    not cost the turn the real selection alongside it."""
    raw = f'{{"skill_ids": ["{DOCS_SKILL_ID}", "{OPERATIONAL}"], "confidence": 0.9}}'
    parsed = parse_select_args(raw, catalog, 3)
    assert parsed is not None
    assert parsed[0] == [OPERATIONAL]


# ---------------------------------------------------------------------------
# The no-task fast path.
# ---------------------------------------------------------------------------


def test_the_fast_path_survives_a_finished_docs_answer() -> None:
    """"Thanks" after a standalone documentation answer is the cold-opening case
    wearing an active set. The pin is not a workflow and nothing about it is in
    progress, so paying for a selector call to conclude what this function already
    knows is the exact cost the fast path exists to avoid."""
    body = RespondRequest.model_validate(respond_body([{"role": "user", "content": "thanks!"}]))
    assert can_skip_selection(body, prior_active_ids=[DOCS_SKILL_ID]) is True
    assert can_skip_selection(body, prior_active_ids=[DOCS_SKILL_ID, WORKFLOW]) is False, (
        "a real workflow beside the pin still disqualifies the turn"
    )


# ---------------------------------------------------------------------------
# The two pin tiers together.
#
# `selection.pinned_skill_ids` (issue #54) is the caller's own pin, and it is the
# MIRROR of this one: admitted first and foundation-paired, where the docs pin is
# admitted last and bare. Both can be live on the same turn, so the tiers need a
# regression that holds them apart — the risk is not that either rule is wrong but
# that one quietly starts applying to the other's ids.
# ---------------------------------------------------------------------------


def test_a_caller_pin_outranks_the_docs_pin(catalog: Any) -> None:
    """A saturated caller pin displaces the docs runbook, not the reverse. Two
    slots go to the pinned specific and its foundation, the third to the
    selection, and the docs pin takes the leftover slot there is none of."""
    result = resolve_active_skills(
        _authoritative([OPERATIONAL]),
        [],
        catalog,
        3,
        pinned_id=DOCS_SKILL_ID,
        client_pinned_ids=[WORKFLOW],
    )
    assert result.active[0] == WORKFLOW, f"the caller pin lost its priority; {result.active}"
    assert FOUNDATION in result.active, "the caller pin was admitted unpaired"
    assert DOCS_SKILL_ID not in result.active
    assert DOCS_SKILL_ID in result.dropped_over_cap


def test_the_docs_pin_still_takes_the_leftover_slot_beside_a_caller_pin(catalog: Any) -> None:
    """The other direction: room for both means both. A caller pin must not
    suppress the docs pin, only outrank it when the budget is contested."""
    result = resolve_active_skills(
        _authoritative([]),
        [],
        catalog,
        3,
        pinned_id=DOCS_SKILL_ID,
        client_pinned_ids=[WORKFLOW],
    )
    assert result.active[:2] == [WORKFLOW, FOUNDATION]
    assert result.active[2] == DOCS_SKILL_ID, (
        f"the docs pin lost the slot the caller pin left it; active={result.active}"
    )


def test_caller_pinning_the_docs_skill_admits_it_once(catalog: Any) -> None:
    """The id both tiers can claim. A developer working ON the docs runbook pins it
    by name on a turn that also produced a `docs_query`; it must be admitted once.
    It is PAIRED here — the automatic pin skips pairing because nobody asked for
    the foundation, which stops being true the moment somebody names the id."""
    result = resolve_active_skills(
        _authoritative([]),
        [],
        catalog,
        3,
        pinned_id=DOCS_SKILL_ID,
        client_pinned_ids=[DOCS_SKILL_ID],
    )
    assert result.active.count(DOCS_SKILL_ID) == 1, f"admitted twice: {result.active}"
    assert result.active == [DOCS_SKILL_ID, FOUNDATION]
    assert DOCS_SKILL_ID not in result.dropped_over_cap


def test_a_caller_pin_can_load_a_skill_the_selector_cannot_see(catalog: Any) -> None:
    """The docs runbook is hidden from `selector_metadata` so the classifier cannot
    also pick it. Pinning is the explicit act that bypasses selection, so hidden
    must not mean unpinnable — otherwise the one skill a developer most needs a
    direct handle on is the one they cannot get."""
    assert DOCS_SKILL_ID not in {m["id"] for m in catalog.selector_metadata()}
    result = resolve_active_skills(
        _authoritative([]), [], catalog, 3, client_pinned_ids=[DOCS_SKILL_ID]
    )
    assert DOCS_SKILL_ID in result.active
