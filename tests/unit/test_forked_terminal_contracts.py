"""The fork plan contract, and the schema the model is shown for it.

Two things are pinned here. The first is the plan's own strictness: the backend
re-imposes agent, worktree, mode and watcher over whatever an expander returned,
so a plan that is vague about any of them is a plan the backend cannot enforce,
and it must be refused rather than defaulted. The second is that the ADVERTISED
schema and the ACCEPTED model describe the same thing — the schema is written
out by hand (flat, stable bytes for the tool region) and would otherwise be free
to drift from the Pydantic model that actually validates the call.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.forked_terminals import (
    FORK_BRANCH_COUNT,
    MAX_AGENT_ID_CHARS,
    MAX_WORKTREE_ID_CHARS,
    ForkBranch,
    ForkDefaults,
    ForkSpawnPlan,
    effective_agent_id,
    effective_controls,
    effective_mode,
    effective_watch,
)
from daintree_assistant_server.services.forked_terminal_calls import (
    FORK_TOOL_DEFINITION,
    FORK_TOOL_WIRE_NAME,
    RESERVED_MESSAGE_NAMES,
    SPAWN_TOOL_DOTTED_NAME,
    SPAWN_TOOL_WIRE_NAME,
    canonical_json,
    find_tool,
    is_reserved_message_name,
)


def _branch(bid: str, **overrides: Any) -> dict[str, Any]:
    branch: dict[str, Any] = {
        "id": bid,
        "worktreeId": f"wt_{bid}",
        "brief": f"Own the {bid} scope; change nothing else; tests pass.",
    }
    branch.update(overrides)
    return branch


def _plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "sharedGoal": "Prepare the application for release.",
        "sharedConstraints": ["Run the relevant tests."],
        "defaults": {"agentId": "claude", "mode": "edit", "watch": True},
        "branches": [
            _branch("backend"),
            _branch("frontend"),
            _branch("tests"),
            _branch("performance"),
        ],
    }
    plan.update(overrides)
    return plan


# ---------------------------------------------------------------------------
# Shape.
# ---------------------------------------------------------------------------


def test_a_well_formed_four_branch_plan_is_accepted() -> None:
    plan = ForkSpawnPlan.model_validate(_plan())
    assert [b.id for b in plan.branches] == ["backend", "frontend", "tests", "performance"]


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5, 8])
def test_only_exactly_four_branches_are_accepted(count: int) -> None:
    branches = [_branch(f"b{i}") for i in range(count)]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_the_branch_count_is_fixed_at_four() -> None:
    # Not a default and not a minimum. A variable count is a later decision, and
    # the expander prompt, the fallback templates and the latency argument are
    # all written for this number.
    assert FORK_BRANCH_COUNT == 4


def test_duplicate_branch_ids_rejected() -> None:
    branches = [_branch("backend"), _branch("backend"), _branch("tests"), _branch("perf")]
    with pytest.raises(ValidationError, match="unique"):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_unknown_fields_rejected_at_every_level() -> None:
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(extra="nope"))
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(
            _plan(defaults={"agentId": "claude", "mode": "edit", "extra": 1})
        )
    branches = [_branch("backend", extra=1), _branch("b2"), _branch("b3"), _branch("b4")]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


@pytest.mark.parametrize("bad", ["Backend", "9lives", "has space", "a" * 49, "", "dot.ted"])
def test_branch_ids_are_slugs(bad: str) -> None:
    branches = [_branch("ok1"), _branch("ok2"), _branch("ok3"), dict(_branch("x"), id=bad)]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_caps_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(sharedGoal="short"))
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(sharedGoal="x" * 4_001))
    branches = [_branch("b1"), _branch("b2"), _branch("b3"), _branch("b4", brief="x" * 2_001)]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(sharedConstraints=["c"] * 17))


def test_the_plan_is_frozen() -> None:
    plan = ForkSpawnPlan.model_validate(_plan())
    with pytest.raises(ValidationError):
        plan.shared_goal = "something else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Semantics — the control plane the backend has to be able to enforce.
# ---------------------------------------------------------------------------


def test_a_branch_with_no_effective_agent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="agentId"):
        ForkSpawnPlan.model_validate(_plan(defaults={"mode": "edit"}))


def test_a_branch_with_no_effective_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="mode"):
        ForkSpawnPlan.model_validate(_plan(defaults={"agentId": "claude"}))


def test_a_per_branch_override_satisfies_a_missing_default() -> None:
    branches = [
        _branch(f"b{i}", agentId="codex", mode="edit", watch=True) for i in range(4)
    ]
    plan = ForkSpawnPlan.model_validate(_plan(defaults={}, branches=branches))
    assert effective_agent_id(plan, plan.branches[0]) == "codex"


def test_two_branches_may_not_share_a_worktree() -> None:
    branches = [
        _branch("backend", worktreeId="wt_shared"),
        _branch("frontend", worktreeId="wt_shared"),
        _branch("tests"),
        _branch("perf"),
    ]
    with pytest.raises(ValidationError, match="own worktree"):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_two_explore_branches_may_not_share_a_worktree_either() -> None:
    # `mode: "explore"` is a line in the AGENT'S PROMPT, not filesystem
    # isolation — an explore agent shares the checkout and can still write to
    # it. A carve-out for read-only branches would be a rule the runtime does
    # not enforce.
    branches = [
        _branch("a", worktreeId="wt_shared", mode="explore"),
        _branch("b", worktreeId="wt_shared", mode="explore"),
        _branch("c", mode="explore"),
        _branch("d", mode="explore"),
    ]
    with pytest.raises(ValidationError, match="own worktree"):
        ForkSpawnPlan.model_validate(
            _plan(defaults={"agentId": "claude", "watch": False}, branches=branches)
        )


def test_an_edit_branch_may_not_share_a_worktree_with_an_explore_branch() -> None:
    branches = [
        _branch("writer", worktreeId="wt_shared", mode="edit"),
        _branch("reader", worktreeId="wt_shared", mode="explore"),
        _branch("c"),
        _branch("d"),
    ]
    with pytest.raises(ValidationError, match="own worktree"):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_worktree_ids_differing_only_by_case_collide() -> None:
    # The collision being prevented is two agents in one DIRECTORY, and the
    # common development filesystem is case-insensitive. Over-rejecting on a
    # case-sensitive one costs a refused plan; under-rejecting costs a corrupted
    # checkout.
    branches = [
        _branch("a", worktreeId="/Repo/Main"),
        _branch("b", worktreeId="/repo/main"),
        _branch("c"),
        _branch("d"),
    ]
    with pytest.raises(ValidationError, match="own worktree"):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


# ---------------------------------------------------------------------------
# Effective controls — what the backend overwrites the expander's answer with.
# ---------------------------------------------------------------------------


def test_effective_controls_carry_every_authority_field() -> None:
    plan = ForkSpawnPlan.model_validate(_plan())
    controls = effective_controls(plan, plan.branches[0])
    assert controls == {
        "agentId": "claude",
        "worktreeId": "wt_backend",
        "mode": "edit",
        "watch": True,
    }


def test_a_branch_overrides_the_plan_defaults() -> None:
    branches = [
        _branch("backend", agentId="codex", mode="explore", watch=False),
        _branch("b2"),
        _branch("b3"),
        _branch("b4"),
    ]
    plan = ForkSpawnPlan.model_validate(_plan(branches=branches))
    assert effective_controls(plan, plan.branches[0]) == {
        "agentId": "codex",
        "worktreeId": "wt_backend",
        "mode": "explore",
        "watch": False,
    }


def test_a_branch_with_no_effective_watch_is_rejected() -> None:
    # The tempting default. `False` is the safe direction for authority, which
    # is exactly why defaulting would go unnoticed: it converts "the model
    # forgot" into "the fleet runs unsupervised", a product failure that raises
    # no alarm anywhere. The supervision decision is the primary model's.
    branches = [_branch(f"b{i}") for i in range(4)]
    with pytest.raises(ValidationError, match="watch"):
        ForkSpawnPlan.model_validate(
            _plan(defaults={"agentId": "claude", "mode": "edit"}, branches=branches)
        )


def test_an_explicit_false_watch_is_a_real_answer() -> None:
    branches = [_branch(f"b{i}") for i in range(4)]
    plan = ForkSpawnPlan.model_validate(
        _plan(defaults={"agentId": "claude", "mode": "edit", "watch": False}, branches=branches)
    )
    assert effective_watch(plan, plan.branches[0]) is False
    assert effective_controls(plan, plan.branches[0])["watch"] is False


def test_defaults_omitted_entirely_still_validates_when_branches_are_complete() -> None:
    # `defaults={}` exercises the model; omitting the key exercises the default
    # factory, which is the shape the advertised schema says is legal.
    branches = [
        _branch(f"b{i}", agentId="codex", mode="edit", watch=True) for i in range(4)
    ]
    plan = ForkSpawnPlan.model_validate(
        {
            "sharedGoal": "Prepare the application for release.",
            "branches": branches,
        }
    )
    assert effective_watch(plan, plan.branches[0]) is True


def test_context_file_paths_ride_along_only_when_the_plan_supplied_them() -> None:
    branches = [
        _branch("backend", contextFilePaths=["src/a.py", "src/b.py"]),
        _branch("b2"),
        _branch("b3"),
        _branch("b4"),
    ]
    plan = ForkSpawnPlan.model_validate(_plan(branches=branches))
    assert effective_controls(plan, plan.branches[0])["context"] == {
        "filePaths": ["src/a.py", "src/b.py"]
    }
    # An absent key means the primary model expressed no opinion — an expander
    # naming files it read is adding context, not seizing authority.
    assert "context" not in effective_controls(plan, plan.branches[1])


# ---------------------------------------------------------------------------
# The advertised schema.
# ---------------------------------------------------------------------------


def _objects(node: Any) -> list[dict[str, Any]]:
    """Every object-typed schema node, depth-first."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_objects(item))
    return found


def test_the_tool_definition_uses_the_reserved_internal_namespace() -> None:
    assert FORK_TOOL_WIRE_NAME.startswith("daintree_internal__")
    assert len(FORK_TOOL_WIRE_NAME) <= 64
    assert FORK_TOOL_DEFINITION["function"]["name"] == FORK_TOOL_WIRE_NAME


def test_every_object_level_forbids_additional_properties() -> None:
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    objects = _objects(schema)
    assert objects, "expected at least the root object"
    assert all(node.get("additionalProperties") is False for node in objects)


def test_the_advertised_schema_matches_the_accepted_model() -> None:
    # The schema is hand-written for flat, stable tool-region bytes; the model is
    # what actually validates. This is the seam where they would drift.
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    branch_schema = schema["properties"]["branches"]["items"]

    plan_aliases = {
        field.alias or name for name, field in ForkSpawnPlan.model_fields.items()
    }
    assert set(schema["properties"]) == plan_aliases

    branch_aliases = {
        field.alias or name for name, field in ForkBranch.model_fields.items()
    }
    assert set(branch_schema["properties"]) == branch_aliases

    defaults_aliases = {
        field.alias or name for name, field in ForkDefaults.model_fields.items()
    }
    assert set(schema["properties"]["defaults"]["properties"]) == defaults_aliases

    assert schema["properties"]["branches"]["minItems"] == FORK_BRANCH_COUNT
    assert schema["properties"]["branches"]["maxItems"] == FORK_BRANCH_COUNT


def test_the_description_states_the_call_alone_rule() -> None:
    # The rule the rewriter enforces has to be stated where the model reads the
    # function, not only in the base prompt — a mixed batch is refused outright,
    # so the model must have been told.
    description = FORK_TOOL_DEFINITION["function"]["description"].lower()
    assert "alone" in description
    assert "four" in description


def test_the_spawn_target_is_spelled_both_ways_consistently() -> None:
    assert SPAWN_TOOL_DOTTED_NAME.replace(".", "__") == SPAWN_TOOL_WIRE_NAME


def test_reserved_message_names_share_the_internal_prefix() -> None:
    assert RESERVED_MESSAGE_NAMES
    assert all(name.startswith("daintree_internal_") for name in RESERVED_MESSAGE_NAMES)


def test_find_tool_locates_a_normalized_entry() -> None:
    tools = [
        {"type": "function", "function": {"name": "fs__read", "parameters": {}}},
        {"type": "function", "function": {"name": SPAWN_TOOL_WIRE_NAME, "parameters": {"a": 1}}},
    ]
    found = find_tool(tools, SPAWN_TOOL_WIRE_NAME)
    assert found is not None
    assert found["function"]["parameters"] == {"a": 1}
    assert find_tool(tools, FORK_TOOL_WIRE_NAME) is None
    assert find_tool(None, SPAWN_TOOL_WIRE_NAME) is None
    assert find_tool([{"type": "function"}], SPAWN_TOOL_WIRE_NAME) is None


# ---------------------------------------------------------------------------
# Serialization.
# ---------------------------------------------------------------------------


def test_canonical_json_is_compact_unescaped_and_unsorted() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"b":1,"a":2}'
    assert canonical_json({"t": "日本"}) == '{"t":"日本"}'


def test_canonical_json_of_a_plan_preserves_branch_order() -> None:
    plan = ForkSpawnPlan.model_validate(_plan())
    text = canonical_json(plan.model_dump(by_alias=True))
    # Matched on the id FIELD, not the bare word: "tests" also appears in the
    # shared constraints, and a substring search would report an order the
    # branches array does not have.
    positions = [text.index(f'"id":"{bid}"') for bid in ("backend", "frontend", "tests")]
    assert positions == sorted(positions)


# ---------------------------------------------------------------------------
# Verbatim identifiers.
#
# The value in `worktreeId` selects a mutable checkout, so it is validated as
# the exact string that will be sent. Trimming would mean checking one value and
# spawning into another — and would let two paddings of one id past the
# one-worktree-per-branch rule above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [" wt_backend", "wt_backend ", " ", "\t", "\n"])
def test_padded_or_blank_worktree_ids_are_rejected(bad: str) -> None:
    branches = [_branch("a", worktreeId=bad), _branch("b"), _branch("c"), _branch("d")]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


@pytest.mark.parametrize("bad", [" claude", "claude ", " ", ""])
def test_padded_or_blank_agent_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(
            _plan(defaults={"agentId": bad, "mode": "edit", "watch": True})
        )
    branches = [_branch("a", agentId=bad), _branch("b"), _branch("c"), _branch("d")]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_blank_worktree_ids_cannot_smuggle_four_agents_into_one_checkout() -> None:
    # The failure this specifically prevents: the CLI reads an empty
    # `worktreeId` as ABSENT and falls back to the active worktree, so four
    # different paddings of nothing are four agents in one directory — and they
    # are four distinct strings to a raw uniqueness check.
    branches = [
        _branch("a", worktreeId=" "),
        _branch("b", worktreeId="  "),
        _branch("c", worktreeId="\t"),
        _branch("d", worktreeId="\n"),
    ]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


@pytest.mark.parametrize("bad", ["", " ", "  path"])
def test_context_file_paths_reject_blank_and_padded_entries(bad: str) -> None:
    branches = [
        _branch("a", contextFilePaths=["src/ok.py", bad]),
        _branch("b"),
        _branch("c"),
        _branch("d"),
    ]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_context_file_paths_are_capped() -> None:
    branches = [
        _branch("a", contextFilePaths=[f"src/f{i}.py" for i in range(33)]),
        _branch("b"),
        _branch("c"),
        _branch("d"),
    ]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


@pytest.mark.parametrize("field_name", ["sharedGoal"])
def test_whitespace_does_not_satisfy_a_prose_minimum(field_name: str) -> None:
    # `min_length` counts spaces. Eight of them tell the deterministic fallback
    # nothing, and the fallback is what writes the spawn when a branch's model
    # call fails.
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(**{field_name: " " * 12}))


def test_a_whitespace_brief_is_rejected() -> None:
    branches = [_branch("a", brief=" " * 12), _branch("b"), _branch("c"), _branch("d")]
    with pytest.raises(ValidationError):
        ForkSpawnPlan.model_validate(_plan(branches=branches))


def test_identifier_caps_match_the_contracts_they_are_copied_from() -> None:
    # The primary model is told to quote the roster and the worktree list
    # exactly. A cap tighter than the contract those values ARRIVE under would
    # refuse a plan whose only fault was doing what it was asked.
    from daintree_assistant_server.contracts.extensions import AgentSnapshot, WorktreeSnapshot

    agent_max = next(
        m.max_length
        for m in AgentSnapshot.model_fields["id"].metadata
        if getattr(m, "max_length", None) is not None
    )
    worktree_max = next(
        m.max_length
        for m in WorktreeSnapshot.model_fields["id"].metadata
        if getattr(m, "max_length", None) is not None
    )
    assert agent_max == MAX_AGENT_ID_CHARS
    assert worktree_max == MAX_WORKTREE_ID_CHARS


# ---------------------------------------------------------------------------
# Deep immutability.
# ---------------------------------------------------------------------------


def test_the_branch_collection_cannot_be_mutated_after_validation() -> None:
    # `frozen=True` stops attribute reassignment, not list mutation. The
    # expansion service holds this object across four concurrent calls, so a
    # plan that could grow a fifth branch afterwards would have passed every
    # invariant and still be wrong by the time it is read.
    plan = ForkSpawnPlan.model_validate(_plan())
    assert isinstance(plan.branches, tuple)
    assert isinstance(plan.shared_constraints, tuple)
    with pytest.raises(TypeError):
        plan.branches[0] = plan.branches[1]  # type: ignore[index]
    with pytest.raises(AttributeError):
        plan.branches.append(plan.branches[0])  # type: ignore[attr-defined]


def test_context_paths_cannot_be_mutated_after_validation() -> None:
    branches = [
        _branch("a", contextFilePaths=["src/a.py"]),
        _branch("b"),
        _branch("c"),
        _branch("d"),
    ]
    plan = ForkSpawnPlan.model_validate(_plan(branches=branches))
    assert isinstance(plan.branches[0].context_file_paths, tuple)
    with pytest.raises(AttributeError):
        plan.branches[0].context_file_paths.append("src/b.py")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The effective_* guards.
#
# Not dead code: `model_construct` skips validation entirely, so server code can
# hand these helpers a plan `_validate_semantics` never saw.
# ---------------------------------------------------------------------------


def _unvalidated_plan() -> ForkSpawnPlan:
    branch = ForkBranch.model_construct(
        id="a", worktree_id="wt_a", brief="x" * 12, agent_id=None, mode=None, watch=None
    )
    return ForkSpawnPlan.model_construct(
        shared_goal="goal enough",
        shared_constraints=(),
        defaults=ForkDefaults.model_construct(agent_id=None, mode=None, watch=None),
        branches=(branch,),
    )


def test_the_helpers_raise_rather_than_guess_on_an_unvalidated_plan() -> None:
    plan = _unvalidated_plan()
    branch = plan.branches[0]
    with pytest.raises(ValueError, match="agentId"):
        effective_agent_id(plan, branch)
    with pytest.raises(ValueError, match="mode"):
        effective_mode(plan, branch)
    with pytest.raises(ValueError, match="watch"):
        effective_watch(plan, branch)


# ---------------------------------------------------------------------------
# Reserved message names.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "daintree_internal_fork_branch",
        "DAINTREE_INTERNAL_FORK_BRANCH",
        " Daintree_Internal_Fork_Branch ",
        "daintree_internal_anything_added_later",
    ],
)
def test_the_whole_internal_message_namespace_is_reserved(name: str) -> None:
    assert is_reserved_message_name(name)


@pytest.mark.parametrize("name", [None, "", "greg", "daintree_compaction", "internal_fork"])
def test_ordinary_message_names_are_not_reserved(name: str | None) -> None:
    # `daintree_compaction` in particular: it is a real reserved name with a
    # different owner, dropped by the assembler rather than refused at the door.
    assert not is_reserved_message_name(name)


# ---------------------------------------------------------------------------
# Schema drift, the half a name comparison cannot see.
#
# Property NAMES matching proves the two describe the same fields; it says
# nothing about whether a value the schema calls legal is one the validator will
# accept. That gap is the expensive one: a plan the model was told it could
# write, refused after it wrote it, costs a repair round-trip on the critical
# path this feature exists to shorten.
# ---------------------------------------------------------------------------


def _declared_bounds(model: type[Any], field_name: str) -> dict[str, Any]:
    """The JSON-Schema-expressible constraints Pydantic will actually enforce."""
    bounds: dict[str, Any] = {}
    for meta in model.model_fields[field_name].metadata:
        for attr, keyword in (
            ("min_length", "minLength"),
            ("max_length", "maxLength"),
            ("pattern", "pattern"),
        ):
            value = getattr(meta, attr, None)
            if value is not None:
                bounds[keyword] = value
    return bounds


def _schema_bounds(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k in {"minLength", "maxLength", "pattern"}}


@pytest.mark.parametrize(
    ("model_name", "field_name", "alias"),
    [
        ("plan", "shared_goal", "sharedGoal"),
        ("branch", "id", "id"),
        ("branch", "brief", "brief"),
        ("branch", "title_hint", "titleHint"),
        ("branch", "acceptance_hint", "acceptanceHint"),
        ("branch", "watch_goal_hint", "watchGoalHint"),
    ],
)
def test_advertised_string_bounds_match_the_enforced_ones(
    model_name: str, field_name: str, alias: str
) -> None:
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    if model_name == "plan":
        model: type[Any] = ForkSpawnPlan
        node = schema["properties"][alias]
    else:
        model = ForkBranch
        node = schema["properties"]["branches"]["items"]["properties"][alias]
    assert _schema_bounds(node) == _declared_bounds(model, field_name)


def test_advertised_collection_bounds_match_the_enforced_ones() -> None:
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    branch_schema = schema["properties"]["branches"]["items"]
    assert schema["properties"]["sharedConstraints"]["maxItems"] == _declared_bounds(
        ForkSpawnPlan, "shared_constraints"
    )["maxLength"]
    assert branch_schema["properties"]["contextFilePaths"]["maxItems"] == _declared_bounds(
        ForkBranch, "context_file_paths"
    )["maxLength"]


def test_advertised_identifier_bounds_match_the_enforced_ones() -> None:
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    branch_schema = schema["properties"]["branches"]["items"]
    for node in (
        branch_schema["properties"]["worktreeId"],
        branch_schema["properties"]["agentId"],
        schema["properties"]["defaults"]["properties"]["agentId"],
    ):
        assert node["minLength"] == 1
    assert branch_schema["properties"]["worktreeId"]["maxLength"] == MAX_WORKTREE_ID_CHARS
    assert branch_schema["properties"]["agentId"]["maxLength"] == MAX_AGENT_ID_CHARS


def test_advertised_required_and_enum_match_the_model() -> None:
    schema = FORK_TOOL_DEFINITION["function"]["parameters"]
    branch_schema = schema["properties"]["branches"]["items"]

    plan_required = {
        field.alias or name
        for name, field in ForkSpawnPlan.model_fields.items()
        if field.is_required()
    }
    assert set(schema["required"]) == plan_required

    branch_required = {
        field.alias or name
        for name, field in ForkBranch.model_fields.items()
        if field.is_required()
    }
    assert set(branch_schema["required"]) == branch_required

    for node in (
        branch_schema["properties"]["mode"],
        schema["properties"]["defaults"]["properties"]["mode"],
    ):
        assert node["enum"] == ["edit", "explore"]


def test_the_cross_field_rules_are_stated_in_prose_because_json_schema_cannot() -> None:
    # Unique branch ids, one worktree per branch, and an effective
    # agent/mode/watch are all cross-field: no JSON Schema keyword expresses
    # them, so the only way the model learns them before it writes a plan is the
    # description. If they move, this is the reminder that the prose has to too.
    description = FORK_TOOL_DEFINITION["function"]["description"].lower()
    assert "own already-resolved worktree" in description
    assert "watch" in description
